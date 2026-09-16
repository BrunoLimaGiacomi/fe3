"""Safe local persistence for typed, revisioned execution plans.

This module intentionally stores plans only.  It does not schedule tasks, lock
resources, approve a plan, or execute tools.  Those choices remain owned by the
runtime command layer.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from .contracts import Plan, PlanRevision


DEFAULT_PLAN_DIRECTORY = Path(".agenteglobal") / "plans"
MAX_PLAN_FILE_BYTES = 1_048_576
_PLAN_ID_RE = re.compile(r"^PLAN-([0-9]{4,})$")
_PLAN_REFERENCE_RE = re.compile(r"^(PLAN-[0-9]{4,})(?:-r([1-9][0-9]*))?$")


class PlanStoreError(RuntimeError):
    """Base error for local plan persistence failures."""


class PlanNotFoundError(PlanStoreError):
    """The requested PLAN-ID or revision reference does not exist."""


class PlanConflictError(PlanStoreError):
    """A caller attempted to overwrite or revise a stale plan snapshot."""


class PlanStore:
    """Persist immutable revision snapshots plus a current-plan pointer.

    A base reference such as ``PLAN-0001`` loads the current revision.  A
    revision reference such as ``PLAN-0001-r2`` loads that immutable snapshot.
    Files are always JSON and are confined beneath ``workspace``.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        directory: Path | str = DEFAULT_PLAN_DIRECTORY,
        max_plan_file_bytes: int = MAX_PLAN_FILE_BYTES,
    ) -> None:
        if max_plan_file_bytes < 1:
            raise ValueError("max_plan_file_bytes must be positive.")
        try:
            root = Path(workspace).resolve(strict=True)
        except OSError as error:
            raise PlanStoreError(f"Cannot resolve workspace: {workspace}") from error
        if not root.is_dir():
            raise PlanStoreError("workspace must be an existing directory.")
        relative_directory = Path(directory)
        if (
            relative_directory.is_absolute()
            or relative_directory.drive
            or ".." in relative_directory.parts
            or any(":" in part for part in relative_directory.parts)
        ):
            raise ValueError("Plan directory must be a relative path inside the workspace.")
        if not relative_directory.parts:
            raise ValueError("Plan directory cannot be empty.")

        self._workspace = root
        self._directory_relative = relative_directory
        self._max_plan_file_bytes = max_plan_file_bytes
        self._lock = threading.RLock()
        self._plans_directory = self._ensure_directory()

    @property
    def plans_directory(self) -> Path:
        return self._plans_directory

    def _ensure_directory(self) -> Path:
        current = self._workspace
        for part in self._directory_relative.parts:
            if part in ("", "."):
                continue
            candidate = current / part
            if candidate.exists():
                if candidate.is_symlink() or not candidate.is_dir():
                    raise PlanStoreError("Plan directory cannot contain symlinks or regular files.")
            else:
                try:
                    candidate.mkdir()
                except OSError as error:
                    raise PlanStoreError(f"Cannot create plan directory: {candidate}") from error
            current = candidate
        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(self._workspace)
        except (OSError, ValueError) as error:
            raise PlanStoreError("Plan directory resolves outside the workspace.") from error
        return resolved

    @staticmethod
    def _parse_reference(reference: str) -> tuple[str, int | None]:
        if not isinstance(reference, str):
            raise ValueError("Plan reference must be a string.")
        match = _PLAN_REFERENCE_RE.fullmatch(reference.strip())
        if match is None:
            raise ValueError("Plan reference must be PLAN-XXXX or PLAN-XXXX-rN.")
        return match.group(1), int(match.group(2)) if match.group(2) is not None else None

    def _path_for(self, plan_id: str, revision: int | None = None) -> Path:
        if _PLAN_ID_RE.fullmatch(plan_id) is None:
            raise ValueError("Invalid PLAN-ID.")
        name = plan_id if revision is None else f"{plan_id}-r{revision}"
        target = self._plans_directory / f"{name}.json"
        if target.parent != self._plans_directory:
            raise PlanStoreError("Plan path escapes the plan directory.")
        if target.exists() and target.is_symlink():
            raise PlanStoreError("Plan files cannot be symlinks.")
        return target

    def next_plan_id(self) -> str:
        """Return the next monotonic PLAN-ID based on persisted base files."""
        with self._lock:
            highest = 0
            for entry in self._plans_directory.iterdir():
                if not entry.is_file() or entry.is_symlink():
                    continue
                match = re.fullmatch(r"PLAN-([0-9]{4,})\.json", entry.name)
                if match is not None:
                    highest = max(highest, int(match.group(1)))
            return f"PLAN-{highest + 1:04d}"

    def _atomic_write(self, destination: Path, payload: bytes) -> None:
        if len(payload) > self._max_plan_file_bytes:
            raise PlanStoreError(
                f"Serialized plan exceeds configured limit of {self._max_plan_file_bytes} bytes."
            )
        if destination.exists() and destination.is_symlink():
            raise PlanStoreError("Refusing to replace a symlinked plan file.")
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".plan-",
                suffix=".tmp",
                dir=self._plans_directory,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, destination)
            temporary_name = None
        except OSError as error:
            raise PlanStoreError(f"Cannot persist plan {destination.name}: {error}") from error
        finally:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def _serialize(self, plan: Plan) -> bytes:
        return (
            json.dumps(
                plan.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def _load_path(self, path: Path, *, expected_id: str, expected_revision: int | None) -> Plan:
        if not path.exists() or not path.is_file():
            raise PlanNotFoundError(f"Plan not found: {path.stem}")
        if path.is_symlink():
            raise PlanStoreError("Plan files cannot be symlinks.")
        try:
            size = path.stat().st_size
        except OSError as error:
            raise PlanStoreError(f"Cannot stat plan file: {path.name}") from error
        if size > self._max_plan_file_bytes:
            raise PlanStoreError(f"Plan file exceeds configured limit: {path.name}")
        try:
            raw = path.read_bytes()
            plan = Plan.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as error:
            raise PlanStoreError(f"Invalid persisted plan: {path.name}") from error
        if plan.plan_id != expected_id:
            raise PlanStoreError("Persisted plan ID does not match its filename.")
        if expected_revision is not None and plan.revision != expected_revision:
            raise PlanStoreError("Persisted plan revision does not match its filename.")
        return plan

    def save(self, plan: Plan) -> Plan:
        """Create the first immutable revision and atomically publish it as current."""
        if not isinstance(plan, Plan):
            raise TypeError("plan must be a Plan.")
        if plan.revision != 1:
            raise ValueError("Use revise() to persist revisions after the initial plan.")
        with self._lock:
            revision_path = self._path_for(plan.plan_id, plan.revision)
            pointer_path = self._path_for(plan.plan_id)
            if revision_path.exists() or pointer_path.exists():
                raise PlanConflictError(f"Plan already exists: {plan.plan_id}")
            payload = self._serialize(plan)
            self._atomic_write(revision_path, payload)
            try:
                self._atomic_write(pointer_path, payload)
            except PlanStoreError:
                # The immutable revision remains valid evidence even if the current pointer fails.
                raise
        return plan

    def load(self, reference: str) -> Plan:
        """Load the current plan or a specific immutable plan revision."""
        plan_id, revision = self._parse_reference(reference)
        with self._lock:
            return self._load_path(self._path_for(plan_id, revision), expected_id=plan_id, expected_revision=revision)

    def revise(self, plan: Plan, *, reason: str, expected_revision: int | None = None) -> Plan:
        """Persist a new immutable revision after checking the caller's base snapshot.

        ``plan`` may contain the desired field changes, but its revision is ignored;
        the store owns revision numbering and timestamps.
        """
        if not isinstance(plan, Plan):
            raise TypeError("plan must be a Plan.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string.")
        with self._lock:
            current = self.load(plan.plan_id)
            expected = current.revision if expected_revision is None else expected_revision
            if expected != current.revision:
                raise PlanConflictError(
                    f"Stale plan revision for {plan.plan_id}: expected {expected}, current {current.revision}."
                )
            if plan.created_at != current.created_at:
                raise PlanConflictError("A revised plan must preserve the original created_at.")
            next_revision = current.revision + 1
            history = [*current.revision_history, PlanRevision(revision=next_revision, reason=reason)]
            candidate_payload = plan.model_dump(mode="python")
            candidate_payload.update(
                revision=next_revision,
                revision_history=history,
                created_at=current.created_at,
                updated_at=datetime.now(timezone.utc),
            )
            candidate = Plan.model_validate(candidate_payload)
            revision_path = self._path_for(candidate.plan_id, candidate.revision)
            if revision_path.exists():
                raise PlanConflictError(f"Plan revision already exists: {candidate.reference}")
            payload = self._serialize(candidate)
            self._atomic_write(revision_path, payload)
            self._atomic_write(self._path_for(candidate.plan_id), payload)
            return candidate

    def list_ids(self) -> tuple[str, ...]:
        """List current base PLAN-IDs without exposing paths outside the workspace."""
        with self._lock:
            identifiers: list[str] = []
            for entry in self._plans_directory.iterdir():
                if entry.is_file() and not entry.is_symlink() and _PLAN_ID_RE.fullmatch(entry.stem):
                    identifiers.append(entry.stem)
            return tuple(sorted(identifiers, key=lambda item: int(item.removeprefix("PLAN-"))))
