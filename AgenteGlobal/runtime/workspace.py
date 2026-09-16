"""Workspace selection and rebinding helpers for the interactive runtime."""

from __future__ import annotations

import os
from pathlib import Path


NATIVE_WORKSPACE_NAME = "WorkSpaceNativo"


class WorkspaceSelectionError(ValueError):
    """Raised when an operator-provided workspace path is unusable."""


def native_workspace(resource_root: Path | str, *, create: bool = False) -> Path:
    """Return the distribution-owned default workspace."""

    try:
        root = Path(resource_root).resolve(strict=True)
        lexical_target = root / NATIVE_WORKSPACE_NAME
        target = lexical_target.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise WorkspaceSelectionError("Não foi possível resolver o workspace nativo.") from error
    try:
        target.relative_to(root)
    except ValueError as error:
        raise WorkspaceSelectionError(
            f"Workspace nativo aponta para fora da distribuição: {lexical_target}"
        ) from error
    if create:
        try:
            lexical_target.mkdir(parents=True, exist_ok=True)
            target = lexical_target.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise WorkspaceSelectionError(f"Não foi possível criar o workspace nativo: {lexical_target}") from error
    if not target.exists():
        raise WorkspaceSelectionError(f"Workspace nativo não encontrado: {target}")
    if not target.is_dir():
        raise WorkspaceSelectionError(f"Workspace nativo não é diretório: {target}")
    return target.resolve(strict=True)


def resolve_workspace_selection(value: str, *, current_workspace: Path | str) -> Path:
    """Resolve an existing directory, or the parent of an existing file.

    Relative operator paths are anchored to the currently active workspace.
    Matching single or double quotes are accepted for pasted Windows paths.
    """

    raw = value.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        raw = raw[1:-1].strip()
    if not raw:
        raise WorkspaceSelectionError("Informe um caminho após /workspace.")
    expanded = os.path.expandvars(os.path.expanduser(raw))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = Path(current_workspace) / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise WorkspaceSelectionError(f"Caminho não encontrado: {candidate}") from error
    if resolved.is_file():
        return resolved.parent.resolve(strict=True)
    if not resolved.is_dir():
        raise WorkspaceSelectionError(f"O caminho não é arquivo nem diretório: {resolved}")
    return resolved


__all__ = [
    "NATIVE_WORKSPACE_NAME",
    "WorkspaceSelectionError",
    "native_workspace",
    "resolve_workspace_selection",
]
