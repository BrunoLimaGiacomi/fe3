"""Safe local Agent Skills discovery and progressive loading.

The registry intentionally separates three disclosure levels:

* L0 reads only bounded YAML front matter and exposes metadata;
* L1 reads the selected ``SKILL.md``;
* L2 reads a selected file below one of the explicitly supported resource
  directories.

This module treats skill files as data.  It never imports, evaluates, or
executes their contents.  Filesystem providers are confined to an explicit
boundary and reject symlinks, traversal, and oversized files.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable


SKILL_FILENAME = "SKILL.md"
RESOURCE_DIRECTORIES = ("references", "scripts", "templates", "assets")
DEFAULT_MAX_SKILLS = 256
DEFAULT_MAX_SKILL_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_RESOURCE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_FRONTMATTER_BYTES = 64 * 1024
DEFAULT_MAX_RESOURCES = 2_000
MAX_SUPPORTED_SKILL_BYTES = 16 * 1024 * 1024
MAX_SUPPORTED_RESOURCE_BYTES = 128 * 1024 * 1024
# Compatibility names for callers that use the file/resource terminology.
MAX_SKILL_FILE_BYTES = DEFAULT_MAX_SKILL_BYTES
MAX_RESOURCE_FILE_BYTES = DEFAULT_MAX_RESOURCE_BYTES

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_FRONTMATTER_END_RE = re.compile(r"^\s*(?:---|\.\.\.)\s*$")


class SkillRegistryError(ValueError):
    """Base error for invalid or unavailable skill data."""


class SkillValidationError(SkillRegistryError):
    """A skill or resource violates the local safety contract."""


class SkillNotFoundError(SkillRegistryError, KeyError):
    """The requested skill or resource was not registered."""


class SkillFormatError(SkillValidationError):
    """Agent Skills front matter is malformed."""


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    """L0 metadata; it contains no skill instructions or resource payload."""

    name: str
    description: str
    tags: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    version: str = "unknown"
    trust: str = "local"
    origin: str = "local"
    _path: Path | None = field(default=None, repr=False, compare=False)
    _skill_root: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        name = self.name.strip().lower() if isinstance(self.name, str) else ""
        if _NAME_RE.fullmatch(name) is None:
            raise SkillValidationError("Skill name must be a safe identifier")
        description = self.description.strip() if isinstance(self.description, str) else ""
        if not description or len(description) > 1_024:
            raise SkillValidationError("Skill description must be a non-empty string of at most 1024 characters")
        raw_tags = (self.tags,) if isinstance(self.tags, str) else self.tags
        raw_capabilities = (self.capabilities,) if isinstance(self.capabilities, str) else self.capabilities
        tags = tuple(str(value).strip().lower() for value in raw_tags)
        capabilities = tuple(str(value).strip().lower() for value in raw_capabilities)
        if any(_TAG_RE.fullmatch(value) is None for value in (*tags, *capabilities)):
            raise SkillValidationError("Skill tags and capabilities must be safe identifiers")
        if len(set(tags)) != len(tags) or len(set(capabilities)) != len(capabilities):
            raise SkillValidationError("Skill tags and capabilities must not contain duplicates")
        for field_name, value, max_length in (
            ("version", self.version, 128),
            ("trust", self.trust, 128),
            ("origin", self.origin, 128),
        ):
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
                raise SkillValidationError(f"Skill {field_name} must be a bounded non-empty string")
            if any(ord(char) < 0x20 for char in value):
                raise SkillValidationError(f"Skill {field_name} cannot contain control characters")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "tags", tags)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "version", self.version.strip())
        object.__setattr__(self, "trust", self.trust.strip())
        object.__setattr__(self, "origin", self.origin.strip())

    @property
    def identifier(self) -> str:
        return self.name

    @property
    def skill_id(self) -> str:
        return self.name

    @property
    def level(self) -> str:
        return "L0"

    @property
    def path(self) -> Path | None:
        """Internal path, when this metadata came from a local provider."""

        return self._path

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "capabilities": list(self.capabilities),
            "version": self.version,
            "trust": self.trust,
            "origin": self.origin,
        }

    model_dump = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True, slots=True)
class SkillDocument:
    """L1 selected skill document.

    ``content`` is the complete UTF-8 ``SKILL.md`` (including front matter),
    while ``instructions`` is the body after the front matter delimiter.
    """

    metadata: SkillMetadata
    content: str
    instructions: str
    frontmatter: Mapping[str, Any] = field(default_factory=dict)
    path: Path | None = field(default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def body(self) -> str:
        return self.instructions

    @property
    def text(self) -> str:
        return self.content

    @property
    def full_content(self) -> str:
        return self.content

    @property
    def level(self) -> str:
        return "L1"


@dataclass(frozen=True, slots=True)
class SkillResourceMetadata:
    """L2 descriptor without file contents."""

    skill_name: str
    kind: str
    relative_path: str
    size_bytes: int
    path: Path | None = field(default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        return self.relative_path

    @property
    def size(self) -> int:
        return self.size_bytes

    @property
    def level(self) -> str:
        return "L2"

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class SkillResource:
    """L2 selected resource.  Its payload is returned as inert bytes."""

    metadata: SkillResourceMetadata
    content: bytes

    @property
    def data(self) -> bytes:
        return self.content

    @property
    def level(self) -> str:
        return "L2"

    def text(self, encoding: str = "utf-8") -> str:
        return self.content.decode(encoding)


@runtime_checkable
class SkillProvider(Protocol):
    """Read-only extension point for non-filesystem skill sources."""

    def discover(self) -> Iterable[SkillMetadata]:
        ...

    def load(self, name: str) -> SkillDocument:
        ...


@dataclass(frozen=True, slots=True)
class _LocalSkill:
    metadata: SkillMetadata
    directory: Path
    skill_file: Path
    frontmatter: Mapping[str, Any]
    body_offset: int


def _is_within(path: Path, boundary: Path) -> bool:
    try:
        path.relative_to(boundary)
    except ValueError:
        return False
    return True


def _has_symlink_component(path: Path, boundary: Path) -> bool:
    """Check an input path without following any component in the check."""

    current = path
    stop = boundary
    while True:
        if current.is_symlink():
            return True
        if current == stop:
            return False
        if current.parent == current:
            return True
        current = current.parent


def _validate_positive_limit(name: str, value: int, hard_max: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    if value > hard_max:
        raise ValueError(f"{name} cannot exceed {hard_max}")
    return value


def _strip_yaml_comment(value: str) -> str:
    quoted: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quoted == '"':
            escaped = True
            continue
        if char in {'"', "'"}:
            if quoted == char:
                quoted = None
            elif quoted is None:
                quoted = char
            continue
        if char == "#" and quoted is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _parse_scalar(value: str) -> Any:
    value = _strip_yaml_comment(value.strip())
    if not value:
        return None
    if value in {"~", "null", "Null", "NULL"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith("[") or value.startswith("{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            try:
                return ast.literal_eval(value)
            except (SyntaxError, ValueError):
                # YAML permits unquoted plain scalars in flow sequences.  We
                # only need a conservative scalar split here; nested YAML
                # objects are not interpreted or evaluated.
                if value.startswith("[") and value.endswith("]"):
                    inner = value[1:-1].strip()
                    if not inner:
                        return []
                    return [_parse_scalar(item.strip()) for item in inner.split(",")]
                if value.startswith("{") and value.endswith("}"):
                    inner = value[1:-1].strip()
                    result: dict[str, Any] = {}
                    if not inner:
                        return result
                    for pair in inner.split(","):
                        if ":" not in pair:
                            raise SkillFormatError(f"Invalid front matter collection: {value!r}")
                        key, item = pair.split(":", 1)
                        key = key.strip().strip("\"'")
                        if not key:
                            raise SkillFormatError(f"Invalid front matter collection: {value!r}")
                        result[key] = _parse_scalar(item.strip())
                    return result
                raise SkillFormatError(f"Invalid front matter collection: {value!r}")
    if value[:1] in {'"', "'"}:
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError) as error:
            raise SkillFormatError(f"Invalid front matter string: {value!r}") from error
    return value


def _parse_yaml_subset(text: str) -> dict[str, Any]:
    """Parse the small, safe YAML subset used by Agent Skills front matter.

    PyYAML is deliberately not a dependency.  Agent Skills front matter is a
    mapping of scalar/list fields plus optional nested ``metadata``.  Unknown
    fields remain inert data and are retained for extension compatibility.
    """

    raw_lines = text.splitlines()
    lines: list[tuple[int, str]] = []
    for line in raw_lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if "\t" in line[:indent]:
            raise SkillFormatError("Tabs are not supported in Agent Skills front matter")
        lines.append((indent, line[indent:]))

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines) or lines[index][0] < indent:
            return {}, index
        is_list = lines[index][0] == indent and lines[index][1].startswith("- ")
        result: Any = [] if is_list else {}
        while index < len(lines):
            current_indent, content = lines[index]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise SkillFormatError("Invalid indentation in Agent Skills front matter")
            if is_list:
                if not content.startswith("- "):
                    break
                result.append(_parse_scalar(content[2:]))
                index += 1
                continue
            if content.startswith("- "):
                raise SkillFormatError("Unexpected list in Agent Skills front matter")
            if ":" not in content:
                raise SkillFormatError(f"Invalid front matter line: {content!r}")
            key, raw_value = content.split(":", 1)
            key = key.strip()
            if not key or any(ord(char) < 0x20 for char in key):
                raise SkillFormatError("Front matter keys must be safe non-empty strings")
            raw_value = raw_value.strip()
            index += 1
            if raw_value in {"|", ">", "|-", ">-", "|+", ">+"}:
                chunks: list[str] = []
                while index < len(lines) and lines[index][0] > indent:
                    chunks.append(lines[index][1])
                    index += 1
                result[key] = "\n".join(chunks) if raw_value.startswith("|") else " ".join(chunks)
            elif raw_value:
                result[key] = _parse_scalar(raw_value)
            elif index < len(lines) and lines[index][0] > indent:
                child_indent = lines[index][0]
                child, index = parse_block(index, child_indent)
                result[key] = child
            else:
                result[key] = None
        return result, index

    parsed, index = parse_block(0, lines[0][0] if lines else 0)
    if index != len(lines) or not isinstance(parsed, dict):
        raise SkillFormatError("Agent Skills front matter must be a mapping")
    return parsed


def _coerce_string(value: Any, *, field_name: str, required: bool = False, max_length: int = 1_024) -> str:
    if value is None:
        if required:
            raise SkillFormatError(f"Missing required front matter field: {field_name}")
        return ""
    if not isinstance(value, str):
        raise SkillFormatError(f"Front matter field {field_name!r} must be a string")
    normalized = value.strip()
    if required and not normalized:
        raise SkillFormatError(f"Front matter field {field_name!r} cannot be empty")
    if len(normalized) > max_length or any(ord(char) < 0x20 and char not in "\n\r\t" for char in normalized):
        raise SkillFormatError(f"Front matter field {field_name!r} exceeds its safe limit")
    return normalized


def _coerce_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Sequence[Any] = tuple(part.strip() for part in value.split(","))
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise SkillFormatError(f"Front matter field {field_name!r} must be a list or string")
    output: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise SkillFormatError(f"Front matter field {field_name!r} entries must be strings")
        item = item.strip().lower()
        if not item or not _TAG_RE.fullmatch(item):
            raise SkillFormatError(f"Invalid {field_name} identifier: {item!r}")
        if item not in output:
            output.append(item)
    return tuple(output)


def _frontmatter_metadata(
    frontmatter: Mapping[str, Any],
    *,
    directory_name: str,
    origin: str,
    trust: str,
    path: Path,
    skill_root: Path,
) -> SkillMetadata:
    nested = frontmatter.get("metadata")
    nested_metadata = nested if isinstance(nested, Mapping) else {}
    name = _coerce_string(frontmatter.get("name"), field_name="name", required=True, max_length=64).lower()
    if _NAME_RE.fullmatch(name) is None:
        raise SkillFormatError("Skill name must be a lowercase Agent Skills identifier")
    if name != directory_name.lower():
        raise SkillFormatError("Skill name must match its directory name")
    description = _coerce_string(
        frontmatter.get("description"), field_name="description", required=True, max_length=1_024
    )
    selected_origin = frontmatter.get("origin", nested_metadata.get("origin", origin))
    selected_trust = frontmatter.get("trust", nested_metadata.get("trust", trust))
    selected_version = frontmatter.get("version", nested_metadata.get("version", "unknown"))
    return SkillMetadata(
        name=name,
        description=description,
        tags=_coerce_list(frontmatter.get("tags", nested_metadata.get("tags")), field_name="tags"),
        capabilities=_coerce_list(
            frontmatter.get("capabilities", nested_metadata.get("capabilities")), field_name="capabilities"
        ),
        version=_coerce_string(selected_version, field_name="version", max_length=128) or "unknown",
        trust=_coerce_string(selected_trust, field_name="trust", max_length=128) or "local",
        origin=_coerce_string(selected_origin, field_name="origin", max_length=128) or "local",
        _path=path,
        _skill_root=skill_root,
    )


def _read_frontmatter(
    path: Path,
    *,
    max_prefix_bytes: int,
) -> tuple[dict[str, Any], int]:
    """Read only the front matter prefix and return its character end offset."""

    try:
        with path.open("rb") as stream:
            first = stream.readline(max_prefix_bytes + 1)
            if len(first) > max_prefix_bytes:
                raise SkillFormatError("Skill front matter exceeds the configured prefix limit")
            if first.decode("utf-8-sig").strip() != "---":
                raise SkillFormatError("SKILL.md must start with Agent Skills front matter")
            lines: list[bytes] = []
            # ``body_offset`` is applied to the decoded full document, so it
            # must be a character count rather than a UTF-8 byte count.
            first_text = first.decode("utf-8-sig")
            body_offset = len(first_text.replace("\r\n", "\n").replace("\r", "\n"))
            consumed = len(first)
            while consumed <= max_prefix_bytes:
                line = stream.readline(max_prefix_bytes - consumed + 1)
                if not line:
                    raise SkillFormatError("Unterminated Agent Skills front matter")
                consumed += len(line)
                if len(line) > max_prefix_bytes or consumed > max_prefix_bytes:
                    raise SkillFormatError("Skill front matter exceeds the configured prefix limit")
                decoded = line.decode("utf-8")
                if _FRONTMATTER_END_RE.fullmatch(decoded.rstrip("\r\n")):
                    body_offset += len(decoded.replace("\r\n", "\n").replace("\r", "\n"))
                    return _parse_yaml_subset(b"".join(lines).decode("utf-8")), body_offset
                body_offset += len(decoded.replace("\r\n", "\n").replace("\r", "\n"))
                lines.append(line)
    except UnicodeDecodeError as error:
        raise SkillFormatError("SKILL.md and front matter must be valid UTF-8") from error
    except OSError as error:
        raise SkillValidationError(f"Cannot read skill metadata: {path.name}") from error
    raise SkillFormatError("Unterminated Agent Skills front matter")


class SkillRegistry:
    """Boundary-confined registry with explicit L0/L1/L2 disclosure."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        skills_dir: Path | str | None = None,
        skills_root: Path | str | None = None,
        directory: Path | str | None = None,
        boundary: Path | str | None = None,
        max_skills: int = DEFAULT_MAX_SKILLS,
        max_skill_bytes: int = DEFAULT_MAX_SKILL_BYTES,
        max_file_bytes: int | None = None,
        max_resource_bytes: int = DEFAULT_MAX_RESOURCE_BYTES,
        max_resource_file_bytes: int | None = None,
        max_frontmatter_bytes: int = DEFAULT_MAX_FRONTMATTER_BYTES,
        max_resources: int = DEFAULT_MAX_RESOURCES,
        origin: str = "local",
        trust: str = "local",
        providers: Iterable[SkillProvider] = (),
    ) -> None:
        selected_root = root or skills_dir or skills_root or directory
        if selected_root is None:
            raise TypeError("SkillRegistry requires root/skills_dir/directory")
        roots = (root, skills_dir, skills_root, directory)
        if sum(value is not None for value in roots) > 1:
            selected_values = [Path(value) for value in roots if value is not None]
            if any(value != selected_values[0] for value in selected_values[1:]):
                raise ValueError("root, skills_dir, skills_root and directory must identify one directory")
        if max_file_bytes is not None:
            max_skill_bytes = max_file_bytes
        if max_resource_file_bytes is not None:
            max_resource_bytes = max_resource_file_bytes
        self._max_skills = _validate_positive_limit("max_skills", max_skills, DEFAULT_MAX_SKILLS * 16)
        self._max_skill_bytes = _validate_positive_limit("max_skill_bytes", max_skill_bytes, MAX_SUPPORTED_SKILL_BYTES)
        self._max_resource_bytes = _validate_positive_limit(
            "max_resource_bytes", max_resource_bytes, MAX_SUPPORTED_RESOURCE_BYTES
        )
        # A tiny skill ceiling should remain usable for fail-closed size
        # validation; the metadata prefix is bounded by whichever ceiling is
        # smaller rather than making construction itself impossible.
        self._max_frontmatter_bytes = _validate_positive_limit(
            "max_frontmatter_bytes", min(max_frontmatter_bytes, max_skill_bytes), max_skill_bytes
        )
        self._max_resources = _validate_positive_limit("max_resources", max_resources, DEFAULT_MAX_RESOURCES * 16)
        self._origin = _coerce_string(origin, field_name="origin", max_length=128) or "local"
        self._trust = _coerce_string(trust, field_name="trust", max_length=128) or "local"
        self._root, self._boundary = self._resolve_boundary(Path(selected_root), boundary)
        self._local: dict[str, _LocalSkill] = {}
        self._providers: list[SkillProvider] = []
        for provider in providers:
            self.register_provider(provider)
        self._discover_local()

    @staticmethod
    def _resolve_boundary(directory: Path, boundary: Path | str | None) -> tuple[Path, Path]:
        raw_directory = directory.absolute()
        if raw_directory.is_symlink():
            raise SkillValidationError("Skills directory cannot be a symlink")
        raw_boundary = Path(boundary).absolute() if boundary is not None else raw_directory
        if raw_boundary.is_symlink():
            raise SkillValidationError("Skills boundary cannot be a symlink")
        try:
            resolved_boundary = raw_boundary.resolve(strict=True)
            resolved_directory = raw_directory.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise SkillValidationError("Cannot resolve skills boundary or directory") from error
        if not resolved_boundary.is_dir() or not resolved_directory.is_dir():
            raise SkillValidationError("Skills boundary and directory must be directories")
        if not _is_within(resolved_directory, resolved_boundary):
            raise SkillValidationError("Skills directory is outside the configured boundary")
        if _has_symlink_component(raw_directory, raw_boundary):
            raise SkillValidationError("Symlinks are not allowed in the skills path")
        return resolved_directory, resolved_boundary

    @property
    def root(self) -> Path:
        return self._root

    @property
    def boundary(self) -> Path:
        return self._boundary

    @classmethod
    def from_directory(cls, directory: Path | str, **kwargs: Any) -> "SkillRegistry":
        return cls(directory, **kwargs)

    @classmethod
    def discover(cls, directory: Path | str, **kwargs: Any) -> "SkillRegistry":
        return cls(directory, **kwargs)

    def register_provider(self, provider: SkillProvider) -> None:
        if not isinstance(provider, SkillProvider):
            if not callable(getattr(provider, "discover", None)) or not callable(getattr(provider, "load", None)):
                raise TypeError("Skill provider must implement discover() and load()")
        self._providers.append(provider)

    add_provider = register_provider

    def _discover_local(self) -> None:
        try:
            entries = sorted(self._root.iterdir(), key=lambda item: item.name.lower())
        except OSError as error:
            raise SkillValidationError("Cannot list local skills directory") from error
        directories = [entry for entry in entries if entry.is_dir() or entry.is_symlink()]
        if len(directories) > self._max_skills:
            raise SkillValidationError("Skill count exceeds the configured limit")
        for directory in directories:
            if directory.is_symlink():
                raise SkillValidationError(f"Skill directory cannot be a symlink: {directory.name}")
            if _NAME_RE.fullmatch(directory.name.lower()) is None:
                raise SkillValidationError(f"Invalid skill directory name: {directory.name!r}")
            skill_file = directory / SKILL_FILENAME
            if skill_file.is_symlink() or not skill_file.is_file():
                raise SkillValidationError(f"Skill must contain a regular {SKILL_FILENAME}: {directory.name}")
            if _has_symlink_component(skill_file.absolute(), self._boundary.absolute()):
                raise SkillValidationError(f"Symlink in skill path: {directory.name}")
            try:
                size = skill_file.stat().st_size
            except OSError as error:
                raise SkillValidationError(f"Cannot stat skill: {directory.name}") from error
            if size > self._max_skill_bytes:
                raise SkillValidationError(f"Skill exceeds {self._max_skill_bytes} bytes: {directory.name}")
            frontmatter, body_offset = _read_frontmatter(
                skill_file, max_prefix_bytes=self._max_frontmatter_bytes
            )
            metadata = _frontmatter_metadata(
                frontmatter,
                directory_name=directory.name,
                origin=self._origin,
                trust=self._trust,
                path=skill_file,
                skill_root=self._root,
            )
            if metadata.name in self._local:
                raise SkillValidationError(f"Duplicate skill name: {metadata.name}")
            self._local[metadata.name] = _LocalSkill(
                metadata=metadata,
                directory=directory,
                skill_file=skill_file,
                frontmatter=frontmatter,
                body_offset=body_offset,
            )

    @staticmethod
    def _normalize_name(name: str) -> str:
        if not isinstance(name, str):
            raise TypeError("Skill name must be a string")
        normalized = name.strip().lower()
        if _NAME_RE.fullmatch(normalized) is None:
            raise SkillValidationError("Skill name must be a safe identifier")
        return normalized

    def _provider_metadata(self) -> tuple[SkillMetadata, ...]:
        metadata: list[SkillMetadata] = []
        seen = set(self._local)
        for provider in self._providers:
            try:
                discovered = tuple(provider.discover())
            except Exception as error:
                raise SkillRegistryError("Skill provider discovery failed") from error
            for item in discovered:
                if not isinstance(item, SkillMetadata):
                    raise SkillRegistryError("Skill providers must return SkillMetadata")
                name = self._normalize_name(item.name)
                if name in seen:
                    raise SkillValidationError(f"Duplicate skill name: {name}")
                seen.add(name)
                metadata.append(item)
        return tuple(metadata)

    def list_metadata(self) -> tuple[SkillMetadata, ...]:
        items = [item.metadata for item in self._local.values()]
        items.extend(self._provider_metadata())
        return tuple(sorted(items, key=lambda item: item.name))

    def list_skills(self) -> tuple[SkillMetadata, ...]:
        return self.list_metadata()

    def list(self) -> tuple[SkillMetadata, ...]:
        return self.list_metadata()

    def list_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.list_metadata())

    @property
    def skills(self) -> tuple[SkillMetadata, ...]:
        return self.list_metadata()

    def get_metadata(self, name: str) -> SkillMetadata | None:
        normalized = self._normalize_name(name)
        local = self._local.get(normalized)
        if local is not None:
            return local.metadata
        return next((item for item in self._provider_metadata() if item.name == normalized), None)

    metadata_for = get_metadata
    get = get_metadata

    def _require_metadata(self, name: str) -> SkillMetadata:
        metadata = self.get_metadata(name)
        if metadata is None:
            raise SkillNotFoundError(f"Unknown skill: {name}")
        return metadata

    def search(
        self,
        query: str = "",
        *,
        tags: Iterable[str] = (),
        capabilities: Iterable[str] = (),
    ) -> tuple[SkillMetadata, ...]:
        """Search L0 metadata only; this never reads skill bodies."""

        terms = tuple(term.lower() for term in str(query).split() if term.strip())
        wanted_tags = {str(tag).strip().lower() for tag in tags if str(tag).strip()}
        wanted_capabilities = {str(capability).strip().lower() for capability in capabilities if str(capability).strip()}
        matches: list[SkillMetadata] = []
        for item in self.list_metadata():
            haystack = " ".join((item.name, item.description, *item.tags, *item.capabilities)).lower()
            if terms and not all(term in haystack for term in terms):
                continue
            if wanted_tags and not wanted_tags.issubset(item.tags):
                continue
            if wanted_capabilities and not wanted_capabilities.issubset(item.capabilities):
                continue
            matches.append(item)
        return tuple(matches)

    def inspect(
        self,
        name: str,
        *,
        level: int = 0,
        include_content: bool = False,
    ) -> SkillMetadata | SkillDocument | tuple[SkillResourceMetadata, ...]:
        """Inspect L0 by default, or explicitly select L1/L2."""

        if isinstance(level, bool) or level not in (0, 1, 2):
            raise ValueError("Skill inspection level must be 0, 1, or 2")
        if include_content:
            level = max(level, 1)
        if level == 0:
            return self._require_metadata(name)
        if level == 1:
            return self.load(name)
        return self.list_resources(name)

    inspect_skill = inspect

    def load_level(self, name: str, level: int = 1) -> SkillDocument | tuple[SkillResourceMetadata, ...] | SkillMetadata:
        return self.inspect(name, level=level)

    def load(self, name: str) -> SkillDocument:
        """Select one skill and load its complete ``SKILL.md`` (L1)."""

        normalized = self._normalize_name(name)
        local = self._local.get(normalized)
        if local is None:
            for provider in self._providers:
                if provider.discover and any(item.name == normalized for item in provider.discover()):
                    return provider.load(normalized)
            raise SkillNotFoundError(f"Unknown skill: {name}")
        self._validate_local_file(local.skill_file, self._max_skill_bytes, "skill")
        try:
            content = local.skill_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as error:
            raise SkillValidationError(f"Cannot load skill: {normalized}") from error
        return SkillDocument(
            metadata=local.metadata,
            content=content,
            instructions=content[local.body_offset :],
            frontmatter=dict(local.frontmatter),
            path=local.skill_file,
        )

    load_skill = load
    select = load

    def _validate_local_file(self, path: Path, maximum: int, kind: str) -> int:
        if path.is_symlink() or not path.is_file():
            raise SkillValidationError(f"{kind} must be a regular non-symlink file")
        if _has_symlink_component(path.absolute(), self._boundary.absolute()):
            raise SkillValidationError(f"Symlink in {kind} path")
        try:
            size = path.stat().st_size
        except OSError as error:
            raise SkillValidationError(f"Cannot stat {kind}") from error
        if size > maximum:
            raise SkillValidationError(f"{kind} exceeds {maximum} bytes")
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self._boundary)
        except (OSError, RuntimeError, ValueError) as error:
            raise SkillValidationError(f"{kind} path escapes the configured boundary") from error
        return size

    @staticmethod
    def _normalize_resource_path(relative_path: Path | str) -> Path:
        if not isinstance(relative_path, (Path, str)):
            raise TypeError("Resource path must be a path or string")
        raw = str(relative_path).replace("\\", "/")
        candidate = Path(raw)
        if (
            not raw
            or candidate.is_absolute()
            or candidate.drive
            or ".." in candidate.parts
            or any(":" in part for part in candidate.parts)
        ):
            raise SkillValidationError("Resource path must be relative and cannot traverse")
        if not candidate.parts or any(part in {"", "."} for part in candidate.parts):
            raise SkillValidationError("Resource path must identify a concrete file")
        return Path(*candidate.parts)

    def _resource_root(self, local: _LocalSkill, kind: str) -> Path:
        if kind not in RESOURCE_DIRECTORIES:
            raise SkillValidationError(f"Unsupported skill resource directory: {kind}")
        root = local.directory / kind
        if root.is_symlink():
            raise SkillValidationError("Skill resource directories cannot be symlinks")
        if root.exists() and not root.is_dir():
            raise SkillValidationError("Skill resource directory must be a directory")
        return root

    def _require_local(self, name: str) -> _LocalSkill:
        normalized = self._normalize_name(name)
        local = self._local.get(normalized)
        if local is None:
            raise SkillNotFoundError(f"Unknown local skill: {name}")
        return local

    def list_resources(
        self, name: str, kind: str | None = None
    ) -> tuple[SkillResourceMetadata, ...]:
        """List L2 descriptors without opening resource contents."""

        local = self._require_local(name)
        kinds = (kind,) if kind is not None else RESOURCE_DIRECTORIES
        resources: list[SkillResourceMetadata] = []
        for selected_kind in kinds:
            root = self._resource_root(local, selected_kind)
            if not root.exists():
                continue
            count = 0
            for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
                if path.is_symlink():
                    raise SkillValidationError("Skill resources cannot contain symlinks")
                if path.is_dir():
                    continue
                count += 1
                if count > self._max_resources:
                    raise SkillValidationError("Skill resource count exceeds the configured limit")
                size = self._validate_local_file(path, self._max_resource_bytes, "skill resource")
                relative = path.relative_to(local.directory).as_posix()
                resources.append(SkillResourceMetadata(local.metadata.name, selected_kind, relative, size, path))
        return tuple(resources)

    resources = list_resources

    def load_resource(
        self,
        name: str,
        relative_path: Path | str,
        *,
        kind: str | None = None,
    ) -> SkillResource:
        """Load one explicitly requested inert L2 resource."""

        local = self._require_local(name)
        relative = self._normalize_resource_path(relative_path)
        parts = relative.parts
        selected_kind = kind or (parts[0] if parts and parts[0] in RESOURCE_DIRECTORIES else None)
        if selected_kind not in RESOURCE_DIRECTORIES:
            raise SkillValidationError("Resource path must begin with an allowed resource directory")
        if kind is not None and (not parts or parts[0] != kind):
            relative = Path(kind, *parts)
        root = self._resource_root(local, selected_kind)
        candidate = local.directory.joinpath(relative)
        if not _is_within(candidate.absolute(), local.directory.absolute()):
            raise SkillValidationError("Resource path escapes the skill directory")
        size = self._validate_local_file(candidate, self._max_resource_bytes, "skill resource")
        try:
            content = candidate.read_bytes()
        except OSError as error:
            raise SkillValidationError("Cannot load skill resource") from error
        if len(content) != size:
            raise SkillValidationError("Skill resource changed while being read")
        metadata = SkillResourceMetadata(local.metadata.name, selected_kind, candidate.relative_to(local.directory).as_posix(), size, candidate)
        return SkillResource(metadata, content)

    def load_resources(
        self, name: str, kind: str | None = None
    ) -> tuple[SkillResource, ...]:
        """Load all explicitly requested L2 resources of one or all kinds."""

        descriptors = self.list_resources(name, kind=kind)
        return tuple(
            self.load_resource(name, descriptor.relative_path, kind=descriptor.kind)
            for descriptor in descriptors
        )


__all__ = [
    "DEFAULT_MAX_FRONTMATTER_BYTES",
    "DEFAULT_MAX_RESOURCE_BYTES",
    "DEFAULT_MAX_RESOURCES",
    "DEFAULT_MAX_SKILL_BYTES",
    "DEFAULT_MAX_SKILLS",
    "MAX_RESOURCE_FILE_BYTES",
    "MAX_SKILL_FILE_BYTES",
    "RESOURCE_DIRECTORIES",
    "SKILL_FILENAME",
    "SkillDocument",
    "SkillFormatError",
    "SkillMetadata",
    "SkillNotFoundError",
    "SkillProvider",
    "SkillRegistry",
    "SkillRegistryError",
    "SkillResource",
    "SkillResourceMetadata",
    "SkillValidationError",
]
