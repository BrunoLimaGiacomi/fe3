"""Workspace-scoped checkpoints that preserve pre-existing user work.

Rollback is conflict-aware: a sealed checkpoint restores a file only when its
current state still matches the state produced immediately after the guarded
change. Later user edits cause the whole rollback to stop before any write.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


DEFAULT_MAX_FILES = 256
DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
_CHECKPOINT_ID_RE = re.compile(r"^CP-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")


class CheckpointError(RuntimeError):
    pass


class CheckpointPathError(CheckpointError, ValueError):
    pass


class CheckpointConflictError(CheckpointError):
    def __init__(self, conflicts: Sequence[str]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__("rollback recusado; arquivos mudaram após o checkpoint: " + ", ".join(self.conflicts))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class CheckpointEntry:
    relative_path: str
    existed_before: bool
    before_sha256: str | None
    before_size: int
    snapshot_file: str | None
    existed_after: bool | None = None
    after_sha256: str | None = None
    after_size: int | None = None


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    workspace: str
    reason: str
    created_at: str
    entries: tuple[CheckpointEntry, ...]
    sealed_at: str | None = None
    rolled_back_at: str | None = None
    git_head: str | None = None
    git_status: tuple[str, ...] = ()

    @property
    def sealed(self) -> bool:
        return self.sealed_at is not None


@dataclass(frozen=True, slots=True)
class CheckpointRollbackResult:
    checkpoint_id: str
    restored: tuple[str, ...]
    removed: tuple[str, ...]


class CheckpointManager:
    def __init__(
        self,
        workspace: str | Path,
        *,
        storage_dir: str | Path | None = None,
        max_files: int = DEFAULT_MAX_FILES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise CheckpointPathError("workspace precisa ser um diretório")
        if max_files < 1 or max_files > 4_096:
            raise ValueError("max_files fora do limite")
        if max_file_bytes < 1 or max_file_bytes > 128 * 1024 * 1024:
            raise ValueError("max_file_bytes fora do limite")
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        selected = Path(storage_dir) if storage_dir is not None else self.workspace / ".agenteglobal" / "checkpoints"
        self.storage_dir = selected.resolve(strict=False)
        if not self._inside(self.storage_dir):
            raise CheckpointPathError("checkpoint storage precisa permanecer dentro do workspace")

    def _inside(self, path: Path) -> bool:
        try:
            path.relative_to(self.workspace)
            return True
        except ValueError:
            return False

    def _target(self, raw: str | Path) -> tuple[Path, str]:
        candidate = Path(raw)
        if "\x00" in str(candidate) or ".." in candidate.parts:
            raise CheckpointPathError("caminho inválido para checkpoint")
        absolute = candidate if candidate.is_absolute() else self.workspace / candidate
        resolved = absolute.resolve(strict=False)
        if not self._inside(resolved) or resolved == self.workspace:
            raise CheckpointPathError("checkpoint aceita somente arquivos dentro do workspace")
        if self._inside(self.storage_dir) and self.storage_dir == resolved:
            raise CheckpointPathError("checkpoint não pode incluir seu próprio storage")
        try:
            resolved.relative_to(self.storage_dir)
        except ValueError:
            pass
        else:
            raise CheckpointPathError("checkpoint não pode incluir seu próprio storage")
        current = absolute.absolute()
        while self._inside(current.resolve(strict=False)):
            if current.exists() and current.is_symlink():
                raise CheckpointPathError(f"symlink recusado: {raw}")
            if current == self.workspace or current.parent == current:
                break
            current = current.parent
        return resolved, resolved.relative_to(self.workspace).as_posix()

    def _checkpoint_dir(self, checkpoint_id: str) -> Path:
        if _CHECKPOINT_ID_RE.fullmatch(checkpoint_id) is None:
            raise CheckpointError("checkpoint_id inválido")
        target = self.storage_dir / checkpoint_id
        if target.is_symlink():
            raise CheckpointPathError("checkpoint directory não pode ser symlink")
        return target

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    def _write_manifest(self, checkpoint: Checkpoint) -> None:
        directory = self._checkpoint_dir(checkpoint.checkpoint_id)
        payload = asdict(checkpoint)
        payload["entries"] = [asdict(entry) for entry in checkpoint.entries]
        data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        self._atomic_write(directory / "manifest.json", data)

    def load(self, checkpoint_id: str) -> Checkpoint:
        manifest = self._checkpoint_dir(checkpoint_id) / "manifest.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise CheckpointError(f"checkpoint não encontrado: {checkpoint_id}")
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            entries = tuple(CheckpointEntry(**item) for item in payload.pop("entries"))
            checkpoint = Checkpoint(entries=entries, **payload)
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
            raise CheckpointError("manifest de checkpoint inválido") from error
        if checkpoint.workspace != str(self.workspace):
            raise CheckpointError("checkpoint pertence a outro workspace")
        return checkpoint

    def list_checkpoints(self, *, limit: int = 20) -> tuple[Checkpoint, ...]:
        """List newest workspace checkpoints without exposing snapshot data."""
        if not 1 <= limit <= 1_000:
            raise ValueError("limit precisa estar entre 1 e 1000")
        if not self.storage_dir.is_dir():
            return ()
        checkpoints: list[Checkpoint] = []
        for directory in sorted(self.storage_dir.iterdir(), reverse=True):
            if len(checkpoints) >= limit:
                break
            if not directory.is_dir() or _CHECKPOINT_ID_RE.fullmatch(directory.name) is None:
                continue
            try:
                checkpoints.append(self.load(directory.name))
            except CheckpointError:
                continue
        return tuple(checkpoints)

    def _git_metadata(self) -> tuple[str | None, tuple[str, ...]]:
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                shell=False,
            )
            status = subprocess.run(
                ["git", "status", "--short", "--untracked-files=all"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None, ()
        selected_head = head.stdout.strip() if head.returncode == 0 else None
        selected_status = tuple(status.stdout.splitlines()[:5_000]) if status.returncode == 0 else ()
        return selected_head, selected_status

    def create(
        self,
        paths: Sequence[str | Path],
        *,
        reason: str,
        include_git_metadata: bool = True,
    ) -> Checkpoint:
        if not reason.strip() or len(reason) > 2_000:
            raise ValueError("checkpoint reason precisa ser limitado")
        if not paths or len(paths) > self.max_files:
            raise ValueError("checkpoint precisa conter de 1 até max_files caminhos")
        resolved = [self._target(path) for path in paths]
        if len({relative for _, relative in resolved}) != len(resolved):
            raise ValueError("checkpoint contém caminhos duplicados")
        checkpoint_id = f"CP-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"
        directory = self._checkpoint_dir(checkpoint_id)
        directory.mkdir(parents=True, exist_ok=False)
        entries: list[CheckpointEntry] = []
        for index, (target, relative) in enumerate(resolved):
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise CheckpointPathError(f"checkpoint aceita somente arquivos regulares: {relative}")
                data = target.read_bytes()
                if len(data) > self.max_file_bytes:
                    raise CheckpointError(f"arquivo excede max_file_bytes: {relative}")
                snapshot_name = f"snapshot-{index:04d}.bin"
                self._atomic_write(directory / snapshot_name, data)
                entries.append(CheckpointEntry(relative, True, _digest(data), len(data), snapshot_name))
            else:
                entries.append(CheckpointEntry(relative, False, None, 0, None))
        git_head, git_status = self._git_metadata() if include_git_metadata else (None, ())
        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            workspace=str(self.workspace),
            reason=reason.strip(),
            created_at=_now(),
            entries=tuple(entries),
            git_head=git_head,
            git_status=git_status,
        )
        self._write_manifest(checkpoint)
        return checkpoint

    def seal(self, checkpoint_id: str) -> Checkpoint:
        checkpoint = self.load(checkpoint_id)
        if checkpoint.sealed:
            return checkpoint
        entries: list[CheckpointEntry] = []
        for entry in checkpoint.entries:
            target, relative = self._target(entry.relative_path)
            if relative != entry.relative_path:
                raise CheckpointPathError("manifest contém caminho não canônico")
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise CheckpointPathError(f"estado posterior não é arquivo regular: {relative}")
                data = target.read_bytes()
                entries.append(replace(entry, existed_after=True, after_sha256=_digest(data), after_size=len(data)))
            else:
                entries.append(replace(entry, existed_after=False, after_sha256=None, after_size=0))
        sealed = replace(checkpoint, entries=tuple(entries), sealed_at=_now())
        self._write_manifest(sealed)
        return sealed

    def guarded_write_text(
        self,
        path: str | Path,
        content: str,
        *,
        reason: str = "guarded workspace write",
        encoding: str = "utf-8",
    ) -> Checkpoint:
        """Checkpoint, atomically write, then seal the exact post-change state."""

        target, _relative = self._target(path)
        checkpoint = self.create([target], reason=reason)
        self._atomic_write(target, content.encode(encoding))
        return self.seal(checkpoint.checkpoint_id)

    def rollback(self, checkpoint_id: str) -> CheckpointRollbackResult:
        checkpoint = self.load(checkpoint_id)
        if not checkpoint.sealed:
            raise CheckpointError("checkpoint precisa ser selado antes do rollback")
        conflicts: list[str] = []
        targets: list[tuple[CheckpointEntry, Path, bytes | None]] = []
        directory = self._checkpoint_dir(checkpoint_id)
        for entry in checkpoint.entries:
            target, relative = self._target(entry.relative_path)
            if relative != entry.relative_path:
                conflicts.append(entry.relative_path)
                continue
            current_exists = target.exists()
            current_hash: str | None = None
            if current_exists:
                if target.is_symlink() or not target.is_file():
                    conflicts.append(relative)
                    continue
                current_hash = _digest(target.read_bytes())
            if current_exists != entry.existed_after or current_hash != entry.after_sha256:
                conflicts.append(relative)
            snapshot_data: bytes | None = None
            if entry.existed_before:
                snapshot = directory / str(entry.snapshot_file)
                if snapshot.is_symlink() or not snapshot.is_file():
                    conflicts.append(relative)
                    continue
                snapshot_data = snapshot.read_bytes()
                if _digest(snapshot_data) != entry.before_sha256:
                    conflicts.append(relative)
                    continue
            targets.append((entry, target, snapshot_data))
        if conflicts:
            raise CheckpointConflictError(conflicts)

        restored: list[str] = []
        removed: list[str] = []
        for entry, target, snapshot_data in targets:
            if entry.existed_before:
                assert snapshot_data is not None
                self._atomic_write(target, snapshot_data)
                restored.append(entry.relative_path)
            elif target.exists():
                target.unlink()
                removed.append(entry.relative_path)
        rolled_back = replace(checkpoint, rolled_back_at=_now())
        self._write_manifest(rolled_back)
        return CheckpointRollbackResult(checkpoint_id, tuple(restored), tuple(removed))


__all__ = [
    "Checkpoint",
    "CheckpointConflictError",
    "CheckpointEntry",
    "CheckpointError",
    "CheckpointManager",
    "CheckpointPathError",
    "CheckpointRollbackResult",
]
