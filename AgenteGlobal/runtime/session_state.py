"""Typed, durable, and bounded session state for the local runtime.

Session state is a structured continuity layer.  It is deliberately separate
from hot conversation history and from the Core, so context compaction can
reload facts, decisions, objectives, and traceable references without
silently restoring an entire prompt.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Mapping, Sequence

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


SESSION_SCHEMA_VERSION = "1.0"
DEFAULT_SESSION_DIRECTORY = Path(".agenteglobal") / "session"
DEFAULT_MAX_STATE_BYTES = 1 * 1024 * 1024
MAX_STATE_BYTES = DEFAULT_MAX_STATE_BYTES
MAX_SESSION_ITEMS = 1_000
MAX_RECORD_TEXT_CHARS = 20_000
MAX_METADATA_BYTES = 256 * 1024
MAX_ID_LENGTH = 256

# The same namespace is used in filenames on Windows and POSIX.  In
# particular, a colon is rejected even though it is not a separator on POSIX.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")


class SessionStateError(RuntimeError):
    """Base error for safe local session persistence."""


class SessionNotFoundError(SessionStateError):
    """The requested session does not exist."""


class SessionIntegrityError(SessionStateError):
    """The session file is malformed, too large, or outside its boundary."""


class SessionConflictError(SessionStateError):
    """A path required for session state is a symlink or incompatible file."""


class StateModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=False,
        validate_assignment=True,
        populate_by_name=True,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _valid_id(value: str, *, name: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None or value.startswith("."):
        raise ValueError(f"{name} must be a safe opaque identifier without path separators.")
    return value


def _safe_relative_path(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty relative path.")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized) or any(part == ".." for part in normalized.split("/")):
        raise ValueError(f"{name} must be relative and cannot contain '..'.")
    return normalized


def _finite_number(value: float, *, name: str) -> float:
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be finite.")
    return value


def _validate_metadata(value: dict[str, Any]) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("metadata must be JSON serializable.") from error
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("metadata exceeds the bounded size limit.")


class SessionFact(StateModel):
    """A durable fact retained independently from chat transcript text."""

    fact_id: str = Field(validation_alias=AliasChoices("fact_id", "id"), min_length=1, max_length=MAX_ID_LENGTH)
    content: str = Field(validation_alias=AliasChoices("content", "text", "value"), min_length=1, max_length=MAX_RECORD_TEXT_CHARS)
    source: str | None = Field(default=None, max_length=4_096)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    task_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    scope: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("fact_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _valid_id(value, name="fact_id")

    @field_validator("task_id")
    @classmethod
    def _task_id(cls, value: str | None) -> str | None:
        return None if value is None else _valid_id(value, name="task_id")

    @field_validator("tags", "scope", mode="before")
    @classmethod
    def _sequence(cls, value: Any) -> Any:
        if isinstance(value, (tuple, list)):
            return list(value)
        return value

    @field_validator("tags", "scope")
    @classmethod
    def _items(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 4_000 for item in value):
            raise ValueError("tags/scope entries must be non-empty and bounded.")
        if len(set(value)) != len(value):
            raise ValueError("tags/scope entries must be unique.")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def _timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc)

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_metadata(value)
        return value

    @property
    def id(self) -> str:
        return self.fact_id


class SessionDecision(StateModel):
    """A durable decision and optional rationale/evidence."""

    decision_id: str = Field(validation_alias=AliasChoices("decision_id", "id"), min_length=1, max_length=MAX_ID_LENGTH)
    content: str = Field(validation_alias=AliasChoices("content", "text", "decision"), min_length=1, max_length=MAX_RECORD_TEXT_CHARS)
    rationale: str | None = Field(default=None, max_length=MAX_RECORD_TEXT_CHARS)
    source: str | None = Field(default=None, max_length=4_096)
    task_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    scope: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("decision_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _valid_id(value, name="decision_id")

    @field_validator("task_id")
    @classmethod
    def _task_id(cls, value: str | None) -> str | None:
        return None if value is None else _valid_id(value, name="task_id")

    @field_validator("scope", mode="before")
    @classmethod
    def _sequence_before(cls, value: Any) -> Any:
        if isinstance(value, (tuple, list)):
            return list(value)
        return value

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 4_000 for item in value) or len(set(value)) != len(value):
            raise ValueError("scope entries must be non-empty, bounded, and unique.")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def _timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc)

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_metadata(value)
        return value

    @property
    def id(self) -> str:
        return self.decision_id


class SessionObjective(StateModel):
    """A durable objective, optionally associated with one task and scope."""

    objective_id: str = Field(validation_alias=AliasChoices("objective_id", "id"), min_length=1, max_length=MAX_ID_LENGTH)
    content: str = Field(validation_alias=AliasChoices("content", "text", "description", "objective"), min_length=1, max_length=MAX_RECORD_TEXT_CHARS)
    status: str = Field(default="active", min_length=1, max_length=64)
    priority: int = Field(default=0, ge=-100, le=100)
    task_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    scope: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("objective_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _valid_id(value, name="objective_id")

    @field_validator("task_id")
    @classmethod
    def _task_id(cls, value: str | None) -> str | None:
        return None if value is None else _valid_id(value, name="task_id")

    @field_validator("scope", mode="before")
    @classmethod
    def _sequence_before(cls, value: Any) -> Any:
        if isinstance(value, (tuple, list)):
            return list(value)
        return value

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 4_000 for item in value) or len(set(value)) != len(value):
            raise ValueError("scope entries must be non-empty, bounded, and unique.")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def _timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc)

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_metadata(value)
        return value

    @property
    def id(self) -> str:
        return self.objective_id


class ArtifactReference(StateModel):
    """Traceable reference to an Artifact Store item; payload is not embedded."""

    artifact_id: str = Field(validation_alias=AliasChoices("artifact_id", "id"), min_length=1, max_length=MAX_ID_LENGTH)
    uri: str | None = Field(default=None, max_length=4_096)
    path: str | None = Field(default=None, max_length=4_096)
    description: str | None = Field(default=None, max_length=2_000)
    checksum_sha256: str | None = Field(default=None, pattern=r"^[A-Fa-f0-9]{64}$")

    @field_validator("artifact_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _valid_id(value, name="artifact_id")

    @field_validator("path")
    @classmethod
    def _path(cls, value: str | None) -> str | None:
        return None if value is None else _safe_relative_path(value, name="path")

    @model_validator(mode="after")
    def _reference(self) -> "ArtifactReference":
        if self.uri is None and self.path is None:
            # An artifact ID alone is valid and portable; storage adapters
            # resolve it under their own already-confined ArtifactStore root.
            return self
        return self

    @property
    def id(self) -> str:
        return self.artifact_id


class ChunkReference(StateModel):
    """Traceable chunk reference without forcing source content into state."""

    chunk_id: str = Field(validation_alias=AliasChoices("chunk_id", "id"), min_length=1, max_length=MAX_ID_LENGTH)
    artifact_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH)
    source_path: str | None = Field(default=None, max_length=4_096)
    ordinal: int = Field(default=0, ge=0, le=10_000_000)
    checksum_sha256: str | None = Field(default=None, pattern=r"^[A-Fa-f0-9]{64}$")

    @field_validator("chunk_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _valid_id(value, name="chunk_id")

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id(cls, value: str | None) -> str | None:
        return None if value is None else _valid_id(value, name="artifact_id")

    @field_validator("source_path")
    @classmethod
    def _path(cls, value: str | None) -> str | None:
        return None if value is None else _safe_relative_path(value, name="source_path")

    @property
    def id(self) -> str:
        return self.chunk_id


class UsageMetrics(StateModel):
    """Bounded usage counters retained for cost/context continuity."""

    requests: int = Field(default=0, ge=0, le=10_000_000)
    input_tokens: int = Field(
        default=0,
        validation_alias=AliasChoices("input_tokens", "prompt_tokens"),
        ge=0,
        le=10_000_000_000,
    )
    output_tokens: int = Field(
        default=0,
        validation_alias=AliasChoices("output_tokens", "completion_tokens"),
        ge=0,
        le=10_000_000_000,
    )
    total_tokens: int = Field(default=0, ge=0, le=10_000_000_000)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0, le=1_000_000.0)
    actual_cost_usd: float | None = Field(default=None, ge=0.0, le=1_000_000.0)

    @field_validator("estimated_cost_usd", "actual_cost_usd")
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        return None if value is None else _finite_number(value, name="cost")

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens

    @property
    def completion_tokens(self) -> int:
        return self.output_tokens


class TurnState(StateModel):
    """Current turn marker and optional compact summary."""

    turn_id: str = Field(default="turn-0", validation_alias=AliasChoices("turn_id", "id"), min_length=1, max_length=MAX_ID_LENGTH)
    turn_number: int = Field(default=0, ge=0, le=10_000_000)
    status: str = Field(default="active", min_length=1, max_length=64)
    summary: str | None = Field(default=None, max_length=MAX_RECORD_TEXT_CHARS)
    started_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("turn_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _valid_id(value, name="turn_id")

    @field_validator("started_at", "updated_at")
    @classmethod
    def _timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware.")
        return value.astimezone(timezone.utc)

    @property
    def id(self) -> str:
        return self.turn_id


class SessionState(StateModel):
    """Versioned structured state that can be safely persisted and reloaded."""

    schema_version: str = SESSION_SCHEMA_VERSION
    session_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    facts: list[SessionFact] = Field(default_factory=list, max_length=MAX_SESSION_ITEMS)
    decisions: list[SessionDecision] = Field(default_factory=list, max_length=MAX_SESSION_ITEMS)
    objectives: list[SessionObjective] = Field(default_factory=list, max_length=MAX_SESSION_ITEMS)
    artifact_refs: list[ArtifactReference] = Field(
        default_factory=list,
        validation_alias=AliasChoices("artifact_refs", "artifacts", "artifact_references"),
        max_length=MAX_SESSION_ITEMS,
    )
    chunk_refs: list[ChunkReference] = Field(
        default_factory=list,
        validation_alias=AliasChoices("chunk_refs", "chunks", "chunk_references"),
        max_length=MAX_SESSION_ITEMS,
    )
    usage: UsageMetrics = Field(default_factory=UsageMetrics)
    current_turn: TurnState | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=_now)

    _MAX_ITEMS: ClassVar[int] = MAX_SESSION_ITEMS

    @field_validator("session_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _valid_id(value, name="session_id")

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: str) -> str:
        if value != SESSION_SCHEMA_VERSION:
            raise ValueError(f"Unsupported session schema version: {value!r}")
        return value

    @field_validator("facts", "decisions", "objectives", mode="before")
    @classmethod
    def _coerce_text_records(cls, value: Any, info: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{info.field_name} must be a list.")
        field_name = info.field_name[:-1] if info.field_name.endswith("s") else info.field_name
        return [
            {"id": f"{field_name}-{index + 1}", "content": item} if isinstance(item, str) else item
            for index, item in enumerate(value)
        ]

    @field_validator("current_turn", mode="before")
    @classmethod
    def _coerce_turn_number(cls, value: Any) -> Any:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return {"turn_id": f"turn-{value}", "turn_number": value}
        return value

    @field_validator("updated_at")
    @classmethod
    def _timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware.")
        return value.astimezone(timezone.utc)

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_metadata(value)
        return value

    @model_validator(mode="after")
    def _unique_ids(self) -> "SessionState":
        for name, entries, id_name in (
            ("facts", self.facts, "fact_id"),
            ("decisions", self.decisions, "decision_id"),
            ("objectives", self.objectives, "objective_id"),
            ("artifact_refs", self.artifact_refs, "artifact_id"),
            ("chunk_refs", self.chunk_refs, "chunk_id"),
        ):
            ids = [getattr(entry, id_name) for entry in entries]
            if len(set(ids)) != len(ids):
                raise ValueError(f"{name} identifiers must be unique.")
        return self

    def compact(self, *, max_items: int | None = None) -> "SessionState":
        """Return a bounded copy suitable for reload after hot-context compaction.

        Without a limit, compaction is intentionally lossless for structured
        state.  With a limit, oldest records are removed per collection while
        references, usage, and the current turn remain typed and durable.
        """

        if max_items is None:
            return self.model_copy(deep=True)
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 0 <= max_items <= MAX_SESSION_ITEMS:
            raise ValueError(f"max_items must be between 0 and {MAX_SESSION_ITEMS}.")
        updates: dict[str, Any] = {}
        for field_name in ("facts", "decisions", "objectives", "artifact_refs", "chunk_refs"):
            values = list(getattr(self, field_name))
            updates[field_name] = values[-max_items:] if max_items else []
        return self.model_copy(update=updates, deep=True)

    def add_fact(self, content: str, *, fact_id: str | None = None, **kwargs: Any) -> "SessionState":
        identifier = fact_id or f"fact-{uuid.uuid4().hex}"
        return self.model_copy(update={"facts": [*self.facts, SessionFact(fact_id=identifier, content=content, **kwargs)]}, deep=True)

    record_fact = add_fact

    def add_decision(self, content: str, *, decision_id: str | None = None, **kwargs: Any) -> "SessionState":
        identifier = decision_id or f"decision-{uuid.uuid4().hex}"
        return self.model_copy(update={"decisions": [*self.decisions, SessionDecision(decision_id=identifier, content=content, **kwargs)]}, deep=True)

    record_decision = add_decision

    def add_objective(self, content: str, *, objective_id: str | None = None, **kwargs: Any) -> "SessionState":
        identifier = objective_id or f"objective-{uuid.uuid4().hex}"
        return self.model_copy(update={"objectives": [*self.objectives, SessionObjective(objective_id=identifier, content=content, **kwargs)]}, deep=True)

    record_objective = add_objective

    def reference_artifact(self, artifact_id: str, **kwargs: Any) -> "SessionState":
        reference = ArtifactReference(artifact_id=artifact_id, **kwargs)
        return self.model_copy(update={"artifact_refs": [*self.artifact_refs, reference]}, deep=True)

    add_artifact_ref = reference_artifact

    def reference_chunk(self, chunk_id: str, **kwargs: Any) -> "SessionState":
        reference = ChunkReference(chunk_id=chunk_id, **kwargs)
        return self.model_copy(update={"chunk_refs": [*self.chunk_refs, reference]}, deep=True)

    add_chunk_ref = reference_chunk

    def advance_turn(self, *, turn_id: str | None = None, summary: str | None = None, status: str = "active") -> "SessionState":
        number = (self.current_turn.turn_number + 1) if self.current_turn else 1
        turn = TurnState(
            turn_id=turn_id or f"turn-{number}",
            turn_number=number,
            status=status,
            summary=summary,
        )
        return self.model_copy(update={"current_turn": turn, "updated_at": _now()}, deep=True)

    begin_turn = advance_turn

    def record_usage(self, usage: Mapping[str, Any]) -> "SessionState":
        if not isinstance(usage, Mapping):
            raise ValueError("usage must be a mapping.")
        current = self.usage
        values = {
            "requests": current.requests + _usage_int(usage, "requests"),
            "input_tokens": current.input_tokens + _usage_int(usage, "input_tokens", "prompt_tokens"),
            "output_tokens": current.output_tokens + _usage_int(usage, "output_tokens", "completion_tokens"),
            "total_tokens": current.total_tokens + _usage_int(usage, "total_tokens", "tokens"),
            "estimated_cost_usd": current.estimated_cost_usd + _usage_float(usage, "estimated_cost_usd"),
            "actual_cost_usd": None,
        }
        if current.actual_cost_usd is not None or "actual_cost_usd" in usage:
            values["actual_cost_usd"] = (current.actual_cost_usd or 0.0) + _usage_float(usage, "actual_cost_usd")
        return self.model_copy(update={"usage": UsageMetrics(**values), "updated_at": _now()}, deep=True)

    @property
    def artifact_references(self) -> list[ArtifactReference]:
        return self.artifact_refs

    @property
    def chunk_references(self) -> list[ChunkReference]:
        return self.chunk_refs

    @property
    def turn(self) -> TurnState | None:
        return self.current_turn

    def save(self, root: Path | str, *, max_state_bytes: int = DEFAULT_MAX_STATE_BYTES) -> Path:
        if isinstance(root, SessionStateStore):
            return root.save(self)
        return SessionStateStore(root, max_state_bytes=max_state_bytes).save(self)

    persist = save

    @classmethod
    def load(cls, root: Path | str, session_id: str, *, max_state_bytes: int = DEFAULT_MAX_STATE_BYTES) -> "SessionState":
        if isinstance(root, SessionStateStore):
            return root.load(session_id)
        return SessionStateStore(root, max_state_bytes=max_state_bytes).load(session_id)


class SessionStateStore:
    """Atomic JSON store confined beneath ``.agenteglobal/session``."""

    def __init__(
        self,
        root: Path | str,
        *,
        directory: Path | str = DEFAULT_SESSION_DIRECTORY,
        max_state_bytes: int = DEFAULT_MAX_STATE_BYTES,
    ) -> None:
        if isinstance(max_state_bytes, bool) or not isinstance(max_state_bytes, int) or max_state_bytes < 1:
            raise ValueError("max_state_bytes must be a positive integer.")
        original_root = Path(root)
        if original_root.is_symlink():
            raise SessionConflictError("Session root cannot be a symlink.")
        try:
            resolved_root = original_root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise SessionStateError("Session root must be an existing directory.") from error
        if not resolved_root.is_dir():
            raise SessionStateError("Session root must be an existing directory.")
        relative = Path(directory)
        if relative.is_absolute() or relative.drive or ".." in relative.parts or not relative.parts:
            raise ValueError("Session directory must be a non-empty relative path.")
        self._root = resolved_root
        self._relative_directory = relative
        self._max_state_bytes = max_state_bytes
        self._directory = self._ensure_directory()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def session_directory(self) -> Path:
        return self._directory

    @property
    def max_state_bytes(self) -> int:
        return self._max_state_bytes

    def _ensure_directory(self) -> Path:
        current = self._root
        for part in self._relative_directory.parts:
            if part in ("", "."):
                continue
            candidate = current / part
            if candidate.exists() or candidate.is_symlink():
                if candidate.is_symlink() or not candidate.is_dir():
                    raise SessionConflictError("Session directory cannot contain symlinks or regular files.")
            else:
                try:
                    candidate.mkdir()
                except OSError as error:
                    raise SessionStateError("Cannot create the session directory.") from error
            current = candidate
        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(self._root)
        except (OSError, RuntimeError, ValueError) as error:
            raise SessionConflictError("Session directory resolves outside the explicit root.") from error
        return resolved

    def path_for(self, session_id: str) -> Path:
        _valid_id(session_id, name="session_id")
        candidate = self._directory / f"{session_id}.json"
        if candidate.parent != self._directory:
            raise SessionConflictError("Session path escaped its directory.")
        return candidate

    def exists(self, session_id: str) -> bool:
        path = self.path_for(session_id)
        return path.is_file() and not path.is_symlink()

    def save(self, state: SessionState) -> Path:
        if not isinstance(state, SessionState):
            state = SessionState.model_validate(state)
        else:
            # Pydantic validates assignment, but a caller can still mutate a
            # nested list in place.  Revalidate at the persistence boundary so
            # duplicate IDs or injected invalid records never reach disk.
            state = SessionState.model_validate(state.model_dump(mode="python"))
        self._validate_reference_paths(state)
        path = self.path_for(state.session_id)
        if path.is_symlink():
            raise SessionConflictError("Refusing to overwrite a symlinked session file.")
        payload = _encode_state(state)
        if len(payload) > self._max_state_bytes:
            raise SessionStateError("Session state exceeds the configured size limit.")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{state.session_id}.",
                suffix=".tmp",
                dir=self._directory,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            # os.replace is atomic on the same filesystem.  The target was
            # checked above; re-check immediately before replacement as a
            # defense against an externally introduced symlink.
            if path.is_symlink():
                raise SessionConflictError("Refusing to replace a symlinked session file.")
            os.replace(temporary_path, path)
            temporary_path = None
            _fsync_directory(self._directory)
            return path
        except SessionStateError:
            raise
        except OSError as error:
            raise SessionStateError("Atomic session state save failed.") from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    persist = save
    write = save

    def load(self, session_id: str) -> SessionState:
        path = self.path_for(session_id)
        if path.is_symlink():
            raise SessionConflictError("Refusing to load a symlinked session file.")
        if not path.exists() or not path.is_file():
            raise SessionNotFoundError(f"Session not found: {session_id}")
        try:
            size = path.stat().st_size
        except OSError as error:
            raise SessionIntegrityError("Cannot inspect session state file.") from error
        if size > self._max_state_bytes:
            raise SessionIntegrityError("Session state file exceeds the configured size limit.")
        try:
            payload = path.read_bytes()
            if len(payload) > self._max_state_bytes:
                raise SessionIntegrityError("Session state file exceeds the configured size limit.")
            # ``model_validate`` is intentionally strict for in-memory
            # callers; JSON mode still parses its standards-compliant datetime
            # strings while applying the same schema constraints.
            json.loads(payload.decode("utf-8"))
            state = SessionState.model_validate_json(payload, strict=False)
        except SessionStateError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise SessionIntegrityError("Session state file is invalid JSON or violates its typed contract.") from error
        if state.session_id != session_id:
            raise SessionIntegrityError("Session file name does not match its session_id.")
        self._validate_reference_paths(state)
        return state

    read = load
    load_state = load
    save_state = save

    def _validate_reference_paths(self, state: SessionState) -> None:
        """Reject references traversing a symlink component under the root.

        References may legitimately point to content that is created later,
        so non-existent final components are allowed.  Existing components
        are nevertheless checked one by one, rather than relying only on a
        final ``resolve`` call that could follow a symlink.
        """

        relative_paths = [
            reference.path
            for reference in state.artifact_refs
            if reference.path is not None
        ] + [
            reference.source_path
            for reference in state.chunk_refs
            if reference.source_path is not None
        ]
        for relative in relative_paths:
            if relative is None:
                continue
            candidate = self._root.joinpath(*relative.replace("\\", "/").split("/"))
            try:
                candidate.relative_to(self._root)
            except ValueError as error:
                raise SessionConflictError("A session reference path escaped its root.") from error
            current = self._root
            for part in candidate.relative_to(self._root).parts:
                current = current / part
                if current.is_symlink():
                    raise SessionConflictError("Session reference paths cannot contain symlinks.")

    def list_ids(self) -> tuple[str, ...]:
        values: list[str] = []
        try:
            entries = tuple(self._directory.iterdir())
        except OSError as error:
            raise SessionStateError("Cannot list session state directory.") from error
        for entry in entries:
            if entry.is_symlink() or not entry.is_file() or entry.suffix != ".json":
                continue
            session_id = entry.stem
            try:
                _valid_id(session_id, name="session_id")
            except ValueError:
                continue
            values.append(session_id)
        return tuple(sorted(values))


PersistentSessionStore = SessionStateStore
SessionStore = SessionStateStore
Fact = SessionFact
Decision = SessionDecision
Objective = SessionObjective
ArtifactRef = ArtifactReference
ChunkRef = ChunkReference
SessionUsage = UsageMetrics
CurrentTurn = TurnState
SessionContext = SessionState


def _encode_state(state: SessionState) -> bytes:
    try:
        data = state.model_dump(mode="json")
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SessionStateError("Session state contains a non-serializable value.") from error


def _usage_int(usage: Mapping[str, Any], *names: str) -> int:
    for name in names:
        if name in usage:
            value = usage[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"usage.{name} must be a non-negative integer.")
            return value
    return 0


def _usage_float(usage: Mapping[str, Any], name: str) -> float:
    value = usage.get(name, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"usage.{name} must be a non-negative number.")
    return _finite_number(float(value), name=f"usage.{name}")


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory durability across POSIX and Windows."""

    try:
        descriptor = os.open(str(directory), getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def slice_context(*args: Any, **kwargs: Any) -> Any:
    """Lazy compatibility wrapper; implementation lives in ``retrieval``.

    Keeping the import lazy avoids a module cycle while allowing callers to
    discover the context-slice API from either runtime module.
    """

    from .retrieval import slice_context as _slice_context

    return _slice_context(*args, **kwargs)


__all__ = [
    "SESSION_SCHEMA_VERSION",
    "DEFAULT_SESSION_DIRECTORY",
    "DEFAULT_MAX_STATE_BYTES",
    "MAX_STATE_BYTES",
    "SessionStateError",
    "SessionNotFoundError",
    "SessionIntegrityError",
    "SessionConflictError",
    "SessionFact",
    "SessionDecision",
    "SessionObjective",
    "ArtifactReference",
    "ChunkReference",
    "UsageMetrics",
    "TurnState",
    "SessionState",
    "SessionStateStore",
    "PersistentSessionStore",
    "SessionStore",
    "Fact",
    "Decision",
    "Objective",
    "ArtifactRef",
    "ChunkRef",
    "SessionUsage",
    "CurrentTurn",
    "SessionContext",
    "slice_context",
]
