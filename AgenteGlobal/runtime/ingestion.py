"""Bounded input ingestion and structural chunking for the context engine.

The module deliberately stops at a durable source artifact and an ordered set
of local chunks.  It does not index, retrieve, or mutate session state.  The
source is stored byte-for-byte before parsing so a parser failure never loses
the original input.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .artifacts import ArtifactMetadata, ArtifactStore


class ContentType(str, Enum):
    PYTHON = "python"
    CODE = "code"
    MARKDOWN = "markdown"
    TEXT = "text"
    JSON = "json"
    YAML = "yaml"
    TOML = "toml"
    LOG = "log"
    BINARY = "binary"


# The alias is useful to clients that use document terminology.
DocumentType = ContentType


class IngestionError(ValueError):
    """Base error for invalid input or a defensive ingestion limit."""


class IngestionLimitError(IngestionError):
    """Input or chunk count exceeded an explicit local bound."""


@dataclass(frozen=True, slots=True)
class IngestionConfig:
    """Defensive bounds for one ingestion operation.

    ``max_chunk_chars`` is the primary bound.  ``max_chunk_bytes`` also
    protects UTF-8-heavy content and leaves a predictable upper bound for a
    later provider call.  A character whose UTF-8 representation is larger
    than the byte bound is retained as a one-character chunk because splitting
    a Unicode scalar value would corrupt the source view.
    """

    max_input_bytes: int = 16 * 1024 * 1024
    max_chunk_chars: int = 8_192
    max_chunk_bytes: int = 64 * 1024
    max_chunks: int = 10_000
    max_structure_depth: int = 64
    max_structure_nodes: int = 20_000

    def __post_init__(self) -> None:
        for name in (
            "max_input_bytes",
            "max_chunk_chars",
            "max_chunk_bytes",
            "max_chunks",
            "max_structure_depth",
            "max_structure_nodes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer.")

    @property
    def max_chunk_size(self) -> int:
        return self.max_chunk_chars


@dataclass(frozen=True, slots=True)
class Chunk:
    """One bounded source slice with character and byte coordinates."""

    chunk_id: str
    kind: str
    text: str
    start_offset: int
    end_offset: int
    order: int
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.chunk_id or not isinstance(self.chunk_id, str):
            raise ValueError("chunk_id must be a non-empty string.")
        if not self.kind or not isinstance(self.kind, str):
            raise ValueError("kind must be a non-empty string.")
        if self.start_offset < 0 or self.end_offset < self.start_offset:
            raise ValueError("Chunk offsets must be non-negative and ordered.")
        if self.order < 0:
            raise ValueError("Chunk order must be non-negative.")

    @property
    def id(self) -> str:
        return self.chunk_id

    @property
    def content(self) -> str:
        return self.text

    @property
    def start(self) -> int:
        return self.start_offset

    @property
    def end(self) -> int:
        return self.end_offset

    @property
    def offset(self) -> tuple[int, int]:
        return self.start_offset, self.end_offset

    @property
    def offsets(self) -> tuple[int, int]:
        return self.offset

    @property
    def size(self) -> int:
        return self.end_offset - self.start_offset

    @property
    def chunk_type(self) -> str:
        return self.kind

    @property
    def body(self) -> str:
        return self.text

    @property
    def offset_start(self) -> int:
        return self.start_offset

    @property
    def offset_end(self) -> int:
        return self.end_offset

    @property
    def byte_start(self) -> int:
        return int(self.metadata.get("byte_start", 0))

    @property
    def byte_end(self) -> int:
        return int(self.metadata.get("byte_end", 0))


@dataclass(frozen=True, slots=True)
class IngestionEvent:
    """Non-sensitive operational record emitted during ingestion."""

    name: str
    timestamp: datetime
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def event(self) -> str:
        return self.name

    @property
    def payload(self) -> Mapping[str, Any]:
        return self.details


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Durable source artifact plus bounded chunks, with no retrieval state."""

    artifact: ArtifactMetadata
    content_type: ContentType
    source_name: str
    size_bytes: int
    chunks: tuple[Chunk, ...]
    strategy: str
    events: tuple[IngestionEvent, ...] = ()

    @property
    def artifact_id(self) -> str:
        return self.artifact.artifact_id

    @property
    def original_artifact(self) -> ArtifactMetadata:
        return self.artifact

    @property
    def document_type(self) -> ContentType:
        return self.content_type

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @property
    def detected_type(self) -> ContentType:
        return self.content_type

    @property
    def source_artifact(self) -> ArtifactMetadata:
        return self.artifact


@dataclass(frozen=True, slots=True)
class _Unit:
    start: int
    end: int
    kind: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


_EXTENSION_TYPES: dict[str, ContentType] = {
    ".py": ContentType.PYTHON,
    ".pyw": ContentType.PYTHON,
    ".pyi": ContentType.PYTHON,
    ".js": ContentType.CODE,
    ".jsx": ContentType.CODE,
    ".ts": ContentType.CODE,
    ".tsx": ContentType.CODE,
    ".java": ContentType.CODE,
    ".go": ContentType.CODE,
    ".rs": ContentType.CODE,
    ".c": ContentType.CODE,
    ".cc": ContentType.CODE,
    ".cpp": ContentType.CODE,
    ".h": ContentType.CODE,
    ".hpp": ContentType.CODE,
    ".cs": ContentType.CODE,
    ".rb": ContentType.CODE,
    ".php": ContentType.CODE,
    ".sh": ContentType.CODE,
    ".bash": ContentType.CODE,
    ".ps1": ContentType.CODE,
    ".sql": ContentType.CODE,
    ".md": ContentType.MARKDOWN,
    ".markdown": ContentType.MARKDOWN,
    ".json": ContentType.JSON,
    ".yaml": ContentType.YAML,
    ".yml": ContentType.YAML,
    ".toml": ContentType.TOML,
    ".log": ContentType.LOG,
    ".out": ContentType.LOG,
    ".err": ContentType.LOG,
}

_MEDIA_TYPES: dict[ContentType, str] = {
    ContentType.PYTHON: "text/x-python; charset=utf-8",
    ContentType.CODE: "text/x-source; charset=utf-8",
    ContentType.MARKDOWN: "text/markdown; charset=utf-8",
    ContentType.TEXT: "text/plain; charset=utf-8",
    ContentType.JSON: "application/json",
    ContentType.YAML: "application/yaml",
    ContentType.TOML: "application/toml",
    ContentType.LOG: "text/plain; charset=utf-8",
    ContentType.BINARY: "application/octet-stream",
}

_HEADING_RE = re.compile(r"^(?P<indent> {0,3})(?P<marks>#{1,6})(?:[ \t]+|$)(?P<title>.*?)[ \t]*\r?\n?$")
_KEY_RE = re.compile(r"^(?P<indent>\s*)(?:(?P<dash>-)[ \t]+)?(?P<key>[^#:\n][^:\n]*):(?:[ \t].*)?(?:\r?\n)?$")
_TOML_TABLE_RE = re.compile(r"^\s*(?P<array>\[\[.*\]\]|\[.*\])[ \t]*(?:#.*)?(?:\r?\n)?$")
_LOG_TIMESTAMP_RE = re.compile(
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}[T ][0-9:.+Z-]+|\[?\d{4}/\d{2}/\d{2}[ T][0-9:.+-]+\]?)"
)
_LOG_LEVEL_RE = re.compile(r"\b(?P<level>TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|ERR|FATAL|CRITICAL|EXCEPTION)\b", re.I)


def _safe_source_name(value: str | Path | None) -> str:
    if value is None:
        return "input"
    text = str(value)
    if not text or len(text) > 512 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise IngestionError("source name must be non-empty, bounded, and free of control characters.")
    # Metadata and logs never need the caller's absolute path.
    name = Path(text).name
    return name or "input"


def _decode_text(payload: bytes) -> str | None:
    if b"\x00" in payload:
        return None
    try:
        # Keep a UTF-8 BOM in the source view so character and byte offsets
        # continue to refer to the exact bytes persisted in the artifact.
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _looks_like_json(text: str) -> bool:
    candidate = text.lstrip()
    if not candidate.startswith(("{", "[")):
        return False
    try:
        json.loads(candidate, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, json.JSONDecodeError):
        return False
    return True


def detect_content_type(
    payload: bytes | str,
    *,
    filename: str | Path | None = None,
    media_type: str | None = None,
) -> ContentType:
    """Detect content using extension first, then conservative content clues."""

    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    text = payload if isinstance(payload, str) else _decode_text(raw)
    if text is not None:
        text = text.removeprefix("\ufeff")
    extension = Path(filename).suffix.lower() if filename else ""
    if media_type:
        lowered = media_type.lower()
        if "json" in lowered:
            return ContentType.JSON
        if "yaml" in lowered or "yml" in lowered:
            return ContentType.YAML
        if "toml" in lowered:
            return ContentType.TOML
        if "markdown" in lowered:
            return ContentType.MARKDOWN
        if lowered.startswith("text/") and "log" in lowered:
            return ContentType.LOG
    by_extension = _EXTENSION_TYPES.get(extension)
    if by_extension is not None:
        return by_extension
    if media_type:
        lowered = media_type.lower()
        if "json" in lowered:
            return ContentType.JSON
        if "yaml" in lowered or "yml" in lowered:
            return ContentType.YAML
        if "markdown" in lowered:
            return ContentType.MARKDOWN
        if lowered.startswith("text/"):
            return ContentType.TEXT
    if text is None:
        return ContentType.BINARY
    if _looks_like_json(text):
        return ContentType.JSON
    stripped = text.lstrip()
    if re.search(r"(?m)^\s*#{1,6}(?:[ \t]+|$)", stripped):
        return ContentType.MARKDOWN
    if re.search(r"(?m)^\s*(?:\[[^\[\]\n]+\]|\[\[[^\[\]\n]+\]\])\s*(?:#.*)?$", text) and "=" in text:
        return ContentType.TOML
    if re.search(r"(?m)^\s*[A-Za-z_][A-Za-z0-9_.-]*\s*=\s*(?:[\"'0-9tTfF\[\{])", text):
        return ContentType.TOML
    if re.search(r"(?m)^\s*(?:[-\w][^:\n]{0,100}):(?:\s|$)", text):
        return ContentType.YAML
    if re.search(r"(?m)^\s*(?:def|class|async\s+def|import|from)\b", text):
        return ContentType.PYTHON
    if re.search(r"(?m)^\s*(?:function|class|const|let|var|public|private|package)\b", text):
        return ContentType.CODE
    if _LOG_TIMESTAMP_RE.search(text) and _LOG_LEVEL_RE.search(text):
        return ContentType.LOG
    return ContentType.TEXT


detect_type = detect_content_type


def _line_entries(text: str) -> list[tuple[int, int, str, int]]:
    entries: list[tuple[int, int, str, int]] = []
    cursor = 0
    line_number = 1
    for line in text.splitlines(keepends=True):
        end = cursor + len(line)
        entries.append((cursor, end, line, line_number))
        cursor, line_number = end, line_number + 1
    if cursor < len(text):
        entries.append((cursor, len(text), text[cursor:], line_number))
    elif not entries and text == "":
        entries.append((0, 0, "", 1))
    return entries


def _line_coordinates(text: str) -> tuple[list[int], list[int]]:
    char_starts = [0]
    byte_starts = [0]
    char_cursor = byte_cursor = 0
    for line in text.splitlines(keepends=True):
        char_cursor += len(line)
        byte_cursor += len(line.encode("utf-8"))
        char_starts.append(char_cursor)
        byte_starts.append(byte_cursor)
    if char_starts[-1] != len(text):
        char_starts.append(len(text))
        byte_starts.append(len(text.encode("utf-8")))
    return char_starts, byte_starts


def _ast_char_offset(text: str, line_starts: list[int], line: int, byte_column: int) -> int:
    start = line_starts[max(0, line - 1)] if line - 1 < len(line_starts) else len(text)
    source_line = text[start : text.find("\n", start) + 1 if "\n" in text[start:] else len(text)]
    try:
        prefix = source_line.encode("utf-8")[:byte_column].decode("utf-8")
    except UnicodeDecodeError:
        prefix = source_line.encode("utf-8")[:byte_column].decode("utf-8", errors="ignore")
    return min(len(text), start + len(prefix))


def _node_span(text: str, line_starts: list[int], node: ast.AST) -> tuple[int, int] | None:
    lineno = getattr(node, "lineno", None)
    end_lineno = getattr(node, "end_lineno", None)
    col = getattr(node, "col_offset", None)
    end_col = getattr(node, "end_col_offset", None)
    if not all(isinstance(value, int) for value in (lineno, end_lineno, col, end_col)):
        return None
    start = _ast_char_offset(text, line_starts, lineno, col)
    end = _ast_char_offset(text, line_starts, end_lineno, end_col)
    decorators = getattr(node, "decorator_list", ())
    if decorators:
        first = _node_span(text, line_starts, decorators[0])
        if first is not None:
            start = min(start, first[0])
    return (start, end) if end > start else None


def _python_units(text: str, config: IngestionConfig) -> tuple[list[_Unit], str | None]:
    line_starts, _ = _line_coordinates(text)
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as error:
        return [], f"python parser fallback: {type(error).__name__}"
    units: list[_Unit] = [_Unit(0, len(text), "file", {"language": "python"})] if text else []
    node_count = 0
    structure_types = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)

    def visit(node: ast.AST, parent: str | None = None, depth: int = 0) -> None:
        nonlocal node_count
        if depth > config.max_structure_depth:
            raise IngestionLimitError("Python structure nesting exceeds the configured limit.")
        if isinstance(node, structure_types):
            node_count += 1
            if node_count > config.max_structure_nodes:
                raise IngestionLimitError("Python structure node count exceeds the configured limit.")
            if isinstance(node, ast.ClassDef):
                kind = "class"
                next_parent = "class"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if parent == "class" else "function"
                next_parent = kind
            else:
                kind, next_parent = "block", parent
            span = _node_span(text, line_starts, node)
            if span is not None:
                metadata: dict[str, Any] = {"language": "python", "ast_type": type(node).__name__}
                name = getattr(node, "name", None)
                if isinstance(name, str):
                    metadata["name"] = name[:256]
                units.append(_Unit(*span, kind, metadata))
            for child in ast.iter_child_nodes(node):
                visit(child, next_parent, depth + 1)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, parent, depth + 1)

    try:
        visit(tree)
    except IngestionLimitError:
        raise
    return units, None


def _generic_code_units(text: str) -> list[_Unit]:
    entries = _line_entries(text)
    declaration_indices: list[tuple[int, str]] = []
    declaration_re = re.compile(r"^(?P<indent>\s*)(?P<kind>class|(?:async\s+)?function|def)\b", re.I)
    for index, (_, _, line, _) in enumerate(entries):
        match = declaration_re.match(line)
        if match:
            kind_text = match.group("kind").lower()
            declaration_indices.append((index, "class" if kind_text == "class" else "function"))
    units: list[_Unit] = []
    if declaration_indices:
        for position, (index, kind) in enumerate(declaration_indices):
            start = entries[index][0]
            end = entries[declaration_indices[position + 1][0]][0] if position + 1 < len(declaration_indices) else len(text)
            units.append(_Unit(start, end, kind, {"language": "code"}))
    else:
        for start, end, line, _ in entries:
            if line.strip():
                units.append(_Unit(start, end, "block", {"language": "code"}))
    return units or [_Unit(0, len(text), "file", {"language": "code"})]


def _markdown_units(text: str) -> list[_Unit]:
    entries = _line_entries(text)
    headings: list[tuple[int, int, int, str]] = []
    for index, (start, end, line, _) in enumerate(entries):
        match = _HEADING_RE.match(line)
        if match:
            headings.append((index, start, len(match.group("marks")), match.group("title").strip()[:512]))
    units: list[_Unit] = []
    for position, (index, start, level, title) in enumerate(headings):
        end_index = len(entries)
        for later_index, later_start, later_level, _ in headings[position + 1 :]:
            if later_level <= level:
                end_index = later_index
                break
        end = entries[end_index][0] if end_index < len(entries) else len(text)
        units.append(_Unit(start, end, "section", {"heading_level": level, "title": title}))
        units.append(_Unit(entries[index][0], entries[index][1], "heading", {"heading_level": level, "title": title}))
    heading_lines = {index for index, _, _, _ in headings}
    paragraph_start: int | None = None
    paragraph_end = 0
    for index, (start, end, line, _) in enumerate(entries + [(len(text), len(text), "", len(entries) + 1)]):
        if index in heading_lines or not line.strip():
            if paragraph_start is not None:
                units.append(_Unit(paragraph_start, paragraph_end, "paragraph", {}))
                paragraph_start = None
            continue
        if paragraph_start is None:
            paragraph_start = start
        paragraph_end = end
    if not units and text:
        units.append(_Unit(0, len(text), "file", {"format": "markdown"}))
    return units


class _JSONStructureParser:
    def __init__(self, text: str, config: IngestionConfig) -> None:
        self.text = text
        self.config = config
        self.decoder = json.JSONDecoder(parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        self.units: list[_Unit] = []
        self.nodes = 0

    def _skip(self, index: int) -> int:
        while index < len(self.text) and self.text[index] in " \t\r\n":
            index += 1
        return index

    def parse(self) -> list[_Unit]:
        start = self._skip(0)
        if start >= len(self.text):
            return []
        end, _ = self._value(start, "$", 0)
        if self._skip(end) != len(self.text):
            raise ValueError("Trailing JSON data.")
        return self.units

    def _value(self, index: int, path: str, depth: int) -> tuple[int, Any]:
        if depth > self.config.max_structure_depth:
            raise IngestionLimitError("JSON structure nesting exceeds the configured limit.")
        index = self._skip(index)
        if index >= len(self.text):
            raise ValueError("Incomplete JSON value.")
        start = index
        marker = self.text[index]
        self.nodes += 1
        if self.nodes > self.config.max_structure_nodes:
            raise IngestionLimitError("JSON structure node count exceeds the configured limit.")
        if marker == "{":
            index += 1
            index = self._skip(index)
            while index < len(self.text) and self.text[index] != "}":
                key_start = index
                key, index = self.decoder.raw_decode(self.text, index)
                if not isinstance(key, str):
                    raise ValueError("JSON object key must be a string.")
                index = self._skip(index)
                if index >= len(self.text) or self.text[index] != ":":
                    raise ValueError("JSON object member requires a colon.")
                value_start = self._skip(index + 1)
                bounded_key = key[:256]
                child_path = f"{path}.{bounded_key}" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", bounded_key) else f"{path}[{json.dumps(bounded_key, ensure_ascii=False)}]"
                value_end, _ = self._value(value_start, child_path, depth + 1)
                self.units.append(_Unit(key_start, value_end, "field", {"path": child_path, "key": bounded_key, "key_truncated": len(key) > len(bounded_key)}))
                index = self._skip(value_end)
                if index < len(self.text) and self.text[index] == ",":
                    index = self._skip(index + 1)
                elif index >= len(self.text) or self.text[index] != "}":
                    raise ValueError("Invalid JSON object separator.")
            if index >= len(self.text):
                raise ValueError("Unclosed JSON object.")
            end = index + 1
            self.units.append(_Unit(start, end, "object", {"path": path, "json_type": "object"}))
            return end, None
        if marker == "[":
            index += 1
            index = self._skip(index)
            item_index = 0
            while index < len(self.text) and self.text[index] != "]":
                value_start = index
                child_path = f"{path}[{item_index}]"
                value_end, _ = self._value(value_start, child_path, depth + 1)
                self.units.append(_Unit(value_start, value_end, "item", {"path": child_path, "index": item_index}))
                item_index += 1
                index = self._skip(value_end)
                if index < len(self.text) and self.text[index] == ",":
                    index = self._skip(index + 1)
                elif index >= len(self.text) or self.text[index] != "]":
                    raise ValueError("Invalid JSON array separator.")
            if index >= len(self.text):
                raise ValueError("Unclosed JSON array.")
            end = index + 1
            self.units.append(_Unit(start, end, "array", {"path": path, "json_type": "array"}))
            return end, None
        value, end = self.decoder.raw_decode(self.text, index)
        self.units.append(_Unit(start, end, "value", {"path": path, "json_type": type(value).__name__}))
        return end, value


def _yaml_units(text: str, config: IngestionConfig | None = None) -> list[_Unit]:
    """Create indentation-aware YAML units without introducing PyYAML."""

    entries = _line_entries(text)
    structural: list[tuple[int, int, int, str, str]] = []
    for index, (start, end, line, _) in enumerate(entries):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _KEY_RE.match(line)
        if not match:
            continue
        indent = len(match.group("indent").replace("\t", "    "))
        kind = "sequence_item" if match.group("dash") else "mapping"
        key = match.group("key").strip()[:256]
        structural.append((index, start, indent, kind, key))
        if config is not None and len(structural) > config.max_structure_nodes:
            raise IngestionLimitError("YAML structure node count exceeds the configured limit.")
    units: list[_Unit] = []
    for position, (index, start, indent, kind, key) in enumerate(structural):
        end_index = len(entries)
        for later_index, _, later_indent, _, _ in structural[position + 1 :]:
            if later_indent <= indent:
                end_index = later_index
                break
        end = entries[end_index][0] if end_index < len(entries) else len(text)
        units.append(_Unit(start, end, kind, {"path": key, "indent": indent, "parser": "indentation"}))
    if text:
        units.insert(0, _Unit(0, len(text), "document", {"parser": "indentation"}))
    return units


def _toml_units(text: str) -> tuple[list[_Unit], str | None]:
    try:
        tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError, RecursionError, MemoryError) as error:
        return [], f"toml parser fallback: {type(error).__name__}"
    entries = _line_entries(text)
    headers: list[tuple[int, int, str]] = []
    for index, (start, _, line, _) in enumerate(entries):
        match = _TOML_TABLE_RE.match(line)
        if match:
            headers.append((index, start, match.group("array")))
    units: list[_Unit] = []
    if text:
        units.append(_Unit(0, entries[headers[0][0]][0] if headers else len(text), "table", {"path": "$"}))
    for position, (index, start, header) in enumerate(headers):
        end = entries[headers[position + 1][0]][0] if position + 1 < len(headers) else len(text)
        kind = "array_of_tables" if header.startswith("[[") else "table"
        units.append(_Unit(start, end, kind, {"path": header.strip("[] "), "header": header}))
    # TOML's standard parser intentionally does not expose source locations.
    # Keys are therefore bounded line-oriented logical units after validation;
    # this still gives arrays and scalar fields useful structural boundaries.
    current_path = "$"
    for index, (start, end, line, _) in enumerate(entries):
        table_match = _TOML_TABLE_RE.match(line)
        if table_match:
            current_path = table_match.group("array").strip("[] ") or "$"
            continue
        key_match = re.match(r"^\s*(?P<key>[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\s*=", line)
        if key_match:
            key = key_match.group("key")
            value_start = line.find("=") + 1
            value = line[value_start:].strip()
            kind = "array" if value.startswith("[") else "key"
            path = f"{current_path}.{key}" if current_path != "$" else f"$.{key}"
            units.append(_Unit(start, end, kind, {"path": path, "key": key[:256]}))
    if not units and text:
        units.append(_Unit(0, len(text), "table", {"path": "$"}))
    return units, None


def _log_units(text: str) -> list[_Unit]:
    entries = _line_entries(text)
    records: list[tuple[int, int, str | None, str, bool]] = []
    current_start: int | None = None
    current_timestamp: str | None = None
    current_level = "record"
    current_error = False
    for start, end, line, _ in entries:
        timestamp_match = _LOG_TIMESTAMP_RE.search(line)
        level_match = _LOG_LEVEL_RE.search(line)
        level_at_boundary = level_match and line.lstrip().upper().startswith(level_match.group("level").upper())
        if timestamp_match or (level_at_boundary and current_start is not None):
            if current_start is not None:
                records.append((current_start, start, current_timestamp, current_level, current_error))
            current_start = start
            current_timestamp = timestamp_match.group("timestamp")[:128] if timestamp_match else None
            current_level = level_match.group("level").upper() if level_match else "record"
            current_error = current_level in {"ERROR", "ERR", "FATAL", "CRITICAL", "EXCEPTION"}
        elif current_start is None and line.strip():
            current_start = start
            current_timestamp = timestamp_match.group("timestamp")[:128] if timestamp_match else None
            current_level = level_match.group("level").upper() if level_match else "record"
            current_error = current_level in {"ERROR", "ERR", "FATAL", "CRITICAL", "EXCEPTION"}
    if current_start is not None:
        records.append((current_start, len(text), current_timestamp, current_level, current_error))
    return [
        _Unit(start, end, "error" if error else "event", {"timestamp": timestamp, "event": level, "error_boundary": error})
        for start, end, timestamp, level, error in records
        if end > start
    ]


class Ingestor:
    """Persist and structurally chunk one input, synchronously and locally."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        config: IngestionConfig | None = None,
        max_input_bytes: int | None = None,
        max_chunk_chars: int | None = None,
        max_chunk_size: int | None = None,
        max_chunk_bytes: int | None = None,
        max_chunks: int | None = None,
        logger: logging.Logger | None = None,
        event_callback: Callable[[IngestionEvent], None] | None = None,
    ) -> None:
        if not hasattr(artifact_store, "put"):
            raise TypeError("artifact_store must provide put().")
        selected = config or IngestionConfig()
        if max_chunk_chars is not None and max_chunk_size is not None and max_chunk_chars != max_chunk_size:
            raise ValueError("max_chunk_chars and max_chunk_size must agree when both are supplied.")
        overrides = {
            name: value
            for name, value in (
                ("max_input_bytes", max_input_bytes),
                ("max_chunk_chars", max_chunk_chars),
                ("max_chunk_bytes", max_chunk_bytes),
                ("max_chunks", max_chunks),
            )
            if value is not None
        }
        if max_chunk_chars is None and max_chunk_size is not None:
            overrides["max_chunk_chars"] = max_chunk_size
        self.config = replace(selected, **overrides) if overrides else selected
        self.artifact_store = artifact_store
        self.logger = logger
        self.event_callback = event_callback
        self._events: list[IngestionEvent] = []

    def _emit(self, name: str, **details: Any) -> None:
        event = IngestionEvent(name=name, timestamp=datetime.now(timezone.utc), details=dict(details))
        self._events.append(event)
        if self.logger is not None:
            self.logger.info("%s", name, extra={"ingestion_event": name, "ingestion_details": dict(details)})
        if self.event_callback is not None:
            try:
                self.event_callback(event)
            except Exception:
                if self.logger is not None:
                    self.logger.exception("Ingestion event callback failed.")

    def ingest_file(
        self,
        path: str | Path,
        *,
        filename: str | Path | None = None,
        source_name: str | Path | None = None,
        media_type: str | None = None,
    ) -> IngestionResult:
        candidate = Path(path)
        try:
            if candidate.is_symlink() or not candidate.is_file():
                raise IngestionError("input path must be a regular file and cannot be a symlink.")
            size = candidate.stat().st_size
            if size > self.config.max_input_bytes:
                raise IngestionLimitError("input exceeds the configured byte limit.")
            payload = candidate.read_bytes()
        except OSError as error:
            raise IngestionError("cannot read input file.") from error
        if len(payload) > self.config.max_input_bytes:
            raise IngestionLimitError("input exceeds the configured byte limit.")
        return self.ingest(
            payload,
            filename=filename or candidate.name,
            source_name=source_name,
            media_type=media_type,
        )

    def ingest(
        self,
        source: bytes | bytearray | memoryview | str | Path,
        *,
        filename: str | Path | None = None,
        source_name: str | Path | None = None,
        media_type: str | None = None,
    ) -> IngestionResult:
        if isinstance(source, Path):
            return self.ingest_file(source, filename=filename, source_name=source_name, media_type=media_type)
        if isinstance(source, str):
            payload = source.encode("utf-8")
        elif isinstance(source, (bytes, bytearray, memoryview)):
            payload = bytes(source)
        else:
            raise TypeError("source must be text, bytes, or a Path.")
        if len(payload) > self.config.max_input_bytes:
            raise IngestionLimitError("input exceeds the configured byte limit.")
        name = _safe_source_name(source_name or filename)
        selected_type = detect_content_type(payload, filename=filename or source_name, media_type=media_type)
        selected_media_type = media_type or _MEDIA_TYPES[selected_type]
        self._events = []
        self._emit("ingestion.started", size_bytes=len(payload))
        self._emit("ingestion.detected", content_type=selected_type.value, source_name=name)
        try:
            artifact = self.artifact_store.put(
                payload,
                summary=f"Original input: {name}"[:4_000],
                media_type=selected_media_type,
                metadata={
                    "source_name": name,
                    "content_type": selected_type.value,
                    "ingestion_schema_version": "1.0",
                },
            )
            self._emit("ingestion.artifact_stored", artifact_id=artifact.artifact_id, size_bytes=len(payload))
            text = _decode_text(payload)
            if text is None:
                units, parse_error, strategy = [], None, "binary"
            else:
                units, parse_error, strategy = self._units_for(text, selected_type)
            if parse_error:
                self._emit("ingestion.parse_fallback", content_type=selected_type.value, error=parse_error)
            chunks = self._chunks_from_units(text or "", units, selected_type, strategy, artifact.artifact_id)
            self._emit("ingestion.chunked", chunk_count=len(chunks), strategy=strategy)
            self._emit("ingestion.completed", artifact_id=artifact.artifact_id, chunk_count=len(chunks))
            return IngestionResult(artifact, selected_type, name, len(payload), tuple(chunks), strategy, tuple(self._events))
        except Exception as error:
            self._emit("ingestion.failed", error=type(error).__name__)
            raise

    def chunk(self, text: str | bytes, *, content_type: ContentType | str | None = None, filename: str | Path | None = None) -> tuple[Chunk, ...]:
        """Chunk without persistence; useful for deterministic local callers."""
        payload = text.encode("utf-8") if isinstance(text, str) else bytes(text)
        if len(payload) > self.config.max_input_bytes:
            raise IngestionLimitError("input exceeds the configured byte limit.")
        decoded = _decode_text(payload)
        if decoded is None:
            return tuple(self._chunks_from_units("", [], ContentType.BINARY, "binary", ""))
        selected = ContentType(content_type) if content_type is not None else detect_content_type(payload, filename=filename)
        units, _, strategy = self._units_for(decoded, selected)
        return tuple(self._chunks_from_units(decoded, units, selected, strategy, ""))

    ingest_input = ingest

    def _units_for(self, text: str, selected_type: ContentType) -> tuple[list[_Unit], str | None, str]:
        bom_offset = 1 if text.startswith("\ufeff") else 0
        parse_text = text[bom_offset:]
        parse_error: str | None = None
        strategy = "structural"
        if selected_type is ContentType.PYTHON:
            units, parse_error = _python_units(parse_text, self.config)
            if parse_error:
                units = []
        elif selected_type is ContentType.CODE:
            units = _generic_code_units(parse_text)
        elif selected_type is ContentType.MARKDOWN:
            units = _markdown_units(parse_text)
        elif selected_type is ContentType.JSON:
            try:
                units = _JSONStructureParser(parse_text, self.config).parse()
            except (ValueError, json.JSONDecodeError, IngestionLimitError) as error:
                if isinstance(error, IngestionLimitError):
                    raise
                units, parse_error = [], f"json parser fallback: {type(error).__name__}"
        elif selected_type is ContentType.YAML:
            units = _yaml_units(parse_text, self.config)
        elif selected_type is ContentType.TOML:
            units, parse_error = _toml_units(parse_text)
        elif selected_type is ContentType.LOG:
            units = _log_units(parse_text)
        elif selected_type is ContentType.TEXT:
            units = _markdown_units(parse_text)
        else:
            units = []
        if not units:
            strategy = "fixed_size"
            units = [
                _Unit(
                    0,
                    len(text),
                    "fixed",
                    {
                        "fallback": True,
                        "fallback_reason": parse_error or "no structural boundaries",
                    },
                )
            ] if text else []
        elif bom_offset:
            units = [
                _Unit(unit.start + bom_offset, unit.end + bom_offset, unit.kind, unit.metadata)
                for unit in units
            ]
        return units, parse_error, strategy

    def _split_ranges(self, text: str, start: int, end: int) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        cursor = start
        while cursor < end:
            candidate_end = min(end, cursor + self.config.max_chunk_chars)
            while candidate_end > cursor + 1 and len(text[cursor:candidate_end].encode("utf-8")) > self.config.max_chunk_bytes:
                candidate_end -= max(1, (len(text[cursor:candidate_end].encode("utf-8")) - self.config.max_chunk_bytes) // 4)
            if candidate_end <= cursor:
                candidate_end = min(end, cursor + 1)
            ranges.append((cursor, candidate_end))
            cursor = candidate_end
        return ranges

    def _chunks_from_units(
        self,
        text: str,
        units: list[_Unit],
        selected_type: ContentType,
        strategy: str,
        artifact_id: str,
    ) -> list[Chunk]:
        if not text:
            return []
        # Stable source order: enclosing structural units precede nested ones.
        ordered: list[_Unit] = []
        seen: set[tuple[int, int, str]] = set()
        for unit in sorted(units, key=lambda item: (item.start, -(item.end - item.start), item.kind)):
            if unit.start < 0 or unit.end > len(text) or unit.end <= unit.start:
                continue
            identity = (unit.start, unit.end, unit.kind)
            if identity in seen:
                continue
            seen.add(identity)
            ranges = self._split_ranges(text, unit.start, unit.end)
            for fragment_index, (start, end) in enumerate(ranges):
                metadata = dict(unit.metadata)
                metadata.update(
                    {
                        "content_type": selected_type.value,
                        "artifact_id": artifact_id,
                        "strategy": strategy,
                        "byte_start": len(text[:start].encode("utf-8")),
                        "byte_end": len(text[:end].encode("utf-8")),
                        "start_offset": start,
                        "end_offset": end,
                        "line_start": text.count("\n", 0, start) + 1,
                        "line_end": text.count("\n", 0, end) + 1,
                    }
                )
                if len(ranges) > 1:
                    metadata.update({"fixed_size_fallback": True, "fragment_index": fragment_index, "fragment_count": len(ranges), "parent_kind": unit.kind})
                if selected_type is ContentType.LOG:
                    metadata.setdefault("event_type", metadata.get("event", "record"))
                    metadata.setdefault("severity", metadata.get("event", "record"))
                    if unit.kind == "error":
                        metadata.update(
                            {
                                "is_error": True,
                                "error_start_offset": start,
                                "error_end_offset": end,
                                "error_boundary_start": start,
                                "error_boundary_end": end,
                            }
                        )
                ordered.append(_Unit(start, end, unit.kind, metadata))
                if len(ordered) > self.config.max_chunks:
                    raise IngestionLimitError("chunk count exceeds the configured limit.")
        if not ordered:
            ordered = [_Unit(*range_, "fixed", {"fallback": True}) for range_ in self._split_ranges(text, 0, len(text))]
        result: list[Chunk] = []
        for order, unit in enumerate(ordered):
            chunk_id = f"chunk-{order + 1:06d}"
            metadata = dict(unit.metadata)
            metadata.update({"chunk_id": chunk_id, "order": order})
            result.append(Chunk(chunk_id, unit.kind, text[unit.start : unit.end], unit.start, unit.end, order, metadata))
        return result


# Public aliases keep the component easy to discover without duplicating code.
IngestionService = Ingestor
DocumentIngestor = Ingestor
Chunker = Ingestor
ArtifactIngestor = Ingestor
IngestionPipeline = Ingestor
DocumentChunk = Chunk
IngestedDocument = IngestionResult


def ingest_document(
    source: bytes | bytearray | memoryview | str | Path,
    artifact_store: ArtifactStore,
    **kwargs: Any,
) -> IngestionResult:
    return Ingestor(artifact_store).ingest(source, **kwargs)


ingest = ingest_document


__all__ = [
    "Chunk",
    "Chunker",
    "ContentType",
    "ArtifactIngestor",
    "DocumentChunk",
    "DocumentIngestor",
    "DocumentType",
    "IngestionConfig",
    "IngestionError",
    "IngestionEvent",
    "IngestionLimitError",
    "IngestionPipeline",
    "IngestionResult",
    "IngestedDocument",
    "IngestionService",
    "Ingestor",
    "detect_content_type",
    "detect_type",
    "ingest_document",
    "ingest",
]
