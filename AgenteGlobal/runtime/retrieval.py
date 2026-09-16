"""Deterministic local retrieval and bounded sub-agent context slicing.

The module intentionally contains no model, provider, or core-runtime import.
Lexical retrieval is the always-available baseline.  A semantic backend is an
explicit dependency-injection point: this module never discovers, creates, or
pretends to use an embedding service on its own.
"""

from __future__ import annotations

import inspect
import json
import math
import re
import unicodedata
from enum import StrEnum
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from .contracts import TaskSpec
from .session_state import (
    ArtifactReference,
    ChunkReference,
    SessionDecision,
    SessionFact,
    SessionObjective,
    SessionState,
    TurnState,
    UsageMetrics,
)


MAX_CHUNKS = 100_000
MAX_CHUNK_ID_LENGTH = 256
MAX_CHUNK_TEXT_CHARS = 1_000_000
MAX_QUERY_CHARS = 20_000
MAX_TOP_K = 1_000
MAX_METADATA_BYTES = 256 * 1024
MAX_CONTEXT_ITEMS = 1_000
MAX_CONTEXT_CHARS = 1_000_000

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)


class RetrievalError(ValueError):
    """Invalid retrieval input or index operation."""


class RetrievalStrategy(StrEnum):
    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class SemanticBackend(Protocol):
    """Minimal injected semantic adapter contract.

    Implementations may accept the canonical keyword arguments shown below or
    a smaller compatible signature.  Returned values are normalized by
    :class:`ChunkRetriever`; the backend must not be trusted with filesystem
    paths or credentials by this module.
    """

    def search(
        self,
        query: str,
        chunks: Sequence["Chunk"],
        *,
        top_k: int,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[Any]: ...


class Chunk(BaseModel):
    """A bounded, traceable piece of source content.

    ``source_path`` is metadata only and must be relative to the caller's
    workspace.  Retrieval never reads it; callers decide whether the path is
    allowed before exposing content to a worker.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=False,
        validate_assignment=True,
        frozen=True,
        populate_by_name=True,
    )

    id: str = Field(
        validation_alias=AliasChoices("id", "chunk_id"),
        min_length=1,
        max_length=MAX_CHUNK_ID_LENGTH,
    )
    text: str = Field(
        validation_alias=AliasChoices("text", "content"),
        min_length=1,
        max_length=MAX_CHUNK_TEXT_CHARS,
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifact_id: str | None = Field(default=None, max_length=256)
    source_path: str | None = Field(default=None, max_length=4_096)
    ordinal: int = Field(default=0, ge=0, le=10_000_000)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if _ID_RE.fullmatch(value) is None or value.startswith("."):
            raise ValueError("Chunk id must be a safe opaque identifier.")
        return value

    @field_validator("artifact_id")
    @classmethod
    def _valid_artifact_id(cls, value: str | None) -> str | None:
        if value is not None and (_ID_RE.fullmatch(value) is None or value.startswith(".")):
            raise ValueError("artifact_id must be a safe opaque identifier.")
        return value

    @field_validator("source_path")
    @classmethod
    def _valid_source_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _validate_relative_path(value, name="source_path")
        return value.replace("\\", "/")

    @field_validator("metadata")
    @classmethod
    def _valid_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ValueError("Chunk metadata must be JSON serializable.") from error
        if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
            raise ValueError("Chunk metadata exceeds the bounded size limit.")
        return value

    @property
    def chunk_id(self) -> str:
        """Compatibility alias for callers that use the domain term."""

        return self.id

    @property
    def content(self) -> str:
        return self.text


ChunkRecord = Chunk
DocumentChunk = Chunk


class MetadataFilter(BaseModel):
    """Optional typed metadata predicates used by lexical and hybrid search."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=False, frozen=True)

    equals: dict[str, Any] = Field(default_factory=dict)
    contains: dict[str, Any] = Field(default_factory=dict)
    any_of: dict[str, list[Any]] = Field(default_factory=dict)

    @field_validator("equals", "contains", "any_of")
    @classmethod
    def _json_values(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ValueError("Metadata filters must be JSON serializable.") from error
        return value

    def matches(self, metadata: Mapping[str, Any]) -> bool:
        for key, expected in self.equals.items():
            if key not in metadata or not _metadata_equal(metadata[key], expected):
                return False
        for key, expected in self.contains.items():
            if key not in metadata or not _metadata_contains(metadata[key], expected):
                return False
        for key, options in self.any_of.items():
            if key not in metadata or not any(_metadata_equal(metadata[key], option) for option in options):
                return False
        return True


MetadataFilters = MetadataFilter


class SemanticMatch(BaseModel):
    """Backend-neutral semantic result accepted from an injected adapter."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    chunk_id: str = Field(
        validation_alias=AliasChoices("chunk_id", "id"),
        min_length=1,
        max_length=MAX_CHUNK_ID_LENGTH,
    )
    score: float

    @field_validator("score")
    @classmethod
    def _finite_score(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Semantic scores must be finite numbers.")
        return value


class RetrievalHit(BaseModel):
    """One deterministic retrieval result with source-specific scores."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    chunk: Chunk
    score: float = Field(ge=0.0)
    lexical_score: float = Field(default=0.0, ge=0.0)
    semantic_score: float = Field(default=0.0, ge=0.0)
    source: str = "lexical"
    rank: int = Field(default=1, ge=1, le=MAX_TOP_K)

    @property
    def chunk_id(self) -> str:
        return self.chunk.id

    @property
    def id(self) -> str:
        return self.chunk.id

    @property
    def document(self) -> Chunk:
        return self.chunk


class RetrievalResponse(BaseModel):
    """Retrieval output and an explicit semantic capability/status signal."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    query: str
    requested_strategy: RetrievalStrategy
    strategy_used: str
    hits: list[RetrievalHit] = Field(default_factory=list)
    total_candidates: int = Field(default=0, ge=0)
    semantic_available: bool = False
    semantic_used: bool = False
    semantic_status: str = "not_requested"
    semantic_error: str | None = None

    @property
    def results(self) -> list[RetrievalHit]:
        return self.hits

    @property
    def availability(self) -> str:
        return self.semantic_status

    @property
    def semantic_unavailable(self) -> bool:
        return self.semantic_status == "unavailable"

    @property
    def semantic_backend_available(self) -> bool:
        return self.semantic_available

    @property
    def unavailable_reason(self) -> str | None:
        return self.semantic_error


class RetrievalQuery(BaseModel):
    """Validated query object for callers that prefer a typed request."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    top_k: int = Field(default=5, ge=1, le=MAX_TOP_K)
    strategy: RetrievalStrategy = RetrievalStrategy.LEXICAL
    filters: MetadataFilter | dict[str, Any] | None = None


class ChunkRetriever:
    """In-memory deterministic lexical index with optional semantic injection."""

    def __init__(
        self,
        chunks: Sequence[Chunk | Mapping[str, Any]] = (),
        *,
        semantic_backend: SemanticBackend | Any | None = None,
        max_chunks: int = MAX_CHUNKS,
    ) -> None:
        if isinstance(max_chunks, bool) or not isinstance(max_chunks, int) or not 1 <= max_chunks <= MAX_CHUNKS:
            raise ValueError(f"max_chunks must be an integer between 1 and {MAX_CHUNKS}.")
        self._max_chunks = max_chunks
        self._chunks: dict[str, Chunk] = {}
        self._semantic_backend = semantic_backend
        for chunk in chunks:
            self.add(chunk)

    @property
    def chunks(self) -> tuple[Chunk, ...]:
        return tuple(self._chunks.values())

    @property
    def semantic_backend(self) -> Any | None:
        return self._semantic_backend

    def add(self, chunk: Chunk | Mapping[str, Any], *, replace: bool = False) -> Chunk:
        normalized = chunk if isinstance(chunk, Chunk) else Chunk.model_validate(chunk)
        if normalized.id in self._chunks and not replace:
            raise RetrievalError(f"Duplicate chunk id: {normalized.id}")
        if normalized.id not in self._chunks and len(self._chunks) >= self._max_chunks:
            raise RetrievalError("Chunk index reached its configured size limit.")
        self._chunks[normalized.id] = normalized
        return normalized

    add_chunk = add

    def remove(self, chunk_id: str) -> None:
        _validate_identifier(chunk_id, name="chunk_id")
        self._chunks.pop(chunk_id, None)

    def search(
        self,
        query: str | RetrievalQuery,
        top_k: int | None = None,
        filters: MetadataFilter | Mapping[str, Any] | None = None,
        strategy: RetrievalStrategy | str | None = None,
    ) -> RetrievalResponse:
        """Search chunks, reporting semantic unavailability instead of guessing.

        Mapping filters with plain values mean equality.  A mapping with
        ``equals``, ``contains`` or ``any_of`` keys is interpreted as a
        :class:`MetadataFilter` object.
        """

        request = self._normalize_request(query, top_k=top_k, filters=filters, strategy=strategy)
        metadata_filter = _coerce_filter(request.filters)
        candidates = tuple(chunk for chunk in self._chunks.values() if metadata_filter.matches(chunk.metadata))
        lexical = self._lexical_hits(request.query, candidates, request.top_k)

        if request.strategy is RetrievalStrategy.LEXICAL:
            return RetrievalResponse(
                query=request.query,
                requested_strategy=request.strategy,
                strategy_used="lexical",
                hits=lexical,
                total_candidates=len(candidates),
            )

        semantic_hits: list[RetrievalHit] = []
        semantic_available, semantic_error = self._semantic_availability()
        if semantic_available:
            try:
                raw = self._invoke_semantic_backend(
                    request.query,
                    candidates,
                    request.top_k,
                    request.filters,
                )
                semantic_hits = self._normalize_semantic_hits(raw, candidates, request.top_k)
            except Exception as error:  # backend is an untrusted extension boundary
                semantic_available = False
                semantic_error = f"semantic backend unavailable: {type(error).__name__}"

        if request.strategy is RetrievalStrategy.SEMANTIC:
            if semantic_available:
                hits = self._with_ranks(semantic_hits)
                return RetrievalResponse(
                    query=request.query,
                    requested_strategy=request.strategy,
                    strategy_used="semantic",
                    hits=hits,
                    total_candidates=len(candidates),
                    semantic_available=True,
                    semantic_used=True,
                    semantic_status="used",
                )
            return RetrievalResponse(
                query=request.query,
                requested_strategy=request.strategy,
                strategy_used="lexical_fallback",
                hits=lexical,
                total_candidates=len(candidates),
                semantic_available=False,
                semantic_used=False,
                semantic_status="unavailable",
                semantic_error=semantic_error or "no semantic backend was explicitly injected",
            )

        if semantic_available:
            hits = self._hybrid_hits(lexical, semantic_hits, request.top_k)
            return RetrievalResponse(
                query=request.query,
                requested_strategy=request.strategy,
                strategy_used="hybrid",
                hits=hits,
                total_candidates=len(candidates),
                semantic_available=True,
                semantic_used=True,
                semantic_status="used",
            )
        return RetrievalResponse(
            query=request.query,
            requested_strategy=request.strategy,
            strategy_used="lexical_fallback",
            hits=lexical,
            total_candidates=len(candidates),
            semantic_available=False,
            semantic_used=False,
            semantic_status="unavailable",
            semantic_error=semantic_error or "no semantic backend was explicitly injected",
        )

    retrieve = search

    def _normalize_request(
        self,
        query: str | RetrievalQuery,
        *,
        top_k: int | None,
        filters: MetadataFilter | Mapping[str, Any] | None,
        strategy: RetrievalStrategy | str | None,
    ) -> RetrievalQuery:
        if isinstance(query, RetrievalQuery):
            if top_k is not None or filters is not None or strategy is not None:
                raise RetrievalError("Do not combine RetrievalQuery with individual search arguments.")
            return query
        if not isinstance(query, str) or not query.strip():
            raise RetrievalError("query must be a non-empty string.")
        if len(query) > MAX_QUERY_CHARS:
            raise RetrievalError("query exceeds the bounded size limit.")
        chosen_top_k = 5 if top_k is None else top_k
        if isinstance(chosen_top_k, bool) or not isinstance(chosen_top_k, int) or not 1 <= chosen_top_k <= MAX_TOP_K:
            raise RetrievalError(f"top_k must be an integer between 1 and {MAX_TOP_K}.")
        try:
            chosen_strategy = RetrievalStrategy.LEXICAL if strategy is None else RetrievalStrategy(strategy)
        except ValueError as error:
            raise RetrievalError("strategy must be lexical, semantic, or hybrid.") from error
        return RetrievalQuery(query=query, top_k=chosen_top_k, strategy=chosen_strategy, filters=filters)

    @staticmethod
    def _lexical_hits(query: str, candidates: Sequence[Chunk], top_k: int) -> list[RetrievalHit]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        query_unique = tuple(dict.fromkeys(query_tokens))
        scored: list[tuple[float, str, float]] = []
        normalized_phrase = _normalize(query)
        for chunk in candidates:
            normalized_text = _normalize(chunk.text)
            tokens = _tokens(normalized_text)
            if not tokens:
                continue
            frequencies = {token: tokens.count(token) for token in query_unique}
            matched = sum(1 for token in query_unique if frequencies[token])
            if not matched:
                continue
            occurrences = sum(frequencies.values())
            score = matched / len(query_unique)
            score += min(0.25, occurrences / max(1, len(tokens)) * 0.25)
            if normalized_phrase and normalized_phrase in normalized_text:
                score += 0.5
            scored.append((score, chunk.id, float(score)))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            RetrievalHit(
                chunk=self_chunk,
                score=score,
                lexical_score=score,
                source="lexical",
                rank=rank,
            )
            for rank, (_, chunk_id, score) in enumerate(scored[:top_k], start=1)
            for self_chunk in (next(chunk for chunk in candidates if chunk.id == chunk_id),)
        ]

    def _semantic_availability(self) -> tuple[bool, str | None]:
        backend = self._semantic_backend
        if backend is None:
            return False, "no semantic backend was explicitly injected"
        search = getattr(backend, "search", None)
        if not callable(search):
            search = getattr(backend, "retrieve", None)
        if not callable(search):
            return False, "injected semantic backend has no callable search method"
        marker = getattr(backend, "reliable", getattr(backend, "is_reliable", True))
        try:
            reliable = marker() if callable(marker) else marker
        except Exception as error:
            return False, f"semantic backend reliability check failed: {type(error).__name__}"
        if reliable is False:
            return False, "injected semantic backend is marked unreliable"
        available = getattr(backend, "available", True)
        try:
            available = available() if callable(available) else available
        except Exception as error:
            return False, f"semantic backend availability check failed: {type(error).__name__}"
        if available is False:
            return False, "injected semantic backend reports unavailable"
        return True, None

    def _invoke_semantic_backend(
        self,
        query: str,
        candidates: Sequence[Chunk],
        top_k: int,
        filters: MetadataFilter | Mapping[str, Any] | None,
    ) -> Sequence[Any]:
        backend = self._semantic_backend
        if backend is None:  # guarded by _semantic_availability
            raise RetrievalError("semantic backend is not configured")
        method = getattr(backend, "search", None)
        if not callable(method):
            method = getattr(backend, "retrieve", None)
        if not callable(method):
            raise RetrievalError("semantic backend has no search method")
        # Prefer the canonical contract.  Signature binding permits small
        # offline fakes (query-only, query+chunks, or query+top_k) without
        # catching a TypeError raised from inside a valid backend.
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            return method(query, candidates, top_k=top_k, filters=filters)
        canonical = {"chunks": candidates, "top_k": top_k, "filters": filters}
        try:
            signature.bind(query, **canonical)
        except TypeError:
            for args, kwargs in (
                ((query, candidates), {"top_k": top_k, "filters": filters}),
                ((query, candidates), {"top_k": top_k}),
                ((query, candidates, top_k), {}),
                ((query, candidates, top_k, filters), {}),
                ((query, top_k), {}),
                ((query,), {}),
            ):
                try:
                    signature.bind(*args, **kwargs)
                except TypeError:
                    continue
                return method(*args, **kwargs)
            raise RetrievalError("semantic backend does not implement a supported search signature")
        return method(query, **canonical)

    @staticmethod
    def _normalize_semantic_hits(
        raw: Sequence[Any], candidates: Sequence[Chunk], top_k: int
    ) -> list[RetrievalHit]:
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
            raise RetrievalError("semantic backend must return a sequence of matches")
        by_id = {chunk.id: chunk for chunk in candidates}
        normalized: dict[str, float] = {}
        if isinstance(raw, Mapping):
            if any(key in raw for key in ("chunk_id", "id", "score", "similarity")):
                raw_items: Iterable[Any] = (raw,)
            else:
                raw_items = tuple({"chunk_id": key, "score": value} for key, value in raw.items())
        else:
            raw_items = raw
        for item in raw_items:
            match = _coerce_semantic_match(item)
            if match is None or match.chunk_id not in by_id:
                continue
            if match.chunk_id not in normalized or match.score > normalized[match.chunk_id]:
                normalized[match.chunk_id] = match.score
        ordered = sorted(normalized.items(), key=lambda pair: (-pair[1], pair[0]))[:top_k]
        return [
            RetrievalHit(
                chunk=by_id[chunk_id],
                score=max(0.0, score),
                semantic_score=max(0.0, score),
                source="semantic",
                rank=rank,
            )
            for rank, (chunk_id, score) in enumerate(ordered, start=1)
        ]

    @staticmethod
    def _hybrid_hits(
        lexical: Sequence[RetrievalHit], semantic: Sequence[RetrievalHit], top_k: int
    ) -> list[RetrievalHit]:
        lexical_by_id = {hit.chunk.id: hit for hit in lexical}
        semantic_by_id = {hit.chunk.id: hit for hit in semantic}
        max_lexical = max((hit.lexical_score for hit in lexical), default=0.0)
        semantic_scores = [hit.semantic_score for hit in semantic]
        max_semantic = max(semantic_scores, default=0.0)
        min_semantic = min(semantic_scores, default=0.0)
        combined: list[tuple[float, str, RetrievalHit | None, RetrievalHit | None]] = []
        for chunk_id in sorted(set(lexical_by_id) | set(semantic_by_id)):
            lexical_hit = lexical_by_id.get(chunk_id)
            semantic_hit = semantic_by_id.get(chunk_id)
            lexical_score = (lexical_hit.lexical_score / max_lexical) if lexical_hit and max_lexical else 0.0
            if semantic_hit is None:
                semantic_score = 0.0
            elif max_semantic == min_semantic:
                semantic_score = 1.0 if max_semantic > 0 else 0.0
            else:
                semantic_score = (semantic_hit.semantic_score - min_semantic) / (max_semantic - min_semantic)
            score = 0.5 * lexical_score + 0.5 * semantic_score
            combined.append((score, chunk_id, lexical_hit, semantic_hit))
        combined.sort(key=lambda item: (-item[0], item[1]))
        output: list[RetrievalHit] = []
        for rank, (score, _, lexical_hit, semantic_hit) in enumerate(combined[:top_k], start=1):
            chunk = semantic_hit.chunk if semantic_hit is not None else lexical_hit.chunk  # type: ignore[union-attr]
            output.append(
                RetrievalHit(
                    chunk=chunk,
                    score=score,
                    lexical_score=lexical_hit.lexical_score if lexical_hit else 0.0,
                    semantic_score=semantic_hit.semantic_score if semantic_hit else 0.0,
                    source="hybrid",
                    rank=rank,
                )
            )
        return output

    @staticmethod
    def _with_ranks(hits: Sequence[RetrievalHit]) -> list[RetrievalHit]:
        return [hit.model_copy(update={"rank": rank}) for rank, hit in enumerate(hits, start=1)]


RetrievalIndex = ChunkRetriever
HybridRetriever = ChunkRetriever
LexicalRetriever = ChunkRetriever
RetrievalResult = RetrievalResponse
SearchResult = RetrievalResponse
ChunkHit = RetrievalHit
SearchStrategy = RetrievalStrategy


class ContextSlicePolicy(BaseModel):
    """Allowlist and budget policy for one worker's context slice.

    The defaults select bounded structured state and objective-relevant
    chunks, while history and global context remain excluded.  This is a
    selection policy, not an authorization grant.
    """

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=False, frozen=True)

    include_facts: bool = True
    include_decisions: bool = True
    include_objectives: bool = True
    include_artifact_refs: bool = True
    include_chunk_refs: bool = True
    include_retrieved_chunks: bool = True
    include_current_turn: bool = True
    include_usage: bool = True
    include_history: bool = False
    include_global_context: bool = False
    max_facts: int = Field(default=50, ge=0, le=MAX_CONTEXT_ITEMS)
    max_decisions: int = Field(default=50, ge=0, le=MAX_CONTEXT_ITEMS)
    max_objectives: int = Field(default=20, ge=0, le=MAX_CONTEXT_ITEMS)
    max_artifact_refs: int = Field(default=100, ge=0, le=MAX_CONTEXT_ITEMS)
    max_chunk_refs: int = Field(default=200, ge=0, le=MAX_CONTEXT_ITEMS)
    max_chunks: int = Field(default=8, ge=0, le=MAX_TOP_K)
    max_chars: int = Field(default=MAX_CONTEXT_CHARS, ge=1, le=MAX_CONTEXT_CHARS)
    retrieval_strategy: RetrievalStrategy = RetrievalStrategy.LEXICAL
    allowed_artifact_ids: tuple[str, ...] = ()
    allowed_chunk_ids: tuple[str, ...] = ()
    allowed_source_paths: tuple[str, ...] = ()
    filter_state_by_task: bool = True

    @field_validator("allowed_artifact_ids", "allowed_chunk_ids", mode="before")
    @classmethod
    def _allowlist_sequences(cls, values: Any) -> Any:
        if isinstance(values, (tuple, list)):
            return tuple(values)
        return values

    @field_validator("allowed_artifact_ids", "allowed_chunk_ids")
    @classmethod
    def _valid_allowlist_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _validate_identifier(value, name="allowlist id")
        if len(set(values)) != len(values):
            raise ValueError("Allowlist identifiers must be unique.")
        return values

    @field_validator("allowed_source_paths", mode="before")
    @classmethod
    def _path_allowlist_sequence(cls, values: Any) -> Any:
        if isinstance(values, (tuple, list)):
            return tuple(values)
        return values

    @field_validator("allowed_source_paths")
    @classmethod
    def _valid_allowlist_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _validate_relative_path(value, name="allowed_source_paths")
        if len(set(values)) != len(values):
            raise ValueError("Allowlist paths must be unique.")
        return tuple(value.replace("\\", "/") for value in values)


class ContextSlice(BaseModel):
    """Minimal, task-bound context returned to a subagent."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    task_id: str
    objective: str
    facts: list[SessionFact] = Field(default_factory=list)
    decisions: list[SessionDecision] = Field(default_factory=list)
    objectives: list[SessionObjective] = Field(default_factory=list)
    artifact_refs: list[ArtifactReference] = Field(default_factory=list)
    chunk_refs: list[ChunkReference] = Field(default_factory=list)
    chunks: list[Chunk] = Field(default_factory=list)
    current_turn: TurnState | None = None
    usage: UsageMetrics | None = None
    history: tuple[str, ...] = ()
    global_context: None = None
    omitted_counts: dict[str, int] = Field(default_factory=dict)
    retrieval_status: str = "not_run"
    retrieval_error: str | None = None

    @property
    def selected_chunks(self) -> list[Chunk]:
        return self.chunks

    def to_prompt_data(self) -> dict[str, Any]:
        """Return serializable data without adding hidden/global context."""

        return self.model_dump(mode="json", exclude_none=True)


class ContextSlicer:
    """Build task-scoped slices without importing or invoking the Core."""

    def __init__(self, retriever: ChunkRetriever | None = None) -> None:
        self._retriever = retriever

    def slice(
        self,
        task_spec: TaskSpec,
        session_state: SessionState,
        chunks: Sequence[Chunk | Mapping[str, Any]] = (),
        *,
        policy: ContextSlicePolicy | Any | None = None,
        filters: MetadataFilter | Mapping[str, Any] | None = None,
        retriever: ChunkRetriever | None = None,
    ) -> ContextSlice:
        if not isinstance(task_spec, TaskSpec):
            task_spec = TaskSpec.model_validate(task_spec)
        if not isinstance(session_state, SessionState):
            session_state = SessionState.model_validate(session_state)
        selected_policy = policy if policy is not None else ContextSlicePolicy()
        if not isinstance(selected_policy, ContextSlicePolicy):
            selected_policy = _adapt_policy(selected_policy)
        selected_facts = _select_state_items(session_state.facts, task_spec, selected_policy, filters, "facts") if selected_policy.include_facts else []
        selected_decisions = _select_state_items(session_state.decisions, task_spec, selected_policy, filters, "decisions") if selected_policy.include_decisions else []
        selected_objectives = _select_state_items(session_state.objectives, task_spec, selected_policy, filters, "objectives") if selected_policy.include_objectives else []
        selected_artifacts = (
            _select_artifact_refs(session_state.artifact_refs, task_spec, selected_policy)
            if selected_policy.include_artifact_refs
            else []
        )
        selected_chunk_refs = _select_chunk_refs(session_state.chunk_refs, task_spec, selected_policy)

        active_retriever = retriever or self._retriever
        if active_retriever is None and chunks:
            active_retriever = ChunkRetriever(chunks)
        retrieved: list[Chunk] = []
        retrieval_status = "not_run"
        retrieval_error: str | None = None
        if active_retriever is not None and selected_policy.include_retrieved_chunks and selected_policy.max_chunks:
            # Ask for the complete bounded candidate set before applying the
            # task's source/read-set boundary.  Otherwise disallowed top hits
            # could consume ``max_chunks`` and hide an allowed lower-ranked
            # chunk.
            retrieval_top_k = min(MAX_TOP_K, max(selected_policy.max_chunks, len(active_retriever.chunks)))
            result = active_retriever.search(
                task_spec.objective,
                top_k=retrieval_top_k,
                filters=filters,
                strategy=selected_policy.retrieval_strategy,
            )
            retrieved = [
                hit.chunk
                for hit in result.hits
                if _chunk_allowed_for_task(hit.chunk, task_spec, selected_policy)
            ][: selected_policy.max_chunks]
            retrieval_status = result.semantic_status if result.requested_strategy is not RetrievalStrategy.LEXICAL else "lexical"
            retrieval_error = result.semantic_error

        omitted: dict[str, int] = {}
        for name, original, selected in (
            ("facts", session_state.facts, selected_facts),
            ("decisions", session_state.decisions, selected_decisions),
            ("objectives", session_state.objectives, selected_objectives),
            ("artifact_refs", session_state.artifact_refs, selected_artifacts),
            ("chunk_refs", session_state.chunk_refs, selected_chunk_refs),
        ):
            if len(original) > len(selected):
                omitted[name] = len(original) - len(selected)
        if len(retrieved) < len(active_retriever.chunks) if active_retriever is not None else False:
            omitted["chunks"] = max(0, len(active_retriever.chunks) - len(retrieved))

        output = ContextSlice(
            task_id=task_spec.task_id,
            objective=task_spec.objective,
            facts=selected_facts,
            decisions=selected_decisions,
            objectives=selected_objectives,
            artifact_refs=selected_artifacts,
            chunk_refs=selected_chunk_refs,
            chunks=retrieved,
            current_turn=session_state.current_turn if selected_policy.include_current_turn else None,
            usage=session_state.usage if selected_policy.include_usage else None,
            # Deliberately empty even if a legacy policy asks for them.
            history=(),
            global_context=None,
            omitted_counts=omitted,
            retrieval_status=retrieval_status,
            retrieval_error=retrieval_error,
        )
        if len(json.dumps(output.to_prompt_data(), ensure_ascii=False).encode("utf-8")) > selected_policy.max_chars:
            raise RetrievalError("Context slice exceeds the policy max_chars limit.")
        return output


def slice_context(
    task_spec: TaskSpec,
    session_state: SessionState,
    chunks: Sequence[Chunk | Mapping[str, Any]] = (),
    *,
    policy: ContextSlicePolicy | Any | None = None,
    filters: MetadataFilter | Mapping[str, Any] | None = None,
    retriever: ChunkRetriever | None = None,
) -> ContextSlice:
    """Functional API for one task-scoped context slice."""

    return ContextSlicer(retriever).slice(
        task_spec,
        session_state,
        chunks,
        policy=policy,
        filters=filters,
    )


build_context_slice = slice_context
create_context_slice = slice_context
slice_context_for_subagent = slice_context


def _validate_identifier(value: str, *, name: str) -> None:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None or value.startswith("."):
        raise ValueError(f"{name} must be a safe opaque identifier.")


def _validate_relative_path(value: str, *, name: str) -> None:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty relative path.")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized) or any(part == ".." for part in normalized.split("/")):
        raise ValueError(f"{name} must stay inside the workspace and cannot contain '..'.")


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _tokens(value: str) -> list[str]:
    return [token for token in _TOKEN_RE.findall(_normalize(value)) if token]


def _metadata_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, (list, tuple, set)) and not isinstance(expected, (list, tuple, set, dict)):
        return expected in actual
    return actual == expected


def _metadata_contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, str) and isinstance(expected, str):
        return _normalize(expected) in _normalize(actual)
    if isinstance(actual, Mapping) and isinstance(expected, Mapping):
        return all(key in actual and _metadata_equal(actual[key], value) for key, value in expected.items())
    if isinstance(actual, (list, tuple, set)):
        if isinstance(expected, (list, tuple, set)):
            return all(item in actual for item in expected)
        return expected in actual
    return False


def _coerce_filter(value: MetadataFilter | Mapping[str, Any] | None) -> MetadataFilter:
    if value is None:
        return MetadataFilter()
    if isinstance(value, MetadataFilter):
        return value
    if not isinstance(value, Mapping):
        raise RetrievalError("filters must be a mapping or MetadataFilter.")
    if any(key in value for key in ("equals", "contains", "any_of")):
        return MetadataFilter.model_validate(value)
    return MetadataFilter(equals=dict(value))


def _coerce_semantic_match(value: Any) -> SemanticMatch | None:
    if isinstance(value, SemanticMatch):
        return value
    if isinstance(value, RetrievalHit):
        return SemanticMatch(chunk_id=value.chunk.id, score=value.semantic_score or value.score)
    if isinstance(value, Chunk):
        return SemanticMatch(chunk_id=value.id, score=1.0)
    if isinstance(value, Mapping):
        chunk = value.get("chunk")
        chunk_id = value.get("chunk_id", value.get("id"))
        if isinstance(chunk, Chunk):
            chunk_id = chunk.id
        score = value.get("score", value.get("similarity"))
        if chunk_id is None or score is None:
            return None
        return SemanticMatch(chunk_id=str(chunk_id), score=float(score))
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        chunk_id = value[0].id if isinstance(value[0], Chunk) else value[0]
        return SemanticMatch(chunk_id=str(chunk_id), score=float(value[1]))
    chunk_id = getattr(value, "chunk_id", getattr(value, "id", None))
    score = getattr(value, "score", getattr(value, "similarity", None))
    if chunk_id is None or score is None:
        return None
    return SemanticMatch(chunk_id=str(chunk_id), score=float(score))


def _adapt_policy(policy: Any) -> ContextSlicePolicy:
    values: dict[str, Any] = {}
    for field_name in ContextSlicePolicy.model_fields:
        if hasattr(policy, field_name):
            values[field_name] = getattr(policy, field_name)
    # Existing manifest ContextPolicy intentionally has no global state fields.
    return ContextSlicePolicy.model_validate(values)


def _entry_metadata(entry: Any) -> dict[str, Any]:
    data = entry.model_dump(mode="python") if hasattr(entry, "model_dump") else dict(entry)
    return data


def _entry_matches(entry: Any, task_spec: TaskSpec, policy: ContextSlicePolicy, filters: Any) -> bool:
    data = _entry_metadata(entry)
    if policy.filter_state_by_task:
        entry_task_id = data.get("task_id")
        task_ids = data.get("task_ids", ())
        if entry_task_id and entry_task_id != task_spec.task_id:
            return False
        if task_ids and task_spec.task_id not in task_ids:
            return False
        entry_scope = data.get("scope", data.get("scopes", ()))
        if entry_scope and task_spec.scope:
            left = {entry_scope} if isinstance(entry_scope, str) else set(entry_scope)
            if not left.intersection(task_spec.scope):
                return False
    if filters is not None:
        return _coerce_filter(filters).matches(data)
    return True


def _select_state_items(items: Sequence[Any], task_spec: TaskSpec, policy: ContextSlicePolicy, filters: Any, name: str) -> list[Any]:
    limit = getattr(policy, f"max_{name}")
    return [item for item in items if _entry_matches(item, task_spec, policy, filters)][:limit]


def _select_artifact_refs(items: Sequence[ArtifactReference], task_spec: TaskSpec, policy: ContextSlicePolicy) -> list[ArtifactReference]:
    task_artifacts = _task_artifact_ids(task_spec)
    values = [
        item
        for item in items
        if (not policy.allowed_artifact_ids or item.artifact_id in policy.allowed_artifact_ids)
        and (not task_artifacts or item.artifact_id in task_artifacts)
    ]
    return values[: policy.max_artifact_refs]


def _select_chunk_refs(items: Sequence[ChunkReference], task_spec: TaskSpec, policy: ContextSlicePolicy) -> list[ChunkReference]:
    values: list[ChunkReference] = []
    for item in items:
        if policy.allowed_chunk_ids and item.chunk_id not in policy.allowed_chunk_ids:
            continue
        if task_spec.read_set and (
            item.source_path is None or not _path_matches_any(item.source_path, task_spec.read_set)
        ):
            continue
        task_artifacts = _task_artifact_ids(task_spec)
        if task_artifacts and item.artifact_id not in task_artifacts:
            continue
        values.append(item)
    return values[: policy.max_chunk_refs] if policy.include_chunk_refs else []


def _chunk_allowed_for_task(chunk: Chunk, task_spec: TaskSpec, policy: ContextSlicePolicy) -> bool:
    if policy.allowed_chunk_ids and chunk.id not in policy.allowed_chunk_ids:
        return False
    if policy.allowed_source_paths and (chunk.source_path is None or not _path_matches_any(chunk.source_path, policy.allowed_source_paths)):
        return False
    chunk_path = chunk.source_path or _metadata_source_path(chunk.metadata)
    if task_spec.read_set and (chunk_path is None or not _path_matches_any(chunk_path, task_spec.read_set)):
        return False
    task_artifacts = _task_artifact_ids(task_spec)
    if task_artifacts and chunk.artifact_id not in task_artifacts and chunk.metadata.get("artifact_id") not in task_artifacts:
        return False
    if task_spec.scope:
        value = chunk.metadata.get("scope", chunk.metadata.get("scopes"))
        if value:
            values = {value} if isinstance(value, str) else set(value)
            if not values.intersection(task_spec.scope):
                return False
    return True


def _task_artifact_ids(task_spec: TaskSpec) -> set[str]:
    value = task_spec.metadata.get("artifact_id", task_spec.metadata.get("artifact_ids"))
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {item for item in value if isinstance(item, str)}
    return set()


def _metadata_source_path(metadata: Mapping[str, Any]) -> str | None:
    value = metadata.get("source_path", metadata.get("source_name", metadata.get("path")))
    return value if isinstance(value, str) else None


def _path_matches_any(path: str, allowed: Sequence[str]) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    for candidate in allowed:
        target = candidate.replace("\\", "/").strip("/")
        if normalized == target or normalized.startswith(target.rstrip("/") + "/"):
            return True
    return False


__all__ = [
    "Chunk",
    "ChunkRecord",
    "DocumentChunk",
    "MetadataFilter",
    "MetadataFilters",
    "SemanticBackend",
    "SemanticMatch",
    "RetrievalError",
    "RetrievalStrategy",
    "RetrievalHit",
    "RetrievalResponse",
    "RetrievalQuery",
    "ChunkRetriever",
    "RetrievalIndex",
    "HybridRetriever",
    "LexicalRetriever",
    "RetrievalResult",
    "SearchResult",
    "ChunkHit",
    "SearchStrategy",
    "ContextSlicePolicy",
    "ContextSlice",
    "ContextSlicer",
    "slice_context",
    "build_context_slice",
    "create_context_slice",
    "slice_context_for_subagent",
]
