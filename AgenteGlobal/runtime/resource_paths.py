"""Resolve distribution resources without changing the tool workspace.

The CLI has two distinct path domains:

* ``workspace`` is the operator-selected boundary for tools, artifacts,
  histories and local command execution;
* the AgenteGlobal distribution root owns read-only packaged defaults such as
  ``skills/``, ``agents/`` and ``model-aliases.json``.

Callers that build runtime configuration should use
``resolve_resource_path(..., packaged_default=True)`` for parser defaults.  A
workspace resource wins when it exists; otherwise the same relative default is
resolved below the distribution root.  Explicit absolute paths and explicit
non-default relative paths remain in the workspace domain.  This helper never
changes the process CWD and never grants a packaged path to a tool.

Integration contract for the Core/CLI layer:

1. Keep ``AgentConfig.workspace`` equal to the operator's requested workspace.
2. Resolve only the four packaged defaults (AGENTS.md, skills, agents and
   model-aliases.json) through this module, passing ``packaged_default=True``.
3. Preserve explicit CLI/environment paths; do not silently reinterpret them
   as packaged resources.
4. Apply the normal read-only/resource validation to returned packaged paths.
   A packaged path is configuration input, not a second tools workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final


_DISTRIBUTION_MARKERS: Final[tuple[str, ...]] = (
    "AgenteGlobal.py",
    "AgenteGlobalCore.py",
    "AGENTS.md",
    "model-aliases.json",
    "agents",
    "skills",
)


class ResourcePathError(ValueError):
    """Raised when a distribution resource cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class DistributionResources:
    """Canonical read-only resources owned by one AgenteGlobal distribution."""

    root: Path

    @property
    def agents_file(self) -> Path:
        return self.root / "AGENTS.md"

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    @property
    def profiles_dir(self) -> Path:
        return self.root / "agents"

    @property
    def model_alias_file(self) -> Path:
        return self.root / "model-aliases.json"


def _as_path(value: Path | str, *, field_name: str) -> Path:
    if not isinstance(value, (Path, str)):
        raise TypeError(f"{field_name} must be a path or string")
    return Path(value).expanduser()


def _looks_like_distribution(root: Path) -> bool:
    return (
        root.is_dir()
        and (root / "AgenteGlobal.py").is_file()
        and (root / "AgenteGlobalCore.py").is_file()
        and (root / "model-aliases.json").is_file()
        and (root / "agents").is_dir()
        and (root / "skills").is_dir()
    )


def resolve_distribution_root(anchor: Path | str | None = None) -> Path:
    """Find and validate the AgenteGlobal distribution containing ``anchor``.

    ``anchor`` may be a module file, a package directory, or a child path.
    Walking only ancestors prevents a caller's current working directory from
    affecting the result.  The returned path is absolute and normalized, but
    no process-global state is changed.
    """

    raw_anchor = Path(__file__) if anchor is None else _as_path(anchor, field_name="anchor")
    try:
        resolved_anchor = raw_anchor.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ResourcePathError(f"Cannot resolve distribution anchor: {raw_anchor}") from error
    start = resolved_anchor if resolved_anchor.is_dir() else resolved_anchor.parent
    for candidate in (start, *start.parents):
        if _looks_like_distribution(candidate):
            return candidate
    raise ResourcePathError(f"No valid AgenteGlobal distribution found above: {resolved_anchor}")


def packaged_resources(root: Path | str | None = None) -> DistributionResources:
    """Return canonical resource paths for a validated distribution root."""

    resolved = resolve_distribution_root(root)
    return DistributionResources(root=resolved)


def _relative_resource(value: Path | str) -> Path:
    candidate = _as_path(value, field_name="resource")
    # ``Path`` on Windows handles drive letters and rooted paths.  Replacing
    # separators first also makes traversal checks deterministic when a value
    # came from a POSIX-oriented config file.
    normalized = str(candidate).replace("\\", "/")
    candidate = Path(normalized)
    if (
        not normalized
        or candidate.is_absolute()
        or bool(candidate.drive)
        or ".." in candidate.parts
        or any(part in {"", "."} for part in candidate.parts)
    ):
        raise ResourcePathError("Packaged resource must be a concrete relative path")
    return candidate


def _resolve_non_strict(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ResourcePathError(f"Cannot resolve resource path: {path}") from error


def resolve_resource_path(
    value: Path | str,
    *,
    workspace: Path | str,
    packaged_default: bool = False,
    package_root: Path | str | None = None,
    require_exists: bool = False,
) -> Path:
    """Resolve one configured resource while retaining the tool workspace.

    Relative values normally resolve below ``workspace``.  When
    ``packaged_default`` is true, an existing workspace candidate still wins;
    if it is absent, an existing candidate below the validated distribution
    root is returned.  This fallback is deliberately opt-in so a missing
    explicit user path is never masked by a package default.

    Absolute values are preserved as supplied (after normalization), which
    lets operators intentionally provide an external read-only context file.
    The caller remains responsible for its policy decision and boundary check.
    """

    workspace_path = _resolve_non_strict(_as_path(workspace, field_name="workspace"))
    configured = _as_path(value, field_name="resource")
    if configured.is_absolute() or configured.drive:
        result = _resolve_non_strict(configured)
    else:
        relative = _relative_resource(configured)
        workspace_entry = workspace_path / relative
        workspace_candidate = _resolve_non_strict(workspace_entry)
        result = workspace_candidate
        # A broken symlink is an invalid local resource, not a signal to
        # silently substitute packaged content.  This keeps fail-closed path
        # validation with the SkillRegistry and avoids masking local mistakes.
        if packaged_default and not workspace_candidate.exists() and not workspace_entry.is_symlink():
            resources = packaged_resources(package_root)
            package_candidate = _resolve_non_strict(resources.root / relative)
            if package_candidate.exists():
                result = package_candidate
    if require_exists and not result.exists():
        raise FileNotFoundError(f"Configured resource not found: {result}")
    return result


def is_packaged_path(path: Path | str, *, package_root: Path | str | None = None) -> bool:
    """Return whether ``path`` is inside the validated distribution root."""

    raw_path = _as_path(path, field_name="path")
    if not raw_path.is_absolute() and not raw_path.drive:
        raise ResourcePathError("Path membership checks require an absolute path")
    candidate = _resolve_non_strict(raw_path)
    root = packaged_resources(package_root).root
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


# Readable aliases for integration callers that prefer ``get_*`` naming.
get_distribution_root = resolve_distribution_root
get_packaged_resources = packaged_resources


__all__ = [
    "DistributionResources",
    "ResourcePathError",
    "get_distribution_root",
    "get_packaged_resources",
    "is_packaged_path",
    "packaged_resources",
    "resolve_distribution_root",
    "resolve_resource_path",
]
