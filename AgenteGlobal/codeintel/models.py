"""Typed, provider-neutral records for local code intelligence."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


CODE_INDEX_SCHEMA_VERSION = "1.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SymbolKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    INTERFACE = "interface"
    STRUCT = "struct"
    ENUM = "enum"
    VARIABLE = "variable"
    CONSTANT = "constant"
    TYPE = "type"
    RESOURCE = "resource"


class RelationshipKind(StrEnum):
    DEFINES = "defines"
    IMPORTS = "imports"
    REFERENCES = "references"
    CALLS = "calls"
    INHERITS = "inherits"
    IMPLEMENTS = "implements"
    READS = "reads"
    WRITES = "writes"
    CREATES = "creates"
    DEPENDS_ON = "depends_on"
    PUBLISHES = "publishes"
    CONSUMES = "consumes"
    INVOKES = "invokes"


class Confidence(StrEnum):
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    UNCERTAIN = "uncertain"


class ParseStatus(StrEnum):
    PARSED = "parsed"
    FALLBACK = "fallback"
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"
    SKIPPED_LARGE = "skipped_large"


class EvidenceRef(StrictModel):
    path: str
    symbol: str = ""
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=1, ge=1)
    source: str
    confidence: Confidence = Confidence.CONFIRMED

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or not normalized:
            raise ValueError("evidence path must be workspace-relative")
        return normalized


class SymbolRecord(StrictModel):
    id: str
    name: str
    qualified_name: str
    kind: SymbolKind
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    source: str
    signature: str = ""
    exported: bool = False


class ReferenceRecord(StrictModel):
    name: str
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    context_symbol: str = ""
    source: str


class ImportRecord(StrictModel):
    module: str
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    imported_name: str = ""
    alias: str = ""
    source: str


class RelationshipRecord(StrictModel):
    source_symbol: str
    target_symbol: str
    kind: RelationshipKind
    confidence: Confidence
    evidence: tuple[EvidenceRef, ...] = ()
    origin: str


class FileRecord(StrictModel):
    path: str
    language: str
    size: int = Field(ge=0)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    mtime_ns: int = Field(ge=0)
    symbols: tuple[SymbolRecord, ...] = ()
    imports: tuple[ImportRecord, ...] = ()
    references: tuple[ReferenceRecord, ...] = ()
    relationships: tuple[RelationshipRecord, ...] = ()
    parse_status: ParseStatus
    parser_source: str
    parse_error: str = Field(default="", max_length=1_000)


class IndexMetadata(StrictModel):
    schema_version: Literal["1.0"] = CODE_INDEX_SCHEMA_VERSION
    workspace_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    updated_at: datetime
    file_count: int = Field(ge=0)
    symbol_count: int = Field(ge=0)
    relationship_count: int = Field(ge=0)
    last_build_seconds: float = Field(ge=0.0)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("index timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)


class CodeIndexSnapshot(StrictModel):
    metadata: IndexMetadata
    files: dict[str, FileRecord] = Field(default_factory=dict)


class IndexRunMetrics(StrictModel):
    duration_seconds: float = Field(ge=0.0)
    scanned_files: int = Field(ge=0)
    indexed_files: int = Field(ge=0)
    unchanged_files: int = Field(ge=0)
    new_files: int = Field(ge=0)
    modified_files: int = Field(ge=0)
    deleted_files: int = Field(ge=0)
    skipped_files: int = Field(ge=0)
    symbols_indexed: int = Field(ge=0)
    relationships_indexed: int = Field(ge=0)
    cache_hits: int = Field(ge=0)
    cache_misses: int = Field(ge=0)
    rebuilt: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "CODE_INDEX_SCHEMA_VERSION",
    "CodeIndexSnapshot",
    "Confidence",
    "EvidenceRef",
    "FileRecord",
    "ImportRecord",
    "IndexMetadata",
    "IndexRunMetrics",
    "ParseStatus",
    "ReferenceRecord",
    "RelationshipKind",
    "RelationshipRecord",
    "SymbolKind",
    "SymbolRecord",
]
