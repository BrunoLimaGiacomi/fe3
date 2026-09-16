"""Small, read-only adapter for an existing GitHub Spec Kit layout.

This module deliberately recognises the files that Spec Kit already creates;
it does not create a second specification framework.  Discovery is local and
bounded.  A caller can use the high-priority context as one input to
``ContextBudget`` while keeping plans and task lists out of that context.

The adapter is intentionally not a workflow engine.  Later-phase orchestration
behaviour remains outside this foundation.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


# These ceilings are safety boundaries, rather than assumptions about the
# size of a Spec Kit repository.  The adapter reads a bounded prefix of a
# larger file and records the original size and truncation state.
DEFAULT_MAX_FILE_BYTES = 256 * 1024
DEFAULT_MAX_CONTENT_CHARS = 12_000
DEFAULT_MAX_CONTEXT_CHARS = 48_000
DEFAULT_MAX_DOCUMENTS = 256
DEFAULT_MAX_SCAN_ENTRIES = 512
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SCAN_ENTRIES = 10_000
MAX_DOCUMENTS = 10_000
MAX_DOCUMENT_CONTENT_CHARS = 64_000
MAX_CONTEXT_CHARS = 256_000


class SpecKitError(ValueError):
    """Base error for invalid local Spec Kit input."""


class SpecKitPathError(SpecKitError):
    """A requested path is outside the workspace or uses a symlink."""


class SpecKitReadError(SpecKitError):
    """A recognised local document cannot be read safely."""


class SpecKitCategory(StrEnum):
    """Semantic role of a recognised Spec Kit document.

    The roles are intentionally explicit: specification is WHAT/WHY, plan is
    HOW, and tasks are executable units.  Constitution and checklist files
    provide governance and verification context respectively.
    """

    CONSTITUTION = "constitution"
    SPECIFICATION = "specification"
    PLAN = "plan"
    TASKS = "tasks"
    CHECKLIST = "checklist"

    # Compatibility spellings useful to callers without adding categories.
    SPEC = "specification"
    TASK = "tasks"
    CHECKLISTS = "checklist"

    @property
    def purpose(self) -> str:
        return {
            SpecKitCategory.CONSTITUTION: "GOVERNANCE",
            SpecKitCategory.SPECIFICATION: "WHAT/WHY",
            SpecKitCategory.PLAN: "HOW",
            SpecKitCategory.TASKS: "EXECUTION UNITS",
            SpecKitCategory.CHECKLIST: "VERIFICATION",
        }[self]


class SpecKitPriority(StrEnum):
    """Priority used when selecting bounded context for a model request."""

    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

    # ``MEDIUM`` is a naming alias, not a separate priority level.
    MEDIUM = "normal"
    REQUIRED = "high"


HIGH_PRIORITY_CATEGORIES = frozenset(
    {
        SpecKitCategory.CONSTITUTION,
        SpecKitCategory.SPECIFICATION,
        SpecKitCategory.CHECKLIST,
    }
)


def _coerce_category(value: SpecKitCategory | str) -> SpecKitCategory:
    try:
        return value if isinstance(value, SpecKitCategory) else SpecKitCategory(str(value))
    except (TypeError, ValueError) as error:
        raise SpecKitError(f"Categoria Spec Kit desconhecida: {value!r}.") from error


def _coerce_priority(value: SpecKitPriority | str) -> SpecKitPriority:
    try:
        return value if isinstance(value, SpecKitPriority) else SpecKitPriority(str(value))
    except (TypeError, ValueError) as error:
        raise SpecKitError(f"Prioridade Spec Kit desconhecida: {value!r}.") from error


def _positive_limit(name: str, value: int, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} deve ser um inteiro positivo.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} não pode exceder {maximum}.")
    return value


def _relative_text(path: Path) -> str:
    """Return a stable, platform-neutral relative path representation."""

    return path.as_posix()


@dataclass(frozen=True, slots=True)
class SpecKitDocument:
    """One bounded, traceable document read from the local workspace."""

    category: SpecKitCategory
    priority: SpecKitPriority
    path: Path
    relative_path: str
    content: str
    size_bytes: int
    feature: str | None = None
    content_truncated: bool = False
    size_limited: bool = False

    def __post_init__(self) -> None:
        category = _coerce_category(self.category)
        priority = _coerce_priority(self.priority)
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise SpecKitPathError("O caminho do documento deve ser absoluto.")
        if (
            not isinstance(self.relative_path, str)
            or not self.relative_path
            or "\x00" in self.relative_path
        ):
            raise SpecKitPathError("relative_path inválido.")
        relative = Path(self.relative_path.replace("/", os.sep))
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            raise SpecKitPathError("relative_path deve permanecer dentro do workspace.")
        if not isinstance(self.content, str):
            raise TypeError("content deve ser str.")
        if len(self.content) > MAX_DOCUMENT_CONTENT_CHARS:
            raise ValueError(
                f"content não pode exceder {MAX_DOCUMENT_CONTENT_CHARS} caracteres."
            )
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError("size_bytes deve ser um inteiro não negativo.")
        if self.feature is not None and not isinstance(self.feature, str):
            raise TypeError("feature deve ser str ou None.")
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "priority", priority)

    @property
    def source_path(self) -> str:
        """Relative source path suitable for telemetry and prompt metadata."""

        return self.relative_path

    @property
    def size(self) -> int:
        return self.size_bytes

    @property
    def bounded_content(self) -> str:
        return self.content

    @property
    def is_truncated(self) -> bool:
        return self.content_truncated or self.size_limited

    @property
    def purpose(self) -> str:
        return self.category.purpose

    @property
    def role(self) -> str:
        return self.purpose

    @property
    def kind(self) -> str:
        return self.category.value

    @property
    def document_type(self) -> str:
        return self.category.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "priority": self.priority.value,
            "path": self.relative_path,
            "relative_path": self.relative_path,
            "feature": self.feature,
            "purpose": self.purpose,
            "content": self.content,
            "size_bytes": self.size_bytes,
            "content_truncated": self.content_truncated,
            "size_limited": self.size_limited,
        }


@dataclass(frozen=True, slots=True)
class SpecKitSkippedPath:
    """Metadata-only reason why a candidate was not read."""

    relative_path: str
    reason: str
    size_bytes: int | None = None
    symlink: bool = False

    @property
    def path(self) -> str:
        return self.relative_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "relative_path": self.relative_path,
            "reason": self.reason,
            "size_bytes": self.size_bytes,
            "symlink": self.symlink,
        }


@dataclass(frozen=True, slots=True)
class SpecKitContext:
    """High-priority, bounded context prepared for ``ContextBudget``."""

    documents: tuple[SpecKitDocument, ...] = ()
    content: str = ""
    max_chars: int = DEFAULT_MAX_CONTEXT_CHARS
    truncated: bool = False
    omitted_documents: int = 0

    def __post_init__(self) -> None:
        documents = tuple(self.documents)
        if any(not isinstance(document, SpecKitDocument) for document in documents):
            raise TypeError("documents deve conter somente SpecKitDocument.")
        if any(document.priority is not SpecKitPriority.HIGH for document in documents):
            raise SpecKitError("SpecKitContext aceita somente documentos de prioridade HIGH.")
        _positive_limit("max_chars", self.max_chars, maximum=MAX_CONTEXT_CHARS)
        if not isinstance(self.content, str):
            raise TypeError("content deve ser str.")
        if len(self.content) > self.max_chars:
            raise ValueError("content excede max_chars.")
        if isinstance(self.omitted_documents, bool) or not isinstance(self.omitted_documents, int):
            raise TypeError("omitted_documents deve ser inteiro.")
        if self.omitted_documents < 0:
            raise ValueError("omitted_documents não pode ser negativo.")
        object.__setattr__(self, "documents", documents)

    @property
    def sources(self) -> tuple[SpecKitDocument, ...]:
        return self.documents

    @property
    def high_priority_documents(self) -> tuple[SpecKitDocument, ...]:
        return self.documents

    @property
    def text(self) -> str:
        return self.content

    def to_prompt(self) -> str:
        return self.content

    render = to_prompt

    def add_to_budget(self, budget: Any) -> Any:
        """Set only the existing constitution/spec budget category.

        Importing ``ContextBudget`` is intentionally avoided: any compatible
        budget object exposing ``set`` can be used, while the repository's
        implementation accepts the ``BudgetCategory`` enum below.
        """

        try:
            from .context_budget import BudgetCategory
        except ImportError:  # top-level ``runtime`` imports used by the CLI
            from runtime.context_budget import BudgetCategory
        if budget is None or not callable(getattr(budget, "set", None)):
            raise TypeError("budget deve expor set(category, value).")
        budget.set(BudgetCategory.CONSTITUTION_SPEC, self.content)
        return budget

    to_budget = add_to_budget

    def to_dict(self) -> dict[str, Any]:
        return {
            "priority": SpecKitPriority.HIGH.value,
            "documents": [document.to_dict() for document in self.documents],
            "content": self.content,
            "max_chars": self.max_chars,
            "truncated": self.truncated,
            "omitted_documents": self.omitted_documents,
        }

    def __str__(self) -> str:
        return self.content


@dataclass(frozen=True, slots=True)
class SpecKitScan:
    """Immutable result of one bounded local discovery pass."""

    workspace: Path
    documents: tuple[SpecKitDocument, ...] = ()
    skipped: tuple[SpecKitSkippedPath, ...] = ()
    has_specify: bool = False
    has_specs: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Path) or not self.workspace.is_absolute():
            raise SpecKitPathError("workspace deve ser um caminho absoluto.")
        documents = tuple(self.documents)
        skipped = tuple(self.skipped)
        if any(not isinstance(item, SpecKitDocument) for item in documents):
            raise TypeError("documents inválidos.")
        if any(not isinstance(item, SpecKitSkippedPath) for item in skipped):
            raise TypeError("skipped inválido.")
        object.__setattr__(self, "documents", documents)
        object.__setattr__(self, "skipped", skipped)

    @property
    def recognized(self) -> bool:
        return bool(self.documents or self.has_specify or self.has_specs)

    @property
    def official_layout(self) -> bool:
        return self.recognized

    @property
    def is_spec_kit(self) -> bool:
        return self.recognized

    @property
    def high_priority_documents(self) -> tuple[SpecKitDocument, ...]:
        return tuple(
            document
            for document in self.documents
            if document.priority is SpecKitPriority.HIGH
        )

    @property
    def sources(self) -> tuple[SpecKitDocument, ...]:
        return self.documents

    def __iter__(self) -> Iterator[SpecKitDocument]:
        return iter(self.documents)

    def __len__(self) -> int:
        return len(self.documents)

    def __getitem__(self, index: int | slice) -> SpecKitDocument | tuple[SpecKitDocument, ...]:
        return self.documents[index]

    def to_context(self, *, max_chars: int = DEFAULT_MAX_CONTEXT_CHARS) -> SpecKitContext:
        return _build_high_priority_context(self.high_priority_documents, max_chars=max_chars)

    high_priority_context = to_context

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "recognized": self.recognized,
            "has_specify": self.has_specify,
            "has_specs": self.has_specs,
            "documents": [document.to_dict() for document in self.documents],
            "skipped": [item.to_dict() for item in self.skipped],
        }


_CONSTITUTION_WORD = "constitution"
_SPECIFY_DIRECTORY = ".specify"
_MEMORY_DIRECTORY = "memory"
_SPECS_DIRECTORY = "specs"
_CHECKLIST_DIRECTORIES = frozenset({"checklists", "checklist"})
_SPEC_NAMES = frozenset({"spec.md", "specification.md"})
_PLAN_NAMES = frozenset({"plan.md", "implementation-plan.md"})
_TASK_NAMES = frozenset({"tasks.md", "task-list.md"})
_ROOT_COMPAT_NAMES = frozenset(
    {
        "constitution.md",
        "constitutions.md",
        "project-constitution.md",
        "spec.md",
        "specification.md",
        "plan.md",
        "implementation-plan.md",
        "tasks.md",
        "task-list.md",
        "checklist.md",
    }
)


def _casefold_name(path: Path) -> str:
    return path.name.casefold()


def _is_constitution_name(path: Path) -> bool:
    return path.suffix.casefold() == ".md" and _CONSTITUTION_WORD in path.stem.casefold()


def _find_named(directory: Path, name: str) -> Path | None:
    """Find one immediate child by case-insensitive name without recursion."""

    try:
        children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return None
    expected = name.casefold()
    for child in children:
        if _casefold_name(child) == expected:
            return child
    return None


def _build_high_priority_context(
    documents: Sequence[SpecKitDocument], *, max_chars: int
) -> SpecKitContext:
    max_chars = _positive_limit("max_chars", max_chars, maximum=MAX_CONTEXT_CHARS)
    selected = tuple(
        sorted(
            (document for document in documents if document.priority is SpecKitPriority.HIGH),
            key=lambda item: (
                {
                    SpecKitCategory.CONSTITUTION: 0,
                    SpecKitCategory.SPECIFICATION: 1,
                    SpecKitCategory.CHECKLIST: 2,
                }.get(item.category, 9),
                item.relative_path.casefold(),
            ),
        )
    )
    if not selected:
        return SpecKitContext(max_chars=max_chars)
    prefix = '<spec_kit_context priority="high">\n'
    suffix = "</spec_kit_context>"
    if max_chars <= len(prefix) + len(suffix):
        return SpecKitContext(
            documents=(),
            content=(prefix + suffix)[:max_chars],
            max_chars=max_chars,
            truncated=bool(selected),
            omitted_documents=len(selected),
        )

    sections: list[str] = [prefix]
    remaining = max_chars - len(prefix) - len(suffix)
    included: list[SpecKitDocument] = []
    omitted = 0
    truncated = False
    for index, document in enumerate(selected):
        header = (
            f'<section category="{document.category.value}" '
            f'purpose="{document.purpose}" path="{document.relative_path}">\n'
        )
        footer = "\n</section>\n"
        section = header + document.content + footer
        if len(section) <= remaining:
            sections.append(section)
            remaining -= len(section)
            included.append(document)
            continue
        if remaining <= 0:
            omitted += 1
            truncated = True
            continue
        marker = "\n[conteúdo truncado]\n"
        body_limit = max(0, remaining - len(header) - len(footer) - len(marker))
        if body_limit > 0 and len(header) + body_limit + len(marker) + len(footer) <= remaining:
            sections.append(header + document.content[:body_limit] + marker + footer)
            included.append(document)
            remaining = 0
        else:
            # Do not emit a partial section whose metadata cannot fit.  The
            # context remains bounded even with an unusually small limit.
            omitted += 1
        truncated = True
        for later in selected[index + 1 :]:
            omitted += 1
        break
    sections.append(suffix)
    content = "".join(sections)
    if len(content) > max_chars:  # defensive guard for future formatting edits
        content = content[:max_chars]
        truncated = True
    return SpecKitContext(
        documents=tuple(included),
        content=content,
        max_chars=max_chars,
        truncated=truncated,
        omitted_documents=omitted,
    )


class SpecKitAdapter:
    """Discover and read existing Spec Kit files without writing anything."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
        max_documents: int = DEFAULT_MAX_DOCUMENTS,
        max_scan_entries: int = DEFAULT_MAX_SCAN_ENTRIES,
    ) -> None:
        try:
            resolved = Path(workspace).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise SpecKitPathError(f"Workspace inválido: {workspace!r}.") from error
        if not resolved.is_dir():
            raise SpecKitPathError("workspace deve ser um diretório existente.")
        self.workspace = resolved
        self.max_file_bytes = _positive_limit(
            "max_file_bytes", max_file_bytes, maximum=MAX_FILE_BYTES
        )
        self.max_content_chars = _positive_limit(
            "max_content_chars", max_content_chars, maximum=MAX_DOCUMENT_CONTENT_CHARS
        )
        self.max_context_chars = _positive_limit(
            "max_context_chars", max_context_chars, maximum=MAX_CONTEXT_CHARS
        )
        self.max_documents = _positive_limit(
            "max_documents", max_documents, maximum=MAX_DOCUMENTS
        )
        self.max_scan_entries = _positive_limit(
            "max_scan_entries", max_scan_entries, maximum=MAX_SCAN_ENTRIES
        )
        self._seen: set[str] = set()
        self._skipped: list[SpecKitSkippedPath] = []
        self._documents: list[SpecKitDocument] = []

    def _relative(self, candidate: Path) -> str:
        try:
            relative = candidate.relative_to(self.workspace)
        except ValueError as error:
            raise SpecKitPathError("O caminho deve permanecer dentro do workspace.") from error
        if not relative.parts or ".." in relative.parts:
            raise SpecKitPathError("O caminho relativo é inválido.")
        return _relative_text(relative)

    def _safe_candidate(self, candidate: Path, *, require_exists: bool = True) -> tuple[Path, str]:
        candidate = Path(candidate)
        if "\x00" in str(candidate):
            raise SpecKitPathError("O caminho não pode conter NUL.")
        if ".." in candidate.parts:
            raise SpecKitPathError("O caminho não pode conter o componente '..'.")
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        # Resolve aliases such as a Windows 8.3 path before comparing with
        # the resolved workspace.  Inspect the original lexical path first so
        # an in-bound symlink cannot disappear during that resolution.
        try:
            lexical = candidate
            while lexical != Path(lexical.anchor):
                if lexical.is_symlink():
                    raise SpecKitPathError(f"Symlink recusado: {candidate}.")
                lexical = lexical.parent
            resolved = candidate.resolve(strict=False)
        except SpecKitPathError:
            raise
        except (OSError, RuntimeError) as error:
            raise SpecKitPathError(f"Não foi possível validar o caminho: {candidate}.") from error
        relative = self._relative(resolved)
        current = self.workspace
        for part in Path(relative).parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise SpecKitPathError(f"Symlink recusado: {relative}.")
            except OSError as error:
                raise SpecKitPathError(f"Não foi possível validar o caminho: {relative}.") from error
        if require_exists and not resolved.exists():
            raise SpecKitReadError(f"Caminho não encontrado: {relative}.")
        try:
            resolved.relative_to(self.workspace)
        except (OSError, RuntimeError, ValueError) as error:
            raise SpecKitPathError(f"O caminho resolve fora do workspace: {relative}.") from error
        return resolved, relative

    def _record_skip(
        self,
        candidate: Path,
        reason: str,
        *,
        size_bytes: int | None = None,
        symlink: bool = False,
    ) -> None:
        try:
            relative = self._relative(candidate)
        except SpecKitPathError:
            relative = candidate.name
        self._skipped.append(
            SpecKitSkippedPath(
                relative_path=relative,
                reason=reason,
                size_bytes=size_bytes,
                symlink=symlink,
            )
        )

    def _directory(self, candidate: Path) -> Path | None:
        try:
            candidate, _ = self._safe_candidate(candidate)
        except SpecKitError as error:
            self._record_skip(
                candidate,
                str(error),
                symlink="symlink" in str(error).casefold(),
            )
            return None
        try:
            if candidate.is_symlink():
                self._record_skip(candidate, "symlink", symlink=True)
                return None
            mode = candidate.lstat().st_mode
            if not stat.S_ISDIR(mode):
                return None
        except OSError:
            self._record_skip(candidate, "unreadable")
            return None
        return candidate

    def _children(self, directory: Path) -> tuple[Path, ...]:
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            self._record_skip(directory, "unreadable")
            return ()
        if len(entries) > self.max_scan_entries:
            for entry in entries[self.max_scan_entries :]:
                self._record_skip(entry, "scan entry limit")
            entries = entries[: self.max_scan_entries]
        return tuple(entries)

    def _add_file(self, candidate: Path, category: SpecKitCategory, feature: str | None = None) -> None:
        if len(self._documents) >= self.max_documents:
            self._record_skip(candidate, "document limit")
            return
        try:
            candidate, relative = self._safe_candidate(candidate)
        except SpecKitError as error:
            self._record_skip(candidate, str(error), symlink="symlink" in str(error).casefold())
            return
        if relative.casefold() in self._seen:
            return
        try:
            if candidate.is_symlink():
                self._record_skip(candidate, "symlink", symlink=True)
                return
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode):
                self._record_skip(candidate, "not a regular file")
                return
            size_bytes = int(info.st_size)
            with candidate.open("rb") as stream:
                payload = stream.read(self.max_file_bytes + 1)
        except (OSError, UnicodeError) as error:
            self._record_skip(candidate, "unreadable")
            return
        size_limited = size_bytes > self.max_file_bytes or len(payload) > self.max_file_bytes
        if len(payload) > self.max_file_bytes:
            payload = payload[: self.max_file_bytes]
        decoded = payload.decode("utf-8", errors="replace")
        content_truncated = len(decoded) > self.max_content_chars
        if content_truncated:
            decoded = decoded[: self.max_content_chars]
        # Recheck the path after reading so a replacement by a symlink is not
        # silently treated as a normal source.  A concurrent file replacement
        # is reported as a skipped candidate on the next discovery pass.
        try:
            if candidate.is_symlink():
                self._record_skip(candidate, "symlink", symlink=True)
                return
        except OSError:
            self._record_skip(candidate, "unreadable")
            return
        document = SpecKitDocument(
            category=category,
            priority=(
                SpecKitPriority.HIGH
                if category in HIGH_PRIORITY_CATEGORIES
                else SpecKitPriority.NORMAL
            ),
            path=candidate.resolve(strict=True),
            relative_path=relative,
            content=decoded,
            size_bytes=size_bytes,
            feature=feature,
            content_truncated=content_truncated,
            size_limited=size_limited,
        )
        self._seen.add(relative.casefold())
        self._documents.append(document)

    def _scan_named_documents(self, directory: Path, feature: str | None = None) -> None:
        for child in self._children(directory):
            if not child.is_file() and not child.is_symlink():
                continue
            name = _casefold_name(child)
            if name in _SPEC_NAMES:
                self._add_file(child, SpecKitCategory.SPECIFICATION, feature)
            elif name in _PLAN_NAMES:
                self._add_file(child, SpecKitCategory.PLAN, feature)
            elif name in _TASK_NAMES:
                self._add_file(child, SpecKitCategory.TASKS, feature)
            elif name.startswith("checklist") and child.suffix.casefold() == ".md":
                self._add_file(child, SpecKitCategory.CHECKLIST, feature)

    def _scan_checklists(self, directory: Path, feature: str | None = None) -> None:
        children = self._children(directory)
        checklist_dir = next(
            (
                child
                for child in children
                if child.is_dir() and not child.is_symlink() and _casefold_name(child) in _CHECKLIST_DIRECTORIES
            ),
            None,
        )
        for child in children:
            if child.is_symlink() and _casefold_name(child) in _CHECKLIST_DIRECTORIES:
                self._record_skip(child, "symlink", symlink=True)
        if checklist_dir is None:
            return
        for child in self._children(checklist_dir):
            if (child.is_file() or child.is_symlink()) and child.suffix.casefold() == ".md":
                self._add_file(child, SpecKitCategory.CHECKLIST, feature)

    def _scan_specs_root(self, specs_root: Path) -> bool:
        directory = self._directory(specs_root)
        if directory is None:
            return False
        for feature_dir in self._children(directory):
            if feature_dir.is_symlink():
                self._record_skip(feature_dir, "symlink", symlink=True)
                continue
            if not feature_dir.is_dir():
                continue
            feature = feature_dir.name
            self._scan_named_documents(feature_dir, feature)
            self._scan_checklists(feature_dir, feature)
        return True

    def _scan_constitutions(self, memory_dir: Path) -> None:
        directory = self._directory(memory_dir)
        if directory is None:
            return
        for child in self._children(directory):
            if (child.is_file() or child.is_symlink()) and _is_constitution_name(child):
                self._add_file(child, SpecKitCategory.CONSTITUTION)

    def _scan_compatibility_root(self) -> None:
        for child in self._children(self.workspace):
            if (child.is_file() or child.is_symlink()) and _casefold_name(child) in _ROOT_COMPAT_NAMES:
                name = _casefold_name(child)
                if _is_constitution_name(child):
                    category = SpecKitCategory.CONSTITUTION
                elif name in _SPEC_NAMES:
                    category = SpecKitCategory.SPECIFICATION
                elif name in _PLAN_NAMES:
                    category = SpecKitCategory.PLAN
                elif name in _TASK_NAMES:
                    category = SpecKitCategory.TASKS
                else:
                    category = SpecKitCategory.CHECKLIST
                self._add_file(child, category)
            elif child.is_symlink() and _casefold_name(child) in _CHECKLIST_DIRECTORIES:
                self._record_skip(child, "symlink", symlink=True)
            elif child.is_dir() and not child.is_symlink() and _casefold_name(child) in _CHECKLIST_DIRECTORIES:
                for checklist in self._children(child):
                    if (checklist.is_file() or checklist.is_symlink()) and checklist.suffix.casefold() == ".md":
                        self._add_file(checklist, SpecKitCategory.CHECKLIST)

    def scan(self) -> SpecKitScan:
        """Perform one local discovery pass and return an immutable result."""

        self._seen = set()
        self._skipped = []
        self._documents = []
        specify = _find_named(self.workspace, _SPECIFY_DIRECTORY)
        has_specify = specify is not None and self._directory(specify) is not None
        if has_specify and specify is not None:
            memory = _find_named(specify, _MEMORY_DIRECTORY)
            if memory is not None:
                self._scan_constitutions(memory)
            # Some existing repositories keep a compatibility constitution
            # directly below .specify; recognise it without scanning templates.
            for child in self._children(specify):
                if (child.is_file() or child.is_symlink()) and _is_constitution_name(child):
                    self._add_file(child, SpecKitCategory.CONSTITUTION)
            specify_specs = _find_named(specify, _SPECS_DIRECTORY)
            if specify_specs is not None:
                self._scan_specs_root(specify_specs)
        specs = _find_named(self.workspace, _SPECS_DIRECTORY)
        has_specs = self._scan_specs_root(specs) if specs is not None else False
        self._scan_compatibility_root()
        documents = tuple(
            sorted(
                self._documents,
                key=lambda item: (item.relative_path.casefold(), item.category.value),
            )
        )
        return SpecKitScan(
            workspace=self.workspace,
            documents=documents,
            skipped=tuple(self._skipped),
            has_specify=has_specify,
            has_specs=has_specs,
        )

    discover = scan
    collect = scan

    def read_document(
        self,
        path: Path | str,
        *,
        category: SpecKitCategory | str | None = None,
        feature: str | None = None,
    ) -> SpecKitDocument:
        """Read one known document after enforcing the workspace boundary."""

        candidate, relative = self._safe_candidate(Path(path))
        name = _casefold_name(candidate)
        if category is None:
            if _is_constitution_name(candidate):
                selected = SpecKitCategory.CONSTITUTION
            elif any(part.casefold() in _CHECKLIST_DIRECTORIES for part in Path(relative).parts):
                selected = SpecKitCategory.CHECKLIST
            elif name in _SPEC_NAMES:
                selected = SpecKitCategory.SPECIFICATION
            elif name in _PLAN_NAMES:
                selected = SpecKitCategory.PLAN
            elif name in _TASK_NAMES:
                selected = SpecKitCategory.TASKS
            elif name.startswith("checklist") and candidate.suffix.casefold() == ".md":
                selected = SpecKitCategory.CHECKLIST
            else:
                raise SpecKitError("Não foi possível inferir a categoria do documento.")
        else:
            selected = _coerce_category(category)
        if feature is None:
            parts = tuple(part for part in Path(relative).parts)
            for index, part in enumerate(parts[:-1]):
                if part.casefold() == _SPECS_DIRECTORY and index + 1 < len(parts) - 1:
                    feature = parts[index + 1]
                    break
        self._seen = set()
        self._documents = []
        self._skipped = []
        self._add_file(candidate, selected, feature)
        if not self._documents:
            raise SpecKitReadError(f"Não foi possível ler o documento: {relative}.")
        return self._documents[0]

    read = read_document
    load = read_document

    def build_high_priority_context(
        self, scan: SpecKitScan | None = None, *, max_chars: int | None = None
    ) -> SpecKitContext:
        result = scan or self.scan()
        if result.workspace != self.workspace:
            raise SpecKitPathError("O resultado de scan pertence a outro workspace.")
        selected_max_chars = self.max_context_chars if max_chars is None else max_chars
        return result.to_context(max_chars=selected_max_chars)

    def context_for_budget(
        self,
        budget: Any | None = None,
        *,
        scan: SpecKitScan | None = None,
        max_chars: int | None = None,
    ) -> SpecKitContext:
        """Build high-priority context and optionally populate a budget."""

        context = self.build_high_priority_context(scan, max_chars=max_chars)
        if budget is not None:
            context.add_to_budget(budget)
        return context

    high_priority_context = build_high_priority_context
    build_context = build_high_priority_context
    to_context = build_high_priority_context

    def add_to_budget(
        self,
        budget: Any,
        *,
        scan: SpecKitScan | None = None,
        max_chars: int | None = None,
    ) -> SpecKitContext:
        context = self.build_high_priority_context(scan, max_chars=max_chars)
        context.add_to_budget(budget)
        return context

    populate_budget = add_to_budget
    apply_to_budget = add_to_budget
    to_budget = add_to_budget


# Domain-friendly aliases keep the adapter discoverable without multiplying
# implementations or introducing a parallel framework vocabulary.
SpecKitSource = SpecKitDocument
SpecKitLayout = SpecKitScan
SpecKitDiscovery = SpecKitScan
SpecKitReader = SpecKitAdapter
SpecKitDocumentCategory = SpecKitCategory
SpecKitDocumentPriority = SpecKitPriority
SpecKitDocumentKind = SpecKitCategory
SpecKitSourceCategory = SpecKitCategory
SpecKitSourcePriority = SpecKitPriority
SpecKitResult = SpecKitScan


__all__ = [
    "DEFAULT_MAX_CONTENT_CHARS",
    "DEFAULT_MAX_CONTEXT_CHARS",
    "DEFAULT_MAX_DOCUMENTS",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_SCAN_ENTRIES",
    "HIGH_PRIORITY_CATEGORIES",
    "MAX_CONTEXT_CHARS",
    "MAX_DOCUMENTS",
    "MAX_DOCUMENT_CONTENT_CHARS",
    "MAX_FILE_BYTES",
    "MAX_SCAN_ENTRIES",
    "SpecKitAdapter",
    "SpecKitCategory",
    "SpecKitContext",
    "SpecKitDiscovery",
    "SpecKitDocument",
    "SpecKitDocumentCategory",
    "SpecKitDocumentKind",
    "SpecKitDocumentPriority",
    "SpecKitError",
    "SpecKitLayout",
    "SpecKitPathError",
    "SpecKitPriority",
    "SpecKitReadError",
    "SpecKitReader",
    "SpecKitResult",
    "SpecKitScan",
    "SpecKitSkippedPath",
    "SpecKitSourceCategory",
    "SpecKitSourcePriority",
    "SpecKitSource",
]
