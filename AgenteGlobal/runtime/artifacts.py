"""Boundary-confined local storage for large runtime artifacts.

The store is intentionally small and synchronous.  It owns durable payloads
and their metadata, but it does not ingest, chunk, index, or retrieve semantic
context.  Those concerns belong to later runtime phases.

Each artifact is published below an explicit root as::

    <root>/<directory>/<artifact-id>/payload.bin
    <root>/<directory>/<artifact-id>/metadata.json
    <root>/<directory>/<artifact-id>/.complete

The completion marker is written last.  This means a reader/listener never
considers a directory that was only partially written to be an artifact.  All
files are first written to a same-directory temporary file and then atomically
renamed into place.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


ARTIFACT_SCHEMA_VERSION = "1.0"
DEFAULT_ARTIFACT_DIRECTORY = Path(".agenteglobal") / "artifacts"

# These are intentionally bounded defaults.  The store is for outputs that
# would otherwise exceed the prompt/result limit, not an unbounded file sink.
MAX_ARTIFACT_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_METADATA_BYTES = 256 * 1024
MAX_ARTIFACT_ID_LENGTH = 137
MAX_ARTIFACT_PREVIEW_CHARS = 4_000
MAX_ARTIFACT_SUMMARY_CHARS = 4_000
MAX_ARTIFACT_MEDIA_TYPE_CHARS = 255
MAX_ARTIFACT_LIST_ENTRIES = 1_000
MAX_ARTIFACT_READ_BYTES = MAX_ARTIFACT_PAYLOAD_BYTES

# IDs are generated from random UUID-like material, while the validator also
# accepts a caller-provided opaque token in the same safe namespace.  No path
# separator, drive marker, colon, or dot-prefix is accepted.
_ARTIFACT_ID_RE = re.compile(r"^artifact-[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class ArtifactStoreError(RuntimeError):
    """Base error for local artifact storage failures."""


class ArtifactNotFoundError(ArtifactStoreError):
    """The requested artifact does not exist or is not complete."""


class ArtifactConflictError(ArtifactStoreError):
    """An artifact ID already exists and cannot be overwritten."""


class ArtifactIntegrityError(ArtifactStoreError):
    """A payload or metadata file does not match its recorded integrity."""


class ArtifactMetadata(BaseModel):
    """Versioned metadata persisted next to one artifact payload."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=False,
        validate_assignment=True,
    )

    schema_version: Literal["1.0"] = ARTIFACT_SCHEMA_VERSION
    artifact_id: str = Field(min_length=1, max_length=MAX_ARTIFACT_ID_LENGTH)
    summary: str = Field(default="", max_length=MAX_ARTIFACT_SUMMARY_CHARS)
    size: int = Field(ge=0)
    preview: str = Field(default="", max_length=MAX_ARTIFACT_PREVIEW_CHARS)
    media_type: str = Field(default="application/octet-stream", max_length=MAX_ARTIFACT_MEDIA_TYPE_CHARS)
    metadata: dict[str, Any] = Field(default_factory=dict)
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @field_validator("artifact_id")
    @classmethod
    def _valid_artifact_id(cls, value: str) -> str:
        if _ARTIFACT_ID_RE.fullmatch(value) is None:
            raise ValueError("artifact_id must be an opaque artifact-* token.")
        return value

    @field_validator("media_type")
    @classmethod
    def _valid_media_type(cls, value: str) -> str:
        if not value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise ValueError("media_type must be a non-empty value without control characters.")
        return value

    @field_validator("created_at")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware.")
        return value.astimezone(timezone.utc)


# A short alias is useful to callers that prefer the domain term ``Metadata``
# without creating a second schema.
ArtifactRecord = ArtifactMetadata


def validate_artifact_id(artifact_id: str) -> str:
    """Validate and return an artifact ID without normalizing it."""

    if not isinstance(artifact_id, str) or _ARTIFACT_ID_RE.fullmatch(artifact_id) is None:
        raise ValueError("artifact_id must be an opaque artifact-* token.")
    return artifact_id


class ArtifactStore:
    """Persist and read artifacts confined beneath an explicit local root.

    ``read`` uses byte offsets and byte limits so binary artifacts remain
    lossless.  Use ``read_text`` for a strict UTF-8 text view.  The store never
    follows a symlink supplied through its directory, artifact ID, metadata,
    payload, or completion marker.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        directory: Path | str = DEFAULT_ARTIFACT_DIRECTORY,
        max_payload_bytes: int = MAX_ARTIFACT_PAYLOAD_BYTES,
        max_metadata_bytes: int = MAX_ARTIFACT_METADATA_BYTES,
        max_preview_chars: int = MAX_ARTIFACT_PREVIEW_CHARS,
        max_summary_chars: int = MAX_ARTIFACT_SUMMARY_CHARS,
        max_read_bytes: int = MAX_ARTIFACT_READ_BYTES,
        max_list_entries: int = MAX_ARTIFACT_LIST_ENTRIES,
    ) -> None:
        self._validate_limit("max_payload_bytes", max_payload_bytes)
        self._validate_limit("max_metadata_bytes", max_metadata_bytes)
        self._validate_limit("max_preview_chars", max_preview_chars)
        self._validate_limit("max_summary_chars", max_summary_chars)
        self._validate_limit("max_read_bytes", max_read_bytes)
        self._validate_limit("max_list_entries", max_list_entries)
        # A smaller payload ceiling also bounds a default full-payload read.
        # Callers that explicitly need a different relationship can still set
        # max_read_bytes below the payload ceiling; never allow it above.
        max_read_bytes = min(max_read_bytes, max_payload_bytes)
        if max_preview_chars > MAX_ARTIFACT_PREVIEW_CHARS:
            raise ValueError(f"max_preview_chars cannot exceed {MAX_ARTIFACT_PREVIEW_CHARS}.")
        if max_summary_chars > MAX_ARTIFACT_SUMMARY_CHARS:
            raise ValueError(f"max_summary_chars cannot exceed {MAX_ARTIFACT_SUMMARY_CHARS}.")

        try:
            resolved_root = Path(root).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ArtifactStoreError(f"Cannot resolve artifact root: {root}") from error
        if not resolved_root.is_dir():
            raise ArtifactStoreError("Artifact root must be an existing directory.")

        relative_directory = self._validate_relative_directory(directory)
        self._root = resolved_root
        self._directory_relative = relative_directory
        self._max_payload_bytes = max_payload_bytes
        self._max_metadata_bytes = max_metadata_bytes
        self._max_preview_chars = max_preview_chars
        self._max_summary_chars = max_summary_chars
        self._max_read_bytes = max_read_bytes
        self._max_list_entries = max_list_entries
        self._artifacts_directory = self._ensure_directory()

    @staticmethod
    def _validate_limit(name: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer.")

    @staticmethod
    def _validate_relative_directory(directory: Path | str) -> Path:
        relative = Path(directory)
        if (
            relative.is_absolute()
            or relative.drive
            or ".." in relative.parts
            or any(":" in part for part in relative.parts)
            or not relative.parts
        ):
            raise ValueError("Artifact directory must be a non-empty relative path inside root.")
        return relative

    @property
    def root(self) -> Path:
        """Resolved explicit root used by this store."""

        return self._root

    @property
    def artifacts_directory(self) -> Path:
        """Resolved directory containing artifact IDs."""

        return self._artifacts_directory

    @property
    def directory(self) -> Path:
        """Compatibility alias for :attr:`artifacts_directory`."""

        return self._artifacts_directory

    def _ensure_directory(self) -> Path:
        current = self._root
        for part in self._directory_relative.parts:
            if part in ("", "."):
                continue
            candidate = current / part
            if candidate.exists() or candidate.is_symlink():
                if candidate.is_symlink() or not candidate.is_dir():
                    raise ArtifactStoreError("Artifact directory cannot contain symlinks or regular files.")
            else:
                try:
                    candidate.mkdir()
                except OSError as error:
                    raise ArtifactStoreError(f"Cannot create artifact directory: {candidate.name}") from error
            current = candidate
        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(self._root)
        except (OSError, RuntimeError, ValueError) as error:
            raise ArtifactStoreError("Artifact directory resolves outside the explicit root.") from error
        return resolved

    @staticmethod
    def _new_artifact_id() -> str:
        # token_hex is opaque and does not embed task names, paths, or
        # timestamps.  Restricting the alphabet to the validator's namespace
        # also keeps IDs safe on Windows and POSIX filesystems.
        return f"artifact-{secrets.token_hex(16)}"

    @staticmethod
    def _payload_bytes(payload: bytes | bytearray | memoryview | str) -> bytes:
        if isinstance(payload, str):
            return payload.encode("utf-8")
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, (bytearray, memoryview)):
            return bytes(payload)
        raise TypeError("payload must be str, bytes, bytearray, or memoryview.")

    def _artifact_directory(self, artifact_id: str, *, require_complete: bool = False) -> Path:
        identifier = validate_artifact_id(artifact_id)
        target = self._artifacts_directory / identifier
        if target.parent != self._artifacts_directory:
            raise ArtifactStoreError("Artifact path escapes the artifact directory.")
        if target.is_symlink():
            raise ArtifactStoreError("Artifact directories cannot be symlinks.")
        try:
            resolved = target.resolve(strict=False)
            resolved.relative_to(self._artifacts_directory)
        except (OSError, RuntimeError, ValueError) as error:
            raise ArtifactStoreError("Artifact path resolves outside the artifact directory.") from error
        if require_complete and not target.exists():
            raise ArtifactNotFoundError(f"Artifact not found: {identifier}")
        return target

    @staticmethod
    def _path_is_safe_file(path: Path, *, error_type: type[ArtifactStoreError] = ArtifactStoreError) -> None:
        if path.is_symlink():
            raise error_type("Artifact files cannot be symlinks.")
        if not path.exists() or not path.is_file():
            raise ArtifactNotFoundError(f"Artifact file not found: {path.name}")

    def _paths_for(self, artifact_id: str, *, require_complete: bool = True) -> tuple[Path, Path, Path]:
        directory = self._artifact_directory(artifact_id, require_complete=require_complete)
        payload_path = directory / "payload.bin"
        metadata_path = directory / "metadata.json"
        complete_path = directory / ".complete"
        for path in (payload_path, metadata_path, complete_path):
            if path.parent != directory:
                raise ArtifactStoreError("Artifact path escapes its ID directory.")
        if require_complete:
            self._path_is_safe_file(complete_path)
            self._path_is_safe_file(payload_path)
            self._path_is_safe_file(metadata_path)
        return payload_path, metadata_path, complete_path

    def _atomic_write(self, destination: Path, payload: bytes) -> None:
        if len(payload) > self._max_metadata_bytes and destination.name == "metadata.json":
            raise ArtifactStoreError(
                f"Serialized artifact metadata exceeds {self._max_metadata_bytes} bytes."
            )
        if destination.is_symlink():
            raise ArtifactStoreError("Refusing to replace a symlinked artifact file.")
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".artifact-",
                suffix=".tmp",
                dir=destination.parent,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, destination)
            temporary_name = None
        except OSError as error:
            raise ArtifactStoreError(f"Cannot persist artifact file {destination.name}: {error}") from error
        finally:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def _serialize_metadata(self, metadata: ArtifactMetadata) -> bytes:
        try:
            serialized = json.dumps(
                metadata.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as error:
            raise ArtifactStoreError("Artifact metadata must be JSON-serializable.") from error
        payload = (serialized + "\n").encode("utf-8")
        if len(payload) > self._max_metadata_bytes:
            raise ArtifactStoreError(
                f"Serialized artifact metadata exceeds {self._max_metadata_bytes} bytes."
            )
        return payload

    def _load_metadata_from(self, metadata_path: Path, *, expected_id: str) -> ArtifactMetadata:
        self._path_is_safe_file(metadata_path)
        try:
            if metadata_path.stat().st_size > self._max_metadata_bytes:
                raise ArtifactIntegrityError("Artifact metadata exceeds the configured limit.")
            raw = metadata_path.read_bytes()
            metadata = ArtifactMetadata.model_validate_json(raw)
        except ArtifactStoreError:
            raise
        except (OSError, ValidationError, ValueError) as error:
            raise ArtifactIntegrityError(f"Invalid artifact metadata for {expected_id}.") from error
        if metadata.artifact_id != expected_id:
            raise ArtifactIntegrityError("Persisted artifact ID does not match its directory.")
        if metadata.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise ArtifactIntegrityError("Unsupported artifact metadata schema version.")
        return metadata

    def _metadata_for(self, artifact_id: str) -> tuple[ArtifactMetadata, Path, Path]:
        identifier = validate_artifact_id(artifact_id)
        payload_path, metadata_path, complete_path = self._paths_for(identifier)
        # Check marker content too: a user-created empty marker must not make a
        # directory readable as a complete artifact.
        try:
            if complete_path.read_bytes() != b"complete\n":
                raise ArtifactIntegrityError("Artifact completion marker is invalid.")
        except OSError as error:
            raise ArtifactStoreError("Cannot read artifact completion marker.") from error
        metadata = self._load_metadata_from(metadata_path, expected_id=identifier)
        try:
            payload_size = payload_path.stat().st_size
        except OSError as error:
            raise ArtifactStoreError("Cannot stat artifact payload.") from error
        if payload_size != metadata.size:
            raise ArtifactIntegrityError("Artifact payload size does not match metadata.")
        if payload_size > self._max_payload_bytes:
            raise ArtifactIntegrityError("Artifact payload exceeds the configured limit.")
        return metadata, payload_path, metadata_path

    def put(
        self,
        payload: bytes | bytearray | memoryview | str,
        *,
        summary: str = "",
        media_type: str = "application/octet-stream",
        metadata: Mapping[str, Any] | None = None,
        artifact_id: str | None = None,
        preview: str | None = None,
    ) -> ArtifactMetadata:
        """Atomically persist one payload and return its validated metadata.

        ``artifact_id`` is optional; when omitted, a random opaque ID is
        generated.  Supplying an ID never permits path syntax and never
        overwrites an existing artifact.
        """

        raw_payload = self._payload_bytes(payload)
        if len(raw_payload) > self._max_payload_bytes:
            raise ValueError(f"payload exceeds the configured limit of {self._max_payload_bytes} bytes.")
        if not isinstance(summary, str) or len(summary) > self._max_summary_chars:
            raise ValueError(f"summary must be a string of at most {self._max_summary_chars} characters.")
        if not isinstance(media_type, str) or not media_type or len(media_type) > MAX_ARTIFACT_MEDIA_TYPE_CHARS:
            raise ValueError("media_type must be a non-empty string of at most 255 characters.")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in media_type):
            raise ValueError("media_type must not contain control characters.")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping.")
        metadata_values = dict(metadata or {})
        if any(not isinstance(key, str) for key in metadata_values):
            raise ValueError("metadata keys must be strings.")
        if preview is not None and not isinstance(preview, str):
            raise TypeError("preview must be a string when supplied.")

        identifier = self._new_artifact_id() if artifact_id is None else validate_artifact_id(artifact_id)
        target = self._artifact_directory(identifier)
        try:
            target.mkdir()
        except FileExistsError as error:
            if target.is_symlink():
                raise ArtifactStoreError("Artifact directories cannot be symlinks.") from error
            raise ArtifactConflictError(f"Artifact already exists: {identifier}") from error
        except OSError as error:
            raise ArtifactStoreError(f"Cannot create artifact directory {identifier}.") from error

        try:
            generated_preview = raw_payload[: self._max_preview_chars * 4].decode("utf-8", errors="replace")
            generated_preview = generated_preview[: self._max_preview_chars]
            selected_preview = generated_preview if preview is None else preview
            if len(selected_preview) > self._max_preview_chars:
                raise ValueError(
                    f"preview must be at most {self._max_preview_chars} characters."
                )
            try:
                # Validate nested metadata and reject NaN/Infinity before the
                # artifact directory can be published.
                json.dumps(metadata_values, ensure_ascii=False, allow_nan=False, sort_keys=True)
            except (TypeError, ValueError) as error:
                raise ValueError("metadata must contain only JSON-serializable finite values.") from error

            created_at = datetime.now(timezone.utc)
            checksum = hashlib.sha256(raw_payload).hexdigest()
            artifact_metadata = ArtifactMetadata(
                artifact_id=identifier,
                summary=summary,
                size=len(raw_payload),
                preview=selected_preview,
                media_type=media_type,
                metadata=metadata_values,
                checksum=checksum,
                created_at=created_at,
            )
            payload_path = target / "payload.bin"
            metadata_path = target / "metadata.json"
            complete_path = target / ".complete"
            self._atomic_write(payload_path, raw_payload)
            self._atomic_write(metadata_path, self._serialize_metadata(artifact_metadata))
            self._atomic_write(complete_path, b"complete\n")
            return artifact_metadata
        except Exception:
            # The directory was created exclusively by this call.  Remove only
            # that bounded, private directory; no caller path is ever followed.
            try:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
            except OSError:
                pass
            raise

    # ``save`` and ``create`` make the isolated store convenient for callers
    # without introducing a second persistence implementation.
    def save(self, payload: bytes | bytearray | memoryview | str, **kwargs: Any) -> ArtifactMetadata:
        return self.put(payload, **kwargs)

    def create(self, payload: bytes | bytearray | memoryview | str, **kwargs: Any) -> ArtifactMetadata:
        return self.put(payload, **kwargs)

    def get_metadata(self, artifact_id: str) -> ArtifactMetadata:
        """Return validated metadata for one complete artifact."""

        return self._metadata_for(artifact_id)[0]

    def load_metadata(self, artifact_id: str) -> ArtifactMetadata:
        """Compatibility alias for :meth:`get_metadata`."""

        return self.get_metadata(artifact_id)

    def read(self, artifact_id: str, *, offset: int = 0, limit: int | None = None) -> bytes:
        """Read a bounded byte slice of a complete artifact payload.

        ``offset`` and ``limit`` are measured in bytes.  A missing limit reads
        through the end, still bounded by ``max_read_bytes``.
        """

        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer.")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be None or a non-negative integer.")
        metadata, payload_path, _ = self._metadata_for(artifact_id)
        if limit is not None and limit > self._max_read_bytes:
            raise ValueError(f"limit cannot exceed {self._max_read_bytes} bytes.")
        requested = metadata.size - offset if limit is None else limit
        if requested < 0:
            requested = 0
        if requested > self._max_read_bytes:
            raise ValueError(f"requested read exceeds {self._max_read_bytes} bytes.")
        try:
            with payload_path.open("rb") as stream:
                stream.seek(offset)
                result = stream.read(requested)
        except OSError as error:
            raise ArtifactStoreError("Cannot read artifact payload.") from error

        # A complete read is also an integrity check.  Partial reads retain
        # bounded I/O and are protected by the exact size check above.
        if offset == 0 and (limit is None or limit >= metadata.size) and len(result) == metadata.size:
            if hashlib.sha256(result).hexdigest() != metadata.checksum:
                raise ArtifactIntegrityError("Artifact payload checksum does not match metadata.")
        return result

    def read_text(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        encoding: str = "utf-8",
    ) -> str:
        """Decode a byte slice as text, using strict UTF-8 by default."""

        if not isinstance(encoding, str) or not encoding or encoding.lower() != "utf-8":
            raise ValueError("Only UTF-8 text decoding is supported by this store.")
        try:
            return self.read(artifact_id, offset=offset, limit=limit).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ArtifactIntegrityError("Artifact payload is not valid UTF-8 text.") from error

    def exists(self, artifact_id: str) -> bool:
        """Return whether a complete, valid artifact exists."""

        try:
            self._metadata_for(artifact_id)
        except (ArtifactNotFoundError, ArtifactStoreError, ValueError):
            return False
        return True

    def _iter_artifact_ids(self) -> list[str]:
        identifiers: list[str] = []
        try:
            entries = tuple(self._artifacts_directory.iterdir())
        except OSError as error:
            raise ArtifactStoreError("Cannot list the artifact directory.") from error
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir():
                continue
            try:
                identifier = validate_artifact_id(entry.name)
                self._metadata_for(identifier)
            except (ArtifactStoreError, ValueError):
                # A foreign, incomplete, or corrupt directory must never make
                # listing follow a path or expose its filesystem location.
                continue
            identifiers.append(identifier)
        identifiers.sort()
        if len(identifiers) > self._max_list_entries:
            raise ArtifactStoreError("Artifact count exceeds the configured listing limit.")
        return identifiers

    def list_ids(self) -> tuple[str, ...]:
        """List only validated opaque IDs, never filesystem paths."""

        return tuple(self._iter_artifact_ids())

    def list(self, *, limit: int | None = None) -> tuple[ArtifactMetadata, ...]:
        """List validated metadata in stable ID order.

        Invalid, incomplete, and symlinked entries are ignored.  This keeps a
        user-controlled directory listing from becoming a path traversal or
        parser attack surface; callers can inspect individual IDs explicitly
        when they need a typed integrity error.
        """

        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be None or a non-negative integer.")
        if limit is not None and limit > self._max_list_entries:
            raise ValueError(f"limit cannot exceed {self._max_list_entries} entries.")
        identifiers = self._iter_artifact_ids()
        if limit is not None:
            identifiers = identifiers[:limit]
        return tuple(self.get_metadata(identifier) for identifier in identifiers)

    def list_metadata(self, *, limit: int | None = None) -> tuple[ArtifactMetadata, ...]:
        """Compatibility alias for :meth:`list`."""

        return self.list(limit=limit)

    def list_artifacts(self, *, limit: int | None = None) -> tuple[ArtifactMetadata, ...]:
        """Compatibility alias for :meth:`list`."""

        return self.list(limit=limit)


# Name used by some integrations that want to emphasize this implementation
# has no cloud backing.  It remains the exact same class and behavior.
LocalArtifactStore = ArtifactStore


def externalize_tool_result(
    store: ArtifactStore,
    tool_name: str,
    result: str,
    *,
    max_inline_bytes: int,
    redactor: Callable[[str], str],
) -> tuple[str, ArtifactMetadata | None]:
    """Return bounded metadata instead of placing a large tool result in context."""

    if len(result.encode("utf-8")) <= max_inline_bytes:
        return result, None
    safe_result = redactor(result)
    record = store.put(
        safe_result,
        summary=f"Output grande da ferramenta {tool_name} externalizado pelo runtime.",
        media_type="application/json; charset=utf-8",
        metadata={
            "tool": tool_name,
            "encoding": "utf-8",
            "redacted": safe_result != result,
            "original_size": len(result.encode("utf-8")),
        },
    )
    return (
        json.dumps(
            {
                "artifact_id": record.artifact_id,
                "summary": record.summary,
                "size": record.size,
                "preview": record.preview,
                "metadata": {
                    **record.metadata,
                    "media_type": record.media_type,
                    "checksum_sha256": record.checksum,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        record,
    )


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ArtifactConflictError",
    "ArtifactIntegrityError",
    "ArtifactMetadata",
    "ArtifactNotFoundError",
    "ArtifactRecord",
    "ArtifactStore",
    "ArtifactStoreError",
    "DEFAULT_ARTIFACT_DIRECTORY",
    "LocalArtifactStore",
    "externalize_tool_result",
    "MAX_ARTIFACT_ID_LENGTH",
    "MAX_ARTIFACT_LIST_ENTRIES",
    "MAX_ARTIFACT_METADATA_BYTES",
    "MAX_ARTIFACT_PAYLOAD_BYTES",
    "MAX_ARTIFACT_PREVIEW_CHARS",
    "MAX_ARTIFACT_READ_BYTES",
    "MAX_ARTIFACT_SUMMARY_CHARS",
    "validate_artifact_id",
]
