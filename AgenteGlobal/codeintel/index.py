"""Persistent incremental code index and compact search primitives."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from .models import (
    CODE_INDEX_SCHEMA_VERSION,
    CodeIndexSnapshot,
    Confidence,
    EvidenceRef,
    FileRecord,
    ImportRecord,
    IndexMetadata,
    IndexRunMetrics,
    ParseStatus,
    ReferenceRecord,
    RelationshipRecord,
    SymbolRecord,
)
from .parsers import LANGUAGE_BY_SUFFIX, ParseResult, SourceAnalyzer, language_for


DEFAULT_INDEX_PATH = Path(".agenteglobal") / "codeintel" / "index.json"
DEFAULT_EXCLUDED_DIRECTORIES = frozenset(
    {".git", ".venv", "venv", "node_modules", ".agenteglobal", "__pycache__", ".terraform"}
)
DEFAULT_MAX_FILES = 100_000
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_SEARCH_RESULTS = 100
MAX_INDEX_BYTES = 128 * 1024 * 1024
INDEX_REPLACE_RETRIES = 3


class CodeIndexError(RuntimeError):
    pass


class CodeIndexCorruptError(CodeIndexError):
    pass


def _workspace_fingerprint(root: Path) -> str:
    return hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CodeIndex:
    """Validated in-memory projection backed by a rebuildable JSON cache."""

    def __init__(self, root: Path | str, snapshot: CodeIndexSnapshot | None = None) -> None:
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(f"Code index root is not a directory: {self.root}")
        fingerprint = _workspace_fingerprint(self.root)
        if snapshot is None:
            now = _now()
            snapshot = CodeIndexSnapshot(
                metadata=IndexMetadata(
                    workspace_fingerprint=fingerprint,
                    created_at=now,
                    updated_at=now,
                    file_count=0,
                    symbol_count=0,
                    relationship_count=0,
                    last_build_seconds=0.0,
                )
            )
        if snapshot.metadata.workspace_fingerprint != fingerprint:
            raise CodeIndexCorruptError("Persisted index belongs to another workspace")
        self.snapshot = snapshot

    @property
    def files(self) -> dict[str, FileRecord]:
        return self.snapshot.files

    @property
    def metadata(self) -> IndexMetadata:
        return self.snapshot.metadata

    def all_symbols(self) -> tuple[SymbolRecord, ...]:
        return tuple(symbol for record in self.files.values() for symbol in record.symbols)

    def all_relationships(self) -> tuple[RelationshipRecord, ...]:
        return tuple(relation for record in self.files.values() for relation in record.relationships)

    def find_symbol(self, name: str, *, limit: int = DEFAULT_SEARCH_RESULTS) -> tuple[SymbolRecord, ...]:
        needle = name.casefold().strip()
        if not needle:
            return ()
        exact = [
            symbol
            for symbol in self.all_symbols()
            if symbol.name.casefold() == needle or symbol.qualified_name.casefold() == needle
        ]
        return tuple(sorted(exact, key=lambda item: (item.path, item.start_line))[:limit])

    def search_symbols(self, query: str, *, limit: int = DEFAULT_SEARCH_RESULTS) -> tuple[SymbolRecord, ...]:
        needle = query.casefold().strip()
        if not needle:
            return ()
        matches = [
            symbol
            for symbol in self.all_symbols()
            if needle in symbol.name.casefold() or needle in symbol.qualified_name.casefold()
        ]
        return tuple(sorted(matches, key=lambda item: (item.name.casefold(), item.path, item.start_line))[:limit])

    def find_definitions(self, name: str, *, limit: int = DEFAULT_SEARCH_RESULTS) -> tuple[SymbolRecord, ...]:
        return self.find_symbol(name, limit=limit)

    def find_references(self, name: str, *, limit: int = DEFAULT_SEARCH_RESULTS) -> tuple[ReferenceRecord, ...]:
        needle = name.casefold().strip()
        matches = [
            reference
            for record in self.files.values()
            for reference in record.references
            if reference.name.casefold() == needle
        ]
        return tuple(sorted(matches, key=lambda item: (item.path, item.start_line))[:limit])

    def find_importers(self, module: str, *, limit: int = DEFAULT_SEARCH_RESULTS) -> tuple[ImportRecord, ...]:
        needle = module.casefold().strip()
        matches = [
            imported
            for record in self.files.values()
            for imported in record.imports
            if imported.module.casefold() == needle or imported.module.casefold().endswith("." + needle)
        ]
        return tuple(sorted(matches, key=lambda item: (item.path, item.start_line))[:limit])

    def find_dependencies(self, path: str) -> tuple[str, ...]:
        record = self.files.get(path.replace("\\", "/"))
        if record is None:
            return ()
        return tuple(sorted({item.module for item in record.imports if item.module}))

    def find_related_symbols(
        self,
        symbol: str,
        *,
        limit: int = DEFAULT_SEARCH_RESULTS,
    ) -> tuple[RelationshipRecord, ...]:
        needle = symbol.casefold().strip()
        matches = [
            relation
            for relation in self.all_relationships()
            if relation.source_symbol.casefold() == needle or relation.target_symbol.casefold() == needle
        ]
        return tuple(
            sorted(matches, key=lambda item: (item.kind.value, item.source_symbol, item.target_symbol))[:limit]
        )

    def search_files(self, query: str, *, limit: int = DEFAULT_SEARCH_RESULTS) -> tuple[FileRecord, ...]:
        needle = query.casefold().strip()
        if not needle:
            return ()
        return tuple(record for path, record in sorted(self.files.items()) if needle in path.casefold())[:limit]

    def lexical_search(self, query: str, *, limit: int = DEFAULT_SEARCH_RESULTS) -> tuple[EvidenceRef, ...]:
        if not query or not query.strip():
            return ()
        rg = shutil.which("rg")
        if rg:
            command = [
                rg,
                "--json",
                "--fixed-strings",
                "--color",
                "never",
                "--glob",
                "!.git/**",
                "--glob",
                "!.venv/**",
                "--glob",
                "!.agenteglobal/**",
                "--",
                query,
                ".",
            ]
            try:
                result = subprocess.run(
                    command,
                    cwd=self.root,
                    capture_output=True,
                    check=False,
                    timeout=10.0,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except (OSError, subprocess.TimeoutExpired):
                result = None
            if result is not None and result.returncode in {0, 1}:
                hits: list[EvidenceRef] = []
                for line in result.stdout.splitlines():
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if item.get("type") != "match":
                        continue
                    data = item.get("data", {})
                    path = str(data.get("path", {}).get("text", "")).replace("\\", "/").removeprefix("./")
                    line_number = int(data.get("line_number") or 1)
                    if path in self.files:
                        hits.append(
                            EvidenceRef(
                                path=path,
                                start_line=line_number,
                                end_line=line_number,
                                source="lexical",
                                confidence=Confidence.CONFIRMED,
                            )
                        )
                    if len(hits) >= limit:
                        break
                return tuple(hits)
        # Bounded fallback over files already admitted into the index.
        matches: list[EvidenceRef] = []
        for path in sorted(self.files):
            candidate = self.root / Path(path)
            try:
                with candidate.open("r", encoding="utf-8", errors="replace") as stream:
                    for line_number, line in enumerate(stream, 1):
                        if query in line:
                            matches.append(
                                EvidenceRef(
                                    path=path,
                                    start_line=line_number,
                                    end_line=line_number,
                                    source="lexical",
                                    confidence=Confidence.CONFIRMED,
                                )
                            )
                            if len(matches) >= limit:
                                return tuple(matches)
            except OSError:
                continue
        return tuple(matches)


class CodeIndexer:
    """Incrementally refresh only changed records and atomically persist them."""

    def __init__(
        self,
        root: Path | str,
        *,
        index_path: Path | str = DEFAULT_INDEX_PATH,
        analyzer: SourceAnalyzer | None = None,
        excluded_directories: Iterable[str] = DEFAULT_EXCLUDED_DIRECTORIES,
        max_files: int = DEFAULT_MAX_FILES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(f"Code index root is not a directory: {self.root}")
        self.index_path = self._safe_index_path(index_path)
        self.analyzer = analyzer or SourceAnalyzer()
        self.excluded_directories = frozenset(excluded_directories)
        if max_files < 1 or max_file_bytes < 1:
            raise ValueError("index limits must be positive")
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes

    def _safe_index_path(self, value: Path | str) -> Path:
        relative = Path(value)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            raise ValueError("index path must be workspace-relative")
        candidate = (self.root / relative).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise ValueError("index path escapes workspace") from error
        return candidate

    def _load(self) -> tuple[CodeIndex, bool]:
        candidates = [self.index_path]
        candidates.extend(self.index_path.parent.glob(f"{self.index_path.stem}.generation-*.json"))
        candidates = [path for path in candidates if path.exists()]
        if not candidates:
            return CodeIndex(self.root), False
        valid: list[CodeIndex] = []
        for candidate in candidates:
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                if candidate.stat().st_size > MAX_INDEX_BYTES:
                    continue
                snapshot = CodeIndexSnapshot.model_validate_json(candidate.read_bytes())
                valid.append(CodeIndex(self.root, snapshot))
            except (OSError, ValidationError, ValueError, CodeIndexError):
                continue
        if not valid:
            return CodeIndex(self.root), True
        # Cloud-sync metadata can make filesystem mtimes newer than the content.
        # The validated snapshot timestamp is the canonical generation order.
        return max(valid, key=lambda item: item.metadata.updated_at), False

    def load(self) -> CodeIndex:
        index, rebuilt = self._load()
        if rebuilt:
            raise CodeIndexCorruptError("index cache is corrupt and must be rebuilt")
        return index

    def _iter_files(self) -> Iterable[Path]:
        count = 0
        for directory, names, files in os.walk(self.root, topdown=True, followlinks=False):
            current = Path(directory)
            names[:] = sorted(
                name
                for name in names
                if name not in self.excluded_directories and not (current / name).is_symlink()
            )
            for name in sorted(files):
                candidate = current / name
                if candidate.is_symlink() or candidate.suffix.lower() not in LANGUAGE_BY_SUFFIX:
                    continue
                count += 1
                if count > self.max_files:
                    raise CodeIndexError(f"repository exceeds configured max_files={self.max_files}")
                yield candidate

    def _record(self, path: Path, *, raw: bytes, stat: os.stat_result) -> FileRecord:
        relative = path.relative_to(self.root).as_posix()
        digest = hashlib.sha256(raw).hexdigest()
        language = language_for(path)
        if len(raw) > self.max_file_bytes:
            parsed = ParseResult(
                status=ParseStatus.SKIPPED_LARGE,
                source="metadata",
                error=f"file exceeds max_file_bytes={self.max_file_bytes}",
            )
        else:
            content = raw.decode("utf-8", errors="replace")
            parsed = self.analyzer.parse(relative, content, language)
        return FileRecord(
            path=relative,
            language=language,
            size=len(raw),
            content_hash=digest,
            mtime_ns=max(0, int(stat.st_mtime_ns)),
            symbols=parsed.symbols,
            imports=parsed.imports,
            references=parsed.references,
            relationships=parsed.relationships,
            parse_status=parsed.status,
            parser_source=parsed.source,
            parse_error=parsed.error,
        )

    def _sync_path(self, value: Path | str) -> tuple[Path, str]:
        """Resolve one source path without following a symlink supplied by a caller."""

        raw = Path(value)
        lexical = raw if raw.is_absolute() else self.root / raw
        candidate = lexical.resolve(strict=False)
        try:
            relative = candidate.relative_to(self.root).as_posix()
        except ValueError as error:
            raise ValueError("sync path resolves outside workspace") from error

        # ``Path.resolve`` can canonicalize Windows short names and would make
        # an in-workspace symlink appear to be its target.  Walk the original
        # lexical path separately, bounded by its component count, so a
        # symlinked file or parent is still rejected without relying on string
        # equality between long and 8.3 path forms.
        current = lexical
        for _ in range(len(lexical.parts) + 2):
            if current.is_symlink():
                raise CodeIndexError("refusing symlinked sync path")
            if current.resolve(strict=False) == self.root:
                break
            parent = current.parent
            if parent == current:
                raise ValueError("sync path escapes workspace")
            current = parent
        else:
            raise ValueError("sync path escapes workspace")
        return candidate, relative

    def sync_file(
        self,
        path: Path | str,
        *,
        index: CodeIndex | None = None,
    ) -> tuple[CodeIndex, IndexRunMetrics]:
        """Update one file record and persist the resulting snapshot atomically.

        This operation deliberately does not call :meth:`_iter_files`.  It is
        used after a successful workspace write, where a full tree scan would
        make the write callback depend on the size of the repository.  Missing
        and unsupported paths remove an existing record; a read or persistence
        failure raises while leaving the supplied in-memory snapshot untouched.
        """

        started = time.perf_counter()
        target, relative = self._sync_path(path)
        if index is None:
            loaded, cache_corrupt = self._load()
            if cache_corrupt:
                raise CodeIndexCorruptError("index cache is corrupt and must be rebuilt")
            index = loaded
        elif index.root != self.root:
            raise ValueError("index belongs to another workspace")

        previous = index.files
        prior = previous.get(relative)
        updated = dict(previous)
        scanned = indexed = unchanged = new = modified = deleted = skipped = 0
        changed = False

        # Excluded directories are never admitted by the full scanner.  Treat a
        # direct sync request for one as a removal to keep cache state coherent.
        excluded = any(part in self.excluded_directories for part in Path(relative).parts[:-1])
        supported = target.suffix.lower() in LANGUAGE_BY_SUFFIX
        if excluded or not supported or target.is_symlink() or not target.is_file():
            if prior is not None:
                del updated[relative]
                deleted = 1
                changed = True
            else:
                skipped = 1
        else:
            scanned = 1
            try:
                stat = target.stat()
                raw = target.read_bytes()
            except FileNotFoundError:
                if prior is not None:
                    del updated[relative]
                    deleted = 1
                    changed = True
                else:
                    skipped = 1
            except OSError as error:
                raise CodeIndexError(f"unable to read sync path {relative}: {type(error).__name__}") from error
            else:
                digest = hashlib.sha256(raw).hexdigest()
                if prior is not None and prior.content_hash == digest:
                    candidate_record = prior
                    if prior.size != len(raw) or prior.mtime_ns != stat.st_mtime_ns:
                        candidate_record = prior.model_copy(
                            update={"size": len(raw), "mtime_ns": max(0, int(stat.st_mtime_ns))}
                        )
                    updated[relative] = candidate_record
                    unchanged = 1
                    changed = candidate_record != prior
                else:
                    candidate_record = self._record(target, raw=raw, stat=stat)
                    updated[relative] = candidate_record
                    indexed = 1
                    changed = True
                    if prior is None:
                        new = 1
                    else:
                        modified = 1

        duration = max(0.0, time.perf_counter() - started)
        if changed:
            now = _now()
            symbol_count = sum(len(record.symbols) for record in updated.values())
            relationship_count = sum(len(record.relationships) for record in updated.values())
            snapshot = CodeIndexSnapshot(
                metadata=IndexMetadata(
                    schema_version=CODE_INDEX_SCHEMA_VERSION,
                    workspace_fingerprint=_workspace_fingerprint(self.root),
                    created_at=index.metadata.created_at,
                    updated_at=now,
                    file_count=len(updated),
                    symbol_count=symbol_count,
                    relationship_count=relationship_count,
                    last_build_seconds=duration,
                ),
                files=updated,
            )
            candidate = CodeIndex(self.root, snapshot)
            # Publish only after fsync + atomic replace.  A failed or oversized
            # serialization therefore cannot leave memory newer than disk.
            self._persist(candidate)
            index.snapshot = candidate.snapshot
        else:
            symbol_count = index.metadata.symbol_count
            relationship_count = index.metadata.relationship_count

        metrics = IndexRunMetrics(
            duration_seconds=duration,
            scanned_files=scanned,
            indexed_files=indexed,
            unchanged_files=unchanged,
            new_files=new,
            modified_files=modified,
            deleted_files=deleted,
            skipped_files=skipped,
            symbols_indexed=symbol_count,
            relationships_indexed=relationship_count,
            cache_hits=unchanged,
            cache_misses=indexed,
            rebuilt=False,
            details={"mode": "incremental", "path": relative},
        )
        return index, metrics

    def _persist(self, index: CodeIndex) -> None:
        parent = self.index_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or self.index_path.is_symlink():
            raise CodeIndexError("refusing symlinked index cache")
        payload = index.snapshot.model_dump_json(indent=2).encode("utf-8") + b"\n"
        if len(payload) > MAX_INDEX_BYTES:
            raise CodeIndexError("serialized index exceeds safe size")
        descriptor, temporary = tempfile.mkstemp(prefix=".code-index.", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            replace_error: PermissionError | None = None
            published = self.index_path
            for attempt in range(INDEX_REPLACE_RETRIES):
                try:
                    os.replace(temporary, self.index_path)
                    replace_error = None
                    break
                except PermissionError as exc:
                    replace_error = exc
                    time.sleep(0.05 * (attempt + 1))
            if replace_error is not None:
                # OneDrive may expose an existing hydrated file as a reparse point
                # that MoveFileEx cannot replace. Publish a uniquely named atomic
                # generation, then best-effort prune older rebuildable caches.
                generation = parent / (
                    f"{self.index_path.stem}.generation-{time.time_ns()}-{os.getpid()}.json"
                )
                os.replace(temporary, generation)
                published = generation
            for candidate in [self.index_path, *parent.glob(f"{self.index_path.stem}.generation-*.json")]:
                if candidate == published or candidate.is_symlink():
                    continue
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def build(self, *, force_rebuild: bool = False) -> tuple[CodeIndex, IndexRunMetrics]:
        started = time.perf_counter()
        if force_rebuild:
            index, rebuilt = CodeIndex(self.root), True
        else:
            index, rebuilt = self._load()
        previous = dict(index.files)
        updated: dict[str, FileRecord] = {}
        scanned = indexed = unchanged = new = modified = skipped = 0
        for path in self._iter_files():
            scanned += 1
            relative = path.relative_to(self.root).as_posix()
            try:
                stat = path.stat()
                raw = path.read_bytes()
            except OSError:
                skipped += 1
                continue
            digest = hashlib.sha256(raw).hexdigest()
            prior = previous.get(relative)
            if prior is not None and prior.content_hash == digest:
                # Metadata can change without requiring another parse.
                if prior.size != len(raw) or prior.mtime_ns != stat.st_mtime_ns:
                    prior = prior.model_copy(update={"size": len(raw), "mtime_ns": max(0, int(stat.st_mtime_ns))})
                updated[relative] = prior
                unchanged += 1
                continue
            record = self._record(path, raw=raw, stat=stat)
            updated[relative] = record
            indexed += 1
            if prior is None:
                new += 1
            else:
                modified += 1
        deleted = len(set(previous) - set(updated))
        now = _now()
        duration = max(0.0, time.perf_counter() - started)
        symbol_count = sum(len(record.symbols) for record in updated.values())
        relationship_count = sum(len(record.relationships) for record in updated.values())
        snapshot = CodeIndexSnapshot(
            metadata=IndexMetadata(
                schema_version=CODE_INDEX_SCHEMA_VERSION,
                workspace_fingerprint=_workspace_fingerprint(self.root),
                created_at=index.metadata.created_at if not rebuilt else now,
                updated_at=now,
                file_count=len(updated),
                symbol_count=symbol_count,
                relationship_count=relationship_count,
                last_build_seconds=duration,
            ),
            files=updated,
        )
        candidate = CodeIndex(self.root, snapshot)
        # Keep the in-memory object transactional with the durable cache.
        self._persist(candidate)
        index.snapshot = candidate.snapshot
        metrics = IndexRunMetrics(
            duration_seconds=duration,
            scanned_files=scanned,
            indexed_files=indexed,
            unchanged_files=unchanged,
            new_files=new,
            modified_files=modified,
            deleted_files=deleted,
            skipped_files=skipped,
            symbols_indexed=symbol_count,
            relationships_indexed=relationship_count,
            cache_hits=unchanged,
            cache_misses=indexed,
            rebuilt=rebuilt,
            details={"tree_sitter_available": self.analyzer.tree_sitter.available},
        )
        return index, metrics


__all__ = [
    "CodeIndex",
    "CodeIndexCorruptError",
    "CodeIndexError",
    "CodeIndexer",
    "DEFAULT_EXCLUDED_DIRECTORIES",
    "DEFAULT_INDEX_PATH",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_FILES",
]
