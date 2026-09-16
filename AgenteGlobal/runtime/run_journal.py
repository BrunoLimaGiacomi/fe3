"""Durable, secret-free journal for resumable DAG runs.

The scheduler remains the in-memory source of operational state.  This module
only records bounded snapshots and derives a conservative recovery decision;
it never marks an unfinished task as completed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .contracts import AgentResult, Plan, TaskLifecycleStatus, TaskSpec, TaskState
from .security_text import is_sensitive_key_name, redact_sensitive_text


DEFAULT_RUN_DIRECTORY = Path(".agenteglobal") / "runs"
MAX_RUN_FILE_BYTES = 4 * 1024 * 1024
MAX_RUN_ID_LENGTH = 128
MAX_RUN_TASKS = 500
MAX_RUN_EVENTS = 2_000
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class RunJournalError(RuntimeError):
    """Base error for durable run persistence and recovery."""


class RunNotFoundError(RunJournalError):
    pass


class RunCorruptError(RunJournalError):
    pass


class RunPlanMismatchError(RunJournalError):
    pass


class ReplayProtectionError(RunJournalError):
    pass


class RunLifecycleStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECOVERY_REVIEW_REQUIRED = "recovery_review_required"


class SideEffectState(StrEnum):
    NONE = "none"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    AMBIGUOUS = "ambiguous"


class RecoveryDisposition(StrEnum):
    RESUMABLE = "resumable"
    RECOVERY_REVIEW_REQUIRED = "recovery_review_required"
    TERMINAL = "terminal"


# Compatibility aliases make the state vocabulary easy to discover without
# introducing a second representation.
RunStatus = RunLifecycleStatus
TaskSideEffectState = SideEffectState


class JournalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_run_id(value: str) -> str:
    if not isinstance(value, str) or _RUN_ID_RE.fullmatch(value) is None:
        raise ValueError("run_id must be a short safe identifier")
    return value


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    """Bound and redact values before they can reach durable JSON."""

    if depth > 12:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (raw_key, child) in enumerate(value.items()):
            if index >= 512:
                result["_truncated"] = True
                break
            key = str(raw_key)[:256]
            # These contract fields contain a digest or an integer budget, not
            # credentials. The generic key-name rule otherwise classifies
            # every ``*_key``/``*token*`` field as sensitive.
            safe_contract_fields = {"idempotency_key", "token_budget"}
            sensitive = is_sensitive_key_name(key) and key.lower() not in safe_contract_fields
            result[key] = "[REDACTED]" if sensitive else _sanitize(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize(item, depth=depth + 1) for item in list(value)[:512]]
    if isinstance(value, str):
        return redact_sensitive_text(value[:20_000])
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2_000]


class TaskExecutionRecord(JournalModel):
    task_id: str = Field(min_length=1, max_length=128)
    status: TaskLifecycleStatus = TaskLifecycleStatus.PENDING
    attempt: int = Field(default=0, ge=0, le=10_000)
    read_only: bool = True
    idempotent: bool = False
    side_effect_state: SideEffectState = SideEffectState.NONE
    idempotency_key: str = Field(default="", max_length=128)
    result: AgentResult | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list, max_length=1_000)
    validation_results: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    errors: list[str] = Field(default_factory=list, max_length=200)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_reason: str | None = Field(default=None, max_length=10_000)

    @field_validator("idempotency_key")
    @classmethod
    def _key_is_safe(cls, value: str) -> str:
        if value and re.fullmatch(r"[a-f0-9]{64}", value) is None:
            raise ValueError("idempotency_key must be a SHA-256 hex digest")
        return value


class RunRecord(JournalModel):
    run_id: str = Field(min_length=1, max_length=MAX_RUN_ID_LENGTH)
    goal_id: str | None = Field(default=None, max_length=128)
    plan_id: str = Field(min_length=1, max_length=64)
    plan_revision: int = Field(ge=1, le=100_000)
    objective: str = Field(default="", max_length=20_000)
    plan_digest: str = Field(min_length=64, max_length=64)
    status: RunLifecycleStatus = RunLifecycleStatus.PENDING
    task_specs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    dependencies: dict[str, list[str]] = Field(default_factory=dict)
    tasks: dict[str, TaskExecutionRecord] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list, max_length=1_000)
    validation_results: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    errors: list[str] = Field(default_factory=list, max_length=200)
    event_count: int = Field(default=0, ge=0, le=MAX_RUN_EVENTS)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    final_state: str | None = Field(default=None, max_length=128)

    @field_validator("run_id")
    @classmethod
    def _run_id_is_safe(cls, value: str) -> str:
        return _safe_run_id(value)

    @field_validator("plan_digest")
    @classmethod
    def _digest_is_safe(cls, value: str) -> str:
        if re.fullmatch(r"[a-fA-F0-9]{64}", value) is None:
            raise ValueError("plan_digest must be a SHA-256 hex digest")
        return value.lower()

    @property
    def task_states(self) -> dict[str, TaskExecutionRecord]:
        return self.tasks


class RunSnapshot(JournalModel):
    run_id: str = Field(min_length=1, max_length=MAX_RUN_ID_LENGTH)
    plan_id: str = Field(min_length=1, max_length=64)
    plan_revision: int = Field(ge=1, le=100_000)
    plan_digest: str = Field(min_length=64, max_length=64)
    status: RunLifecycleStatus
    tasks: dict[str, TaskExecutionRecord] = Field(default_factory=dict)
    dependencies: dict[str, list[str]] = Field(default_factory=dict)
    captured_at: datetime = Field(default_factory=_now)

    @property
    def task_states(self) -> dict[str, TaskExecutionRecord]:
        return self.tasks


class RunRecoveryState(JournalModel):
    run_id: str = Field(min_length=1, max_length=MAX_RUN_ID_LENGTH)
    disposition: RecoveryDisposition
    completed_task_ids: list[str] = Field(default_factory=list, max_length=MAX_RUN_TASKS)
    retry_task_ids: list[str] = Field(default_factory=list, max_length=MAX_RUN_TASKS)
    recovery_review_task_ids: list[str] = Field(default_factory=list, max_length=MAX_RUN_TASKS)
    terminal_task_ids: list[str] = Field(default_factory=list, max_length=MAX_RUN_TASKS)
    reason: str = Field(default="", max_length=10_000)
    generated_at: datetime = Field(default_factory=_now)

    @property
    def resumable(self) -> bool:
        return self.disposition is RecoveryDisposition.RESUMABLE


def plan_digest(plan: Plan) -> str:
    payload = json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def idempotency_key(run_id: str, task_id: str) -> str:
    return hashlib.sha256(f"{_safe_run_id(run_id)}:{task_id}".encode("utf-8")).hexdigest()


class RunJournal:
    """Atomic run persistence confined to one workspace."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        directory: Path | str = DEFAULT_RUN_DIRECTORY,
        max_file_bytes: int = MAX_RUN_FILE_BYTES,
    ) -> None:
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        try:
            root = Path(workspace).resolve(strict=True)
        except OSError as error:
            raise RunJournalError("workspace cannot be resolved") from error
        if not root.is_dir():
            raise RunJournalError("workspace must be an existing directory")
        relative = Path(directory)
        if relative.is_absolute() or relative.drive or ".." in relative.parts or not relative.parts:
            raise ValueError("run directory must be a relative path inside workspace")
        current = root
        for part in relative.parts:
            if part in {"", "."}:
                continue
            candidate = current / part
            if candidate.exists():
                if candidate.is_symlink() or not candidate.is_dir():
                    raise RunJournalError("run directory cannot contain symlinks or regular files")
            else:
                try:
                    candidate.mkdir()
                except OSError as error:
                    raise RunJournalError("cannot create run directory") from error
            current = candidate
        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise RunJournalError("run directory resolves outside workspace") from error
        self.workspace = root
        self.directory = resolved
        self.max_file_bytes = max_file_bytes
        self._lock = threading.RLock()
        self._active_run_id: str | None = None

    def _path(self, run_id: str, *, snapshot: bool = False) -> Path:
        safe = _safe_run_id(run_id)
        suffix = ".snapshot.json" if snapshot else ".json"
        return self.directory / f"{safe}{suffix}"

    def _atomic_write(self, destination: Path, value: Mapping[str, Any]) -> None:
        payload = (json.dumps(_sanitize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > self.max_file_bytes:
            raise RunJournalError("serialized run exceeds configured limit")
        if destination.exists() and destination.is_symlink():
            raise RunJournalError("refusing to replace symlinked run file")
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".run-", suffix=".tmp", dir=self.directory)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # OneDrive, antivirus and indexers can hold the destination for a
            # few milliseconds on Windows. Keep the operation atomic, but
            # tolerate that transient sharing violation within a tiny bound.
            for attempt, delay in enumerate((0.01, 0.025, 0.05, 0.0)):
                try:
                    os.replace(temporary_name, destination)
                    break
                except PermissionError:
                    if attempt == 3:
                        raise
                    time.sleep(delay)
            temporary_name = None
            try:
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Windows does not permit opening directories this way; the
                # file itself was already flushed and atomically replaced.
                pass
        except OSError as error:
            raise RunJournalError("cannot atomically persist run") from error
        finally:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def _load_path(self, path: Path, run_id: str) -> RunRecord:
        if not path.exists() or not path.is_file():
            raise RunNotFoundError(f"run not found: {run_id}")
        if path.is_symlink():
            raise RunCorruptError("run files cannot be symlinks")
        try:
            if path.stat().st_size > self.max_file_bytes:
                raise RunCorruptError("run file exceeds configured limit")
            # JSON serialization turns enum/datetime values into strings.  The
            # JSON-aware Pydantic entrypoint parses those wire representations
            # while still enforcing the strict model contract.
            record = RunRecord.model_validate_json(path.read_bytes())
        except RunJournalError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
            raise RunCorruptError(f"invalid persisted run: {run_id}") from error
        if record.run_id != run_id:
            raise RunCorruptError("persisted run ID does not match its filename")
        return record

    def _snapshot_for(self, record: RunRecord) -> RunSnapshot:
        return RunSnapshot(
            run_id=record.run_id,
            plan_id=record.plan_id,
            plan_revision=record.plan_revision,
            plan_digest=record.plan_digest,
            status=record.status,
            tasks=record.tasks,
            dependencies=record.dependencies,
        )

    def create_run(self, plan: Plan, *, goal_id: str | None = None, run_id: str | None = None) -> RunRecord:
        if not isinstance(plan, Plan):
            raise TypeError("plan must be a Plan")
        with self._lock:
            selected_id = run_id or self._next_run_id()
            _safe_run_id(selected_id)
            record = RunRecord(
                run_id=selected_id,
                goal_id=goal_id,
                plan_id=plan.plan_id,
                plan_revision=plan.revision,
                objective=plan.objective,
                plan_digest=plan_digest(plan),
                task_specs={task.task_id: task.model_dump(mode="json") for task in plan.tasks},
                dependencies={task.task_id: list(task.dependencies) for task in plan.tasks},
                tasks={
                    task.task_id: TaskExecutionRecord(
                        task_id=task.task_id,
                        read_only=task.read_only,
                        idempotent=bool(task.metadata.get("idempotent", False)),
                        idempotency_key=idempotency_key(selected_id, task.task_id),
                    )
                    for task in plan.tasks
                },
            )
            if self._path(selected_id).exists():
                raise RunJournalError(f"run already exists: {selected_id}")
            self._persist(record)
            self._active_run_id = selected_id
            return record

    def register_tasks(self, run_id: str, tasks: list[TaskSpec] | tuple[TaskSpec, ...]) -> RunRecord:
        """Atomically add runtime-created repair tasks before they can execute."""

        selected = tuple(tasks)
        if not selected:
            return self.load(run_id)
        if any(not isinstance(task, TaskSpec) for task in selected):
            raise TypeError("tasks must contain only TaskSpec")
        with self._lock:
            record = self.load(run_id)
            incoming_ids = [task.task_id for task in selected]
            if len(set(incoming_ids)) != len(incoming_ids):
                raise RunJournalError("duplicate task IDs in runtime registration")
            if len(record.tasks) + sum(task_id not in record.tasks for task_id in incoming_ids) > MAX_RUN_TASKS:
                raise RunJournalError("run task limit exceeded")
            known_ids = {*record.tasks, *incoming_ids}
            unknown_dependencies = sorted(
                {
                    dependency
                    for task in selected
                    for dependency in task.dependencies
                    if dependency not in known_ids
                }
            )
            if unknown_dependencies:
                raise RunJournalError(
                    f"runtime tasks reference unknown dependencies: {unknown_dependencies}"
                )
            for task in selected:
                serialized = task.model_dump(mode="json")
                existing = record.task_specs.get(task.task_id)
                if existing is not None:
                    comparable_existing = json.loads(json.dumps(existing))
                    legacy_limits = comparable_existing.get("limits")
                    serialized_limits = serialized.get("limits")
                    if (
                        isinstance(legacy_limits, dict)
                        and isinstance(serialized_limits, dict)
                        and legacy_limits.get("token_budget") == "[REDACTED]"
                    ):
                        # Journals produced before token_budget was recognized
                        # as a non-secret contract field lost only this value.
                        legacy_limits["token_budget"] = serialized_limits.get("token_budget")
                    if comparable_existing != _sanitize(serialized):
                        raise RunJournalError(
                            f"runtime task conflicts with durable task: {task.task_id}"
                        )
                    continue
                record.task_specs[task.task_id] = serialized
                record.dependencies[task.task_id] = list(task.dependencies)
                record.tasks[task.task_id] = TaskExecutionRecord(
                    task_id=task.task_id,
                    read_only=task.read_only,
                    idempotent=bool(task.metadata.get("idempotent", False)),
                    idempotency_key=idempotency_key(run_id, task.task_id),
                    last_reason="runtime_task_registered",
                )
            self._persist(record)
            return record

    def _next_run_id(self) -> str:
        highest = 0
        for item in self.directory.iterdir():
            match = re.fullmatch(r"RUN-([0-9]{4,})\.json", item.name)
            if match and item.is_file() and not item.is_symlink():
                highest = max(highest, int(match.group(1)))
        return f"RUN-{highest + 1:04d}"

    def _persist(self, record: RunRecord) -> RunRecord:
        record.updated_at = _now()
        self._atomic_write(self._path(record.run_id), record.model_dump(mode="json"))
        self._atomic_write(self._path(record.run_id, snapshot=True), self._snapshot_for(record).model_dump(mode="json"))
        return record

    def save(self, record: RunRecord) -> RunRecord:
        if not isinstance(record, RunRecord):
            raise TypeError("record must be a RunRecord")
        with self._lock:
            return self._persist(record)

    persist = save

    def load(self, run_id: str) -> RunRecord:
        with self._lock:
            record = self._load_path(self._path(run_id), run_id)
            self._active_run_id = run_id
            return record

    def load_snapshot(self, run_id: str) -> RunSnapshot:
        _safe_run_id(run_id)
        path = self._path(run_id, snapshot=True)
        if not path.exists() or not path.is_file() or path.is_symlink():
            raise RunNotFoundError(f"run snapshot not found: {run_id}")
        try:
            if path.stat().st_size > self.max_file_bytes:
                raise RunCorruptError("run snapshot exceeds configured limit")
            return RunSnapshot.model_validate_json(path.read_bytes())
        except RunJournalError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
            raise RunCorruptError(f"invalid persisted run snapshot: {run_id}") from error

    def update_task(
        self,
        run_id: str,
        task_id: str,
        status: TaskLifecycleStatus,
        *,
        attempt: int | None = None,
        result: AgentResult | None = None,
        reason: str | None = None,
        side_effect_state: SideEffectState | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        validation_results: list[dict[str, Any]] | None = None,
        errors: list[str] | None = None,
    ) -> TaskExecutionRecord:
        with self._lock:
            record = self.load(run_id)
            current = record.tasks.get(task_id)
            if current is None:
                raise RunJournalError(f"unknown task in run: {task_id}")
            now = _now()
            terminal = status in {
                TaskLifecycleStatus.COMPLETED,
                TaskLifecycleStatus.BLOCKED,
                TaskLifecycleStatus.FAILED_FINAL,
                TaskLifecycleStatus.CANCELLED,
            }
            updated = current.model_copy(
                update={
                    "status": status,
                    "attempt": current.attempt if attempt is None else attempt,
                    "result": result if result is not None else current.result,
                    "last_reason": reason,
                    "side_effect_state": side_effect_state or current.side_effect_state,
                    "artifacts": artifacts if artifacts is not None else current.artifacts,
                    "validation_results": validation_results if validation_results is not None else current.validation_results,
                    "errors": errors if errors is not None else current.errors,
                    "finished_at": now if terminal else current.finished_at,
                }
            )
            record.tasks[task_id] = updated
            self._persist(record)
            return updated

    def mark_task_started(
        self,
        run_id: str,
        task: TaskSpec,
        *,
        attempt: int,
        allow_replay: bool = False,
    ) -> TaskExecutionRecord:
        with self._lock:
            record = self.load(run_id)
            current = record.tasks.get(task.task_id)
            if current is None:
                raise RunJournalError(f"unknown task in run: {task.task_id}")
            if current.status is TaskLifecycleStatus.COMPLETED and not allow_replay:
                raise ReplayProtectionError(f"completed task cannot be replayed: {task.task_id}")
            key = idempotency_key(run_id, task.task_id)
            updated = current.model_copy(
                update={
                    "status": TaskLifecycleStatus.RUNNING,
                    "attempt": attempt,
                    "read_only": task.read_only,
                    "idempotent": bool(task.metadata.get("idempotent", False)),
                    "idempotency_key": key,
                    "side_effect_state": SideEffectState.IN_PROGRESS,
                    "started_at": _now(),
                    "finished_at": None,
                    "last_reason": "task_started",
                }
            )
            record.tasks[task.task_id] = updated
            record.status = RunLifecycleStatus.RUNNING
            self._persist(record)
            return updated

    def mark_run(self, run_id: str, status: RunLifecycleStatus, *, reason: str | None = None) -> RunRecord:
        with self._lock:
            record = self.load(run_id)
            record.status = status
            record.final_state = status.value if status in {
                RunLifecycleStatus.COMPLETED,
                RunLifecycleStatus.FAILED,
                RunLifecycleStatus.CANCELLED,
                RunLifecycleStatus.RECOVERY_REVIEW_REQUIRED,
            } else record.final_state
            if reason:
                record.errors = [*record.errors, redact_sensitive_text(reason)][:200]
            return self._persist(record)

    def record_scheduler_result(self, run_id: str, states: tuple[TaskState, ...] | list[TaskState]) -> RunRecord:
        with self._lock:
            record = self.load(run_id)
            for state in states:
                if state.task_id not in record.tasks:
                    continue
                self.update_task(
                    run_id,
                    state.task_id,
                    state.status,
                    # A seeded completed state starts at scheduler attempt 0;
                    # never erase the durable attempt count on resume.
                    attempt=max(record.tasks[state.task_id].attempt, state.attempt),
                    result=state.result,
                    reason=state.reason,
                    side_effect_state=(
                        SideEffectState.COMPLETED
                        if state.status is TaskLifecycleStatus.COMPLETED
                        else SideEffectState.AMBIGUOUS
                        if state.status is TaskLifecycleStatus.RUNNING
                        and not record.tasks[state.task_id].read_only
                        else record.tasks[state.task_id].side_effect_state
                    ),
                )
            return self.load(run_id)

    def record_event(self, event: Any, *, run_id: str | None = None) -> RunRecord | None:
        selected = run_id or self._active_run_id
        if selected is None:
            return None
        name = str(getattr(event, "name", "") or (event.get("name", "") if isinstance(event, Mapping) else ""))
        payload = getattr(event, "payload", None) or (event.get("payload", {}) if isinstance(event, Mapping) else {})
        if not isinstance(payload, Mapping):
            return None
        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            return None
        event_status = name.removeprefix("task.")
        try:
            status = TaskLifecycleStatus(event_status)
        except ValueError:
            return None
        with self._lock:
            record = self.load(selected)
            if task_id not in record.tasks:
                return record
            record.event_count = min(MAX_RUN_EVENTS, record.event_count + 1)
            current = record.tasks[task_id]
            record.tasks[task_id] = current.model_copy(
                update={
                    "status": status,
                    "attempt": int(payload.get("attempt", current.attempt)),
                    "last_reason": str(payload.get("reason"))[:10_000] if payload.get("reason") else current.last_reason,
                    "started_at": current.started_at or _now() if status is TaskLifecycleStatus.RUNNING else current.started_at,
                }
            )
            return self._persist(record)

    def recovery_state(self, run_id: str, *, plan: Plan | None = None) -> RunRecoveryState:
        record = self.load(run_id)
        self._validate_plan(record, plan)
        completed: list[str] = []
        retry: list[str] = []
        review: list[str] = []
        terminal: list[str] = []
        for task_id, task in record.tasks.items():
            if task.status is TaskLifecycleStatus.COMPLETED and task.result is not None:
                completed.append(task_id)
            elif task.status in {TaskLifecycleStatus.PENDING, TaskLifecycleStatus.READY, TaskLifecycleStatus.FAILED_RETRYABLE}:
                retry.append(task_id)
            elif task.status is TaskLifecycleStatus.RUNNING:
                if task.read_only or task.idempotent:
                    retry.append(task_id)
                else:
                    review.append(task_id)
            elif task.status in {TaskLifecycleStatus.BLOCKED, TaskLifecycleStatus.FAILED_FINAL, TaskLifecycleStatus.CANCELLED}:
                terminal.append(task_id)
            else:
                review.append(task_id)
        disposition = (
            RecoveryDisposition.RECOVERY_REVIEW_REQUIRED
            if review
            else RecoveryDisposition.RESUMABLE
            if retry
            else RecoveryDisposition.TERMINAL
        )
        return RunRecoveryState(
            run_id=run_id,
            disposition=disposition,
            completed_task_ids=completed,
            retry_task_ids=retry,
            recovery_review_task_ids=review,
            terminal_task_ids=terminal,
            reason=(f"tasks require review: {','.join(review)}" if review else "recovery decision derived from durable task states"),
        )

    def _validate_plan(self, record: RunRecord, plan: Plan | None) -> None:
        if plan is None:
            return
        if plan.plan_id != record.plan_id or plan.revision != record.plan_revision:
            raise RunPlanMismatchError(
                f"run {record.run_id} targets {record.plan_id}-r{record.plan_revision}, supplied {plan.reference}"
            )
        if plan_digest(plan) != record.plan_digest:
            raise RunPlanMismatchError("plan content is incompatible with the durable run")

    def prepare_resume(
        self,
        run_id: str,
        *,
        plan: Plan | None = None,
        allow_recovery_review: bool = False,
    ) -> tuple[RunRecord, RunRecoveryState]:
        with self._lock:
            record = self.load(run_id)
            state = self.recovery_state(run_id, plan=plan)
            if state.recovery_review_task_ids and not allow_recovery_review:
                record.status = RunLifecycleStatus.RECOVERY_REVIEW_REQUIRED
                self._persist(record)
                return record, state
            for task_id in state.retry_task_ids:
                task = record.tasks[task_id]
                record.tasks[task_id] = task.model_copy(
                    update={"status": TaskLifecycleStatus.PENDING, "side_effect_state": SideEffectState.NONE, "finished_at": None}
                )
            # A terminal run has no work to resume.  Keep its durable terminal
            # state stable across repeated /resume calls.
            if state.disposition is RecoveryDisposition.TERMINAL:
                return record, state
            record.status = RunLifecycleStatus.RUNNING
            self._persist(record)
            return record, state

    def seed_completed_results(self, run_id: str, *, plan: Plan | None = None) -> dict[str, AgentResult]:
        record = self.load(run_id)
        self._validate_plan(record, plan)
        return {
            task_id: task.result
            for task_id, task in record.tasks.items()
            if task.status is TaskLifecycleStatus.COMPLETED and task.result is not None
        }


__all__ = [
    "DEFAULT_RUN_DIRECTORY",
    "MAX_RUN_FILE_BYTES",
    "RecoveryDisposition",
    "ReplayProtectionError",
    "RunCorruptError",
    "RunJournal",
    "RunJournalError",
    "RunLifecycleStatus",
    "RunNotFoundError",
    "RunPlanMismatchError",
    "RunRecord",
    "RunRecoveryState",
    "RunSnapshot",
    "RunStatus",
    "SideEffectState",
    "TaskExecutionRecord",
    "TaskSideEffectState",
    "idempotency_key",
    "plan_digest",
]
