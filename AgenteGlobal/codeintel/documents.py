"""Bounded LSP document state and diagnostic storage.

This module deliberately stores metadata, not full document contents.  The
workspace remains the source of truth and full text is read only while a sync
notification is being sent.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pydantic import Field
from runtime.security_text import redact_sensitive_text, truncate_single_line

from .models import StrictModel


MAX_SYNCED_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_DIAGNOSTICS_PER_DOCUMENT = 200
MAX_DIAGNOSTIC_MESSAGE_CHARS = 2_000
MAX_DIAGNOSTIC_SOURCE_CHARS = 256
MAX_DIAGNOSTIC_CODE_CHARS = 256
MAX_DIAGNOSTIC_POSITION = 10_000_000
MAX_TRACKED_DOCUMENTS = 1_000
MAX_DIAGNOSTIC_DOCUMENTS = 1_000


class LSPDocumentStateError(RuntimeError):
    """Raised for an invalid local document lifecycle transition."""


class DocumentState(StrictModel):
    uri: str = Field(min_length=1, max_length=8_192)
    path: str = Field(min_length=1, max_length=8_192)
    language: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    open: bool
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class DocumentSyncResult(StrictModel):
    action: str
    state: DocumentState
    lsp_synced: bool = False
    error: str = Field(default="", max_length=1_000)


class DiagnosticPosition(StrictModel):
    line: int = Field(ge=0, le=MAX_DIAGNOSTIC_POSITION)
    character: int = Field(ge=0, le=MAX_DIAGNOSTIC_POSITION)


class DiagnosticRange(StrictModel):
    start: DiagnosticPosition
    end: DiagnosticPosition


class DiagnosticRecord(StrictModel):
    path: str = Field(min_length=1, max_length=8_192)
    range: DiagnosticRange
    severity: int = Field(ge=1, le=4)
    code: str | int | None = None
    message: str = Field(min_length=1, max_length=MAX_DIAGNOSTIC_MESSAGE_CHARS)
    source: str = Field(default="", max_length=MAX_DIAGNOSTIC_SOURCE_CHARS)
    version: int | None = Field(default=None, ge=0)


class DiagnosticSnapshot(StrictModel):
    path: str
    uri: str
    version: int | None = Field(default=None, ge=0)
    diagnostics: tuple[DiagnosticRecord, ...] = ()
    discarded: int = Field(default=0, ge=0)
    truncated: bool = False


class DiagnosticPublishResult(StrictModel):
    accepted: bool
    reason: str = ""
    snapshot: DiagnosticSnapshot | None = None


class DocumentRegistry:
    """Own minimal document state and bounded, replace-on-publish diagnostics."""

    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise NotADirectoryError(f"LSP workspace is not a directory: {self.workspace}")
        self._documents: dict[str, DocumentState] = {}
        self._diagnostics: dict[str, DiagnosticSnapshot] = {}

    @staticmethod
    def content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def validate_content(content: str) -> None:
        if len(content.encode("utf-8")) > MAX_SYNCED_DOCUMENT_BYTES:
            raise ValueError(f"document exceeds sync limit of {MAX_SYNCED_DOCUMENT_BYTES} bytes")

    def resolve_path(self, path: Path | str) -> tuple[Path, str]:
        supplied = Path(path)
        candidate = supplied.resolve(strict=False) if supplied.is_absolute() else (self.workspace / supplied).resolve(strict=False)
        try:
            relative = candidate.relative_to(self.workspace).as_posix()
        except ValueError as error:
            raise ValueError("document path escapes workspace") from error
        return candidate, relative

    def path_from_uri(self, uri: str) -> tuple[Path, str] | None:
        parsed = urlparse(uri)
        if (
            parsed.scheme != "file"
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
            or any(ord(character) < 32 for character in uri)
        ):
            return None
        raw_path = unquote(parsed.path)
        if os.name == "nt" and raw_path.startswith("/") and len(raw_path) > 2 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        try:
            return self.resolve_path(Path(raw_path))
        except ValueError:
            return None

    def read_content(self, path: Path | str) -> str:
        candidate, _ = self.resolve_path(path)
        if candidate.is_symlink() or not candidate.is_file():
            raise FileNotFoundError(f"document is not a regular file: {candidate}")
        raw = candidate.read_bytes()
        if len(raw) > MAX_SYNCED_DOCUMENT_BYTES:
            raise ValueError(f"document exceeds sync limit of {MAX_SYNCED_DOCUMENT_BYTES} bytes")
        return raw.decode("utf-8", errors="replace")

    def get(self, path: Path | str) -> DocumentState | None:
        _, relative = self.resolve_path(path)
        return self._documents.get(relative)

    def open_document(self, path: Path | str, *, language: str, content: str) -> tuple[DocumentState, bool]:
        candidate, relative = self.resolve_path(path)
        self.validate_content(content)
        digest = self.content_hash(content)
        current = self._documents.get(relative)
        if current is not None and current.open:
            raise LSPDocumentStateError("document is already open")
        if current is None and len(self._documents) >= MAX_TRACKED_DOCUMENTS:
            raise LSPDocumentStateError("document registry is full")
        version = (current.version + 1) if current is not None else 1
        state = DocumentState(
            uri=candidate.as_uri(),
            path=relative,
            language=language,
            version=version,
            open=True,
            content_hash=digest,
        )
        self._documents[relative] = state
        return state, False

    def change_document(
        self,
        path: Path | str,
        *,
        content: str,
        version: int | None = None,
    ) -> DocumentState:
        _, relative = self.resolve_path(path)
        self.validate_content(content)
        current = self._documents.get(relative)
        if current is None or not current.open:
            raise LSPDocumentStateError("document must be open before change")
        next_version = current.version + 1 if version is None else version
        if next_version <= current.version:
            raise LSPDocumentStateError("document version must increase on change")
        state = current.model_copy(
            update={"version": next_version, "content_hash": self.content_hash(content)}
        )
        self._documents[relative] = state
        return state

    def close_document(self, path: Path | str) -> DocumentState:
        _, relative = self.resolve_path(path)
        current = self._documents.get(relative)
        if current is None or not current.open:
            raise LSPDocumentStateError("document is not open")
        state = current.model_copy(update={"open": False})
        self._documents[relative] = state
        return state

    def open_documents(self, language: str | None = None) -> tuple[DocumentState, ...]:
        return tuple(
            state
            for _, state in sorted(self._documents.items())
            if state.open and (language is None or state.language == language)
        )

    @staticmethod
    def _position(value: Any) -> DiagnosticPosition | None:
        if not isinstance(value, dict):
            return None
        line = value.get("line")
        character = value.get("character")
        if not isinstance(line, int) or isinstance(line, bool):
            return None
        if not isinstance(character, int) or isinstance(character, bool):
            return None
        try:
            return DiagnosticPosition(line=line, character=character)
        except ValueError:
            return None

    def _diagnostic(
        self,
        path: str,
        version: int | None,
        value: Any,
    ) -> DiagnosticRecord | None:
        if not isinstance(value, dict):
            return None
        raw_range = value.get("range")
        if not isinstance(raw_range, dict):
            return None
        start = self._position(raw_range.get("start"))
        end = self._position(raw_range.get("end"))
        message = value.get("message")
        if start is None or end is None or not isinstance(message, str) or not message.strip():
            return None
        if (end.line, end.character) < (start.line, start.character):
            return None
        raw_severity = value.get("severity", 3)
        if not isinstance(raw_severity, int) or isinstance(raw_severity, bool) or not 1 <= raw_severity <= 4:
            return None
        severity = raw_severity
        raw_code = value.get("code")
        if isinstance(raw_code, bool) or not isinstance(raw_code, (str, int, type(None))):
            return None
        if isinstance(raw_code, str):
            raw_code = raw_code[:MAX_DIAGNOSTIC_CODE_CHARS]
        source = value.get("source")
        if source is None:
            source = ""
        elif not isinstance(source, str):
            return None
        safe_message = truncate_single_line(
            re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", redact_sensitive_text(message.strip())),
            MAX_DIAGNOSTIC_MESSAGE_CHARS,
        )
        if not safe_message:
            return None
        try:
            return DiagnosticRecord(
                path=path,
                range=DiagnosticRange(start=start, end=end),
                severity=severity,
                code=raw_code,
                message=safe_message,
                source=source[:MAX_DIAGNOSTIC_SOURCE_CHARS],
                version=version,
            )
        except ValueError:
            return None

    def publish_diagnostics(self, params: Any) -> DiagnosticPublishResult:
        if not isinstance(params, dict):
            return DiagnosticPublishResult(accepted=False, reason="malformed params")
        uri = params.get("uri")
        values = params.get("diagnostics")
        if not isinstance(uri, str) or not isinstance(values, list):
            return DiagnosticPublishResult(accepted=False, reason="malformed publishDiagnostics payload")
        resolved = self.path_from_uri(uri)
        if resolved is None:
            return DiagnosticPublishResult(accepted=False, reason="diagnostic outside workspace")
        _, relative = resolved
        raw_version = params.get("version")
        if "version" in params and (
            not isinstance(raw_version, int) or isinstance(raw_version, bool) or raw_version < 0
        ):
            return DiagnosticPublishResult(accepted=False, reason="invalid diagnostic version")
        version = raw_version if isinstance(raw_version, int) and not isinstance(raw_version, bool) and raw_version >= 0 else None
        if version is None:
            state = self._documents.get(relative)
            version = state.version if state is not None else None
        previous = self._diagnostics.get(relative)
        if previous is not None and previous.version is not None:
            if version is None:
                return DiagnosticPublishResult(accepted=False, reason="unversioned diagnostics cannot replace versioned state")
            if version < previous.version:
                return DiagnosticPublishResult(accepted=False, reason="stale diagnostic version")
        state = self._documents.get(relative)
        if state is not None and version is not None and version < state.version:
            return DiagnosticPublishResult(accepted=False, reason="diagnostics are older than the open document")
        diagnostics: list[DiagnosticRecord] = []
        discarded = 0
        for value in values[:MAX_DIAGNOSTICS_PER_DOCUMENT]:
            normalized = self._diagnostic(relative, version, value)
            if normalized is None:
                discarded += 1
            else:
                diagnostics.append(normalized)
        truncated = len(values) > MAX_DIAGNOSTICS_PER_DOCUMENT
        discarded += max(0, len(values) - MAX_DIAGNOSTICS_PER_DOCUMENT)
        if values and not diagnostics:
            return DiagnosticPublishResult(accepted=False, reason="all diagnostics were malformed")
        if relative not in self._diagnostics and len(self._diagnostics) >= MAX_DIAGNOSTIC_DOCUMENTS:
            return DiagnosticPublishResult(accepted=False, reason="diagnostic registry is full")
        snapshot = DiagnosticSnapshot(
            path=relative,
            uri=uri,
            version=version,
            diagnostics=tuple(diagnostics),
            discarded=discarded,
            truncated=truncated,
        )
        self._diagnostics[relative] = snapshot
        return DiagnosticPublishResult(accepted=True, snapshot=snapshot)

    def diagnostic_snapshot(self, path: Path | str) -> DiagnosticSnapshot | None:
        _, relative = self.resolve_path(path)
        return self._diagnostics.get(relative)

    def clear_diagnostics(self, path: Path | str | None = None) -> None:
        if path is None:
            self._diagnostics.clear()
            return
        _, relative = self.resolve_path(path)
        self._diagnostics.pop(relative, None)

    def diagnostics(self, path: Path | str | None = None, *, limit: int = 100) -> tuple[DiagnosticRecord, ...]:
        if limit < 1:
            raise ValueError("diagnostic limit must be positive")
        snapshots = (
            (self.diagnostic_snapshot(path),)
            if path is not None
            else tuple(snapshot for _, snapshot in sorted(self._diagnostics.items()))
        )
        results: list[DiagnosticRecord] = []
        for snapshot in snapshots:
            if snapshot is None:
                continue
            results.extend(snapshot.diagnostics[: max(0, limit - len(results))])
            if len(results) >= limit:
                break
        return tuple(results)


__all__ = [
    "DiagnosticPosition",
    "DiagnosticPublishResult",
    "DiagnosticRange",
    "DiagnosticRecord",
    "DiagnosticSnapshot",
    "DocumentRegistry",
    "DocumentState",
    "DocumentSyncResult",
    "LSPDocumentStateError",
    "MAX_DIAGNOSTICS_PER_DOCUMENT",
    "MAX_SYNCED_DOCUMENT_BYTES",
]
