"""Local orchestration for ingestion, retrieval and durable session context."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .agent_registry import ContextPolicy
from .artifacts import ArtifactMetadata, ArtifactStore
from .contracts import TaskSpec
from .ingestion import IngestionResult, Ingestor
from .retrieval import (
    Chunk as RetrievalChunk,
    ChunkRetriever,
    ContextSlice,
    ContextSlicePolicy,
    MetadataFilter,
    RetrievalResponse,
    RetrievalStrategy,
    slice_context,
)
from .session_state import SessionNotFoundError, SessionState, SessionStateStore


DEFAULT_SESSION_ID = "current"
DEFAULT_RETRIEVAL_CHUNKS = 6
MAX_PROMPT_CHUNK_CHARS = 6_000
MAX_SESSION_PROMPT_CHARS = 24_000
MAX_SESSION_CHUNK_REFS = 128


@dataclass(frozen=True, slots=True)
class LargeInputContext:
    """Compact provider-facing view of one fully preserved local input."""

    ingestion: IngestionResult
    retrieval: RetrievalResponse

    @property
    def artifact_id(self) -> str:
        return self.ingestion.artifact_id

    def to_prompt(self) -> str:
        chunks = [
            {
                "chunk_id": hit.chunk.id,
                "kind": hit.chunk.metadata.get("kind"),
                "score": hit.score,
                "ordinal": hit.chunk.ordinal,
                "content": hit.chunk.text[:MAX_PROMPT_CHUNK_CHARS],
                "metadata": hit.chunk.metadata,
            }
            for hit in self.retrieval.hits
        ]
        data = {
            "notice": (
                "O input original foi preservado localmente. Esta é apenas uma fatia recuperada; "
                "use read_artifact ou retrieve_context se precisar de outra parte."
            ),
            "artifact_id": self.ingestion.artifact_id,
            "summary": self.ingestion.artifact.summary,
            "size": self.ingestion.size_bytes,
            "preview": self.ingestion.artifact.preview,
            "content_type": self.ingestion.content_type.value,
            "chunk_count": self.ingestion.chunk_count,
            "chunking_strategy": self.ingestion.strategy,
            "retrieval_strategy": self.retrieval.strategy_used,
            "semantic_status": self.retrieval.semantic_status,
            "retrieved_chunks": chunks,
        }
        return "<retrieved_context source=\"large_input\">\n" + json.dumps(
            data, ensure_ascii=False, indent=2
        ) + "\n</retrieved_context>"


def ingest_oversized_user_input(
    user_input: str,
    context_engine: "ContextEngine",
) -> tuple[str, LargeInputContext]:
    """Preserve a large paste and retain an optional goal/plan command prefix."""

    command_prefix = ""
    content = user_input
    lowered = user_input.lower()
    for candidate in ("/plan ", "/goal "):
        if lowered.startswith(candidate):
            command_prefix = user_input[: len(candidate)]
            content = user_input[len(candidate) :]
            break
    result = context_engine.ingest_text(
        content,
        source_name="pasted-user-input.txt",
        query=content[-20_000:],
    )
    return command_prefix + result.to_prompt(), result


class ContextEngine:
    """Compose the Phase 7 context subsystems without provider dependencies."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        artifact_store: ArtifactStore | None = None,
        semantic_backend: Any | None = None,
        session_id: str = DEFAULT_SESSION_ID,
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise NotADirectoryError(f"Workspace is not a directory: {self.workspace}")
        self.artifact_store = artifact_store or ArtifactStore(self.workspace)
        self.ingestor = Ingestor(self.artifact_store)
        self.retriever = ChunkRetriever(semantic_backend=semantic_backend)
        self.session_store = SessionStateStore(self.workspace)
        try:
            self.session_state = self.session_store.load(session_id)
        except SessionNotFoundError:
            self.session_state = SessionState(session_id=session_id)
            self.session_store.save(self.session_state)

    @staticmethod
    def _retrieval_chunk(result: IngestionResult, chunk: Any) -> RetrievalChunk:
        identifier = f"{result.artifact_id}.{chunk.chunk_id}"
        metadata = {
            **dict(chunk.metadata),
            "kind": chunk.kind,
            "source_name": result.source_name,
            "content_type": result.content_type.value,
            "artifact_id": result.artifact_id,
            "start_offset": chunk.start_offset,
            "end_offset": chunk.end_offset,
        }
        return RetrievalChunk(
            id=identifier,
            text=chunk.text,
            metadata=metadata,
            artifact_id=result.artifact_id,
            ordinal=chunk.order,
        )

    def _index(self, result: IngestionResult) -> tuple[RetrievalChunk, ...]:
        indexed: list[RetrievalChunk] = []
        for chunk in result.chunks:
            record = self._retrieval_chunk(result, chunk)
            self.retriever.add(record)
            indexed.append(record)
        return tuple(indexed)

    def _persist_ingestion(self, result: IngestionResult, chunks: tuple[RetrievalChunk, ...]) -> None:
        state = self.session_state.reference_artifact(
            result.artifact_id,
            description=result.artifact.summary,
            checksum_sha256=result.artifact.checksum,
        )
        for chunk in chunks[:MAX_SESSION_CHUNK_REFS]:
            state = state.reference_chunk(
                chunk.id,
                artifact_id=result.artifact_id,
                ordinal=chunk.ordinal,
            )
        state = state.advance_turn(
            summary=(
                f"Input grande preservado em {result.artifact_id}: {result.size_bytes} bytes, "
                f"tipo {result.content_type.value}, {result.chunk_count} chunks."
            )
        )
        self.session_store.save(state)
        self.session_state = state

    def ingest_text(
        self,
        content: str,
        *,
        source_name: str = "pasted-input.txt",
        query: str | None = None,
        top_k: int = DEFAULT_RETRIEVAL_CHUNKS,
    ) -> LargeInputContext:
        result = self.ingestor.ingest(content, filename=source_name, source_name=source_name)
        chunks = self._index(result)
        self._persist_ingestion(result, chunks)
        lookup = (query or content[-20_000:] or source_name)[-20_000:]
        retrieval = self.retriever.search(
            lookup,
            top_k=top_k,
            filters=MetadataFilter(equals={"artifact_id": result.artifact_id}),
            strategy=RetrievalStrategy.HYBRID,
        )
        return LargeInputContext(result, retrieval)

    def ingest_file(
        self,
        path: Path | str,
        *,
        query: str | None = None,
        top_k: int = DEFAULT_RETRIEVAL_CHUNKS,
    ) -> LargeInputContext:
        candidate = Path(path)
        result = self.ingestor.ingest_file(candidate, source_name=candidate.name)
        chunks = self._index(result)
        self._persist_ingestion(result, chunks)
        lookup = (query or candidate.name)[-20_000:]
        retrieval = self.retriever.search(
            lookup,
            top_k=top_k,
            filters=MetadataFilter(equals={"artifact_id": result.artifact_id}),
            strategy=RetrievalStrategy.HYBRID,
        )
        return LargeInputContext(result, retrieval)

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_RETRIEVAL_CHUNKS,
        artifact_id: str | None = None,
        strategy: RetrievalStrategy | str = RetrievalStrategy.HYBRID,
    ) -> RetrievalResponse:
        filters = MetadataFilter(equals={"artifact_id": artifact_id}) if artifact_id else None
        return self.retriever.search(query, top_k=top_k, filters=filters, strategy=strategy)

    def context_slice(
        self,
        task_spec: TaskSpec,
        *,
        policy: ContextPolicy | ContextSlicePolicy | None = None,
    ) -> ContextSlice:
        return slice_context(
            task_spec,
            self.session_state,
            policy=policy,
            retriever=self.retriever,
        )

    def record_usage(self, usage: Mapping[str, Any]) -> None:
        state = self.session_state.record_usage({"requests": 1, **dict(usage)})
        self.session_store.save(state)
        self.session_state = state

    def session_prompt(self, *, max_chars: int = MAX_SESSION_PROMPT_CHARS) -> str:
        state = self.session_state.compact(max_items=20)
        data = state.model_dump(mode="json", exclude_none=True)
        for collection in ("facts", "decisions", "objectives"):
            for item in data.get(collection, []):
                if "content" in item:
                    item["content"] = str(item["content"])[:1_000]
                if "rationale" in item:
                    item["rationale"] = str(item["rationale"])[:1_000]
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        while len(encoded) > max_chars:
            candidates = [
                name
                for name in ("chunk_refs", "artifact_refs", "facts", "decisions", "objectives")
                if data.get(name)
            ]
            if not candidates:
                break
            data[candidates[0]].pop(0)
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > max_chars:
            encoded = json.dumps(
                {
                    "schema_version": data["schema_version"],
                    "session_id": data["session_id"],
                    "usage": data.get("usage", {}),
                    "current_turn": data.get("current_turn"),
                    "truncated": True,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return "<session_state>\n" + encoded + "\n</session_state>"

    def status(self) -> dict[str, Any]:
        return {
            "session_id": self.session_state.session_id,
            "indexed_chunks": len(self.retriever.chunks),
            "artifacts": len(self.session_state.artifact_refs),
            "chunk_references": len(self.session_state.chunk_refs),
            "usage": self.session_state.usage.model_dump(mode="json"),
            "semantic_backend": self.retriever.semantic_backend is not None,
        }

    def list_artifacts(self, *, limit: int = 50) -> tuple[ArtifactMetadata, ...]:
        return self.artifact_store.list(limit=limit)


__all__ = [
    "ContextEngine",
    "DEFAULT_RETRIEVAL_CHUNKS",
    "DEFAULT_SESSION_ID",
    "LargeInputContext",
]
