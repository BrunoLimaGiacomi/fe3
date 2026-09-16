"""Bounded, secret-free operational state for CLI/TUI inspection.

The projection consumes runtime events but deliberately retains only stable
identifiers, counters and statuses.  Prompts, model deltas, tool output,
arguments and exception text are never copied into the snapshot.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Mapping

from .events import RuntimeEvent


_SAFE_SCALARS = frozenset(
    {
        "agent", "attempt", "blocker_count", "capacity_tokens",
        "convergence_pass", "context_utilization", "dependency_count",
        "context_window_tokens",
        "duration_seconds", "gap_count", "hit_count", "model",
        "output_reserve_tokens", "plan_id", "remaining_tokens", "retry_count",
        "session_state_tokens", "skill_name", "source", "stage", "status",
        "task_count", "task_id", "token_footprint", "tool", "used_tokens",
        "workflow", "hot_context_tokens", "loaded_skills_tokens",
        "retrieved_context_tokens", "artifact_tokens", "provider",
        "version", "trust", "origin",
        "depth", "cache_hit", "files_read", "symbols_inspected", "relationships_found",
        "indexed_files", "unchanged_files", "new_files", "modified_files", "deleted_files",
    }
)
_TERMINAL_TASK_EVENTS = {
    "task.blocked", "task.completed", "task.failed_final", "task.cancelled"
}


@dataclass(slots=True)
class OperationalState:
    """Event-sourced status projection used by human-facing commands."""

    trace_limit: int = 160
    started_monotonic: float = field(default_factory=time.monotonic)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _trace: deque[dict[str, Any]] = field(init=False, repr=False)
    _run: dict[str, Any] = field(default_factory=lambda: {"status": "idle"}, init=False)
    _workflow: dict[str, Any] = field(default_factory=dict, init=False)
    _spec: dict[str, Any] = field(default_factory=lambda: {"status": "not_applicable"}, init=False)
    _scheduler: dict[str, Any] = field(default_factory=lambda: {"status": "idle"}, init=False)
    _tasks: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _agents: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _context: dict[str, Any] = field(default_factory=dict, init=False)
    _skills: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _convergence: dict[str, Any] = field(default_factory=lambda: {"status": "not_started", "passes": 0}, init=False)
    _counters: dict[str, int] = field(default_factory=dict, init=False)
    _waiting: dict[str, Any] = field(default_factory=dict, init=False)
    _codeintel: dict[str, Any] = field(default_factory=lambda: {"status": "idle"}, init=False)

    def __post_init__(self) -> None:
        if not 20 <= self.trace_limit <= 2_000:
            raise ValueError("trace_limit precisa estar entre 20 e 2000")
        self._trace = deque(maxlen=self.trace_limit)

    @staticmethod
    def _safe_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in payload.items()
            if key in _SAFE_SCALARS
            and isinstance(value, (str, int, float, bool, type(None)))
            and not (isinstance(value, str) and len(value) > 256)
        }

    def __call__(self, event: RuntimeEvent) -> None:
        payload = self._safe_payload(event.payload)
        with self._lock:
            self._trace.append(
                {
                    "sequence": event.sequence,
                    "time": event.timestamp.isoformat(),
                    "event": event.name,
                    "source": event.source,
                    "status": str(payload.get("status") or ""),
                }
            )
            name = event.name
            if name == "run.started":
                self._run = {"status": "running"}
            elif name == "run.completed":
                self._run = {"status": str(payload.get("status") or "completed")}
            elif name == "workflow.state_changed":
                self._workflow = payload
                workflow_status = str(payload.get("status") or "")
                if workflow_status in {"awaiting_approval", "approval_required", "paused"}:
                    self._waiting = {"reason": "human approval", "intervention_required": True}
            elif name == "spec_workflow.stage_changed":
                self._spec = payload
            elif name.startswith("scheduler."):
                self._scheduler = {"status": str(payload.get("status") or name.split(".", 1)[1])}
            elif name.startswith("task."):
                task_id = str(payload.get("task_id") or "")
                if task_id:
                    current = self._tasks.setdefault(task_id, {"task_id": task_id})
                    current.update(payload)
                    current["status"] = str(payload.get("status") or name.split(".", 1)[1])
                    if name == "task.failed_retryable":
                        self._counters["retries"] = self._counters.get("retries", 0) + 1
                    elif name == "task.blocked":
                        self._waiting = {"reason": "task blocked", "intervention_required": True}
                    elif name in _TERMINAL_TASK_EVENTS and not any(
                        item.get("status") == "blocked" for item in self._tasks.values()
                    ):
                        self._waiting = {}
            elif name.startswith("agent."):
                agent = str(payload.get("agent") or "AgenteGlobal")
                current = self._agents.setdefault(agent, {"agent": agent})
                current.update(payload)
                current["status"] = str(payload.get("status") or name.split(".", 1)[1])
                if name == "agent.waiting_model":
                    self._waiting = {"reason": "model", "agent": agent, "intervention_required": False}
                elif name == "agent.completed":
                    self._waiting = {}
            elif name == "context.prepared":
                self._context = payload
            elif name == "skill.loaded":
                skill_name = str(payload.get("skill_name") or "")
                if skill_name:
                    self._skills[skill_name] = {**payload, "loaded": True}
            elif name == "artifact.created":
                self._counters["artifacts"] = self._counters.get("artifacts", 0) + 1
            elif name.startswith("convergence."):
                self._convergence.update(payload)
                self._convergence["status"] = str(payload.get("status") or name.split(".", 1)[1])
                pass_number = payload.get("convergence_pass")
                if isinstance(pass_number, int):
                    self._convergence["passes"] = max(int(self._convergence.get("passes", 0)), pass_number)
            elif name == "analyze.blocked":
                self._waiting = {"reason": "analyze blockers", "intervention_required": True}
            elif name == "plan.approval_recorded":
                self._waiting = {}
            elif name == "code_index.updated":
                self._codeintel = {**payload, "status": "indexed"}
            elif name == "exploration.started":
                self._codeintel.update(payload)
                self._codeintel["status"] = "exploring"
                self._waiting = {"reason": "codebase exploration", "intervention_required": False}
            elif name == "exploration.completed":
                self._codeintel.update(payload)
                self._codeintel["status"] = "completed"
                self._waiting = {}
            elif name.endswith(".failed"):
                self._counters["failures"] = self._counters.get("failures", 0) + 1

    def snapshot(self, *, trace_limit: int = 20) -> dict[str, Any]:
        with self._lock:
            tasks = [dict(value) for _, value in sorted(self._tasks.items())]
            agents = [dict(value) for _, value in sorted(self._agents.items())]
            return {
                "started_at": self.started_at,
                "elapsed_seconds": max(0.0, time.monotonic() - self.started_monotonic),
                "run": dict(self._run),
                "workflow": dict(self._workflow),
                "spec": dict(self._spec),
                "scheduler": dict(self._scheduler),
                "tasks": tasks,
                "dependencies": sum(int(item.get("dependency_count", 0)) for item in tasks),
                "agents": agents,
                "context": dict(self._context),
                "skills": [dict(value) for _, value in sorted(self._skills.items())],
                "convergence": dict(self._convergence),
                "waiting": dict(self._waiting),
                "retries": int(self._counters.get("retries", 0)),
                "failures": int(self._counters.get("failures", 0)),
                "artifact_count": int(self._counters.get("artifacts", 0)),
                "code_intelligence": dict(self._codeintel),
                "trace": list(self._trace)[-max(1, min(trace_limit, self.trace_limit)):],
            }


def context_budget_payload(snapshot: Any, *, duration_seconds: float, omitted_messages: int) -> dict[str, Any]:
    """Return only numeric budget telemetry suitable for events and TUI."""

    categories = getattr(snapshot, "categories", {})

    def tokens(name: str) -> int:
        item = categories.get(name)
        value = getattr(item, "effective_tokens", 0)
        return int(value) if isinstance(value, int) else 0

    capacity = int(getattr(snapshot, "max_input_tokens", 0))
    used = int(getattr(snapshot, "reconciled_input_tokens", 0))
    return {
        "status": "prepared",
        "duration_seconds": max(0.0, float(duration_seconds)),
        "capacity_tokens": capacity,
        "context_window_tokens": int(getattr(snapshot, "context_window_tokens", 0)),
        "used_tokens": used,
        "output_reserve_tokens": int(getattr(snapshot, "output_reserve_tokens", 0)),
        "remaining_tokens": int(getattr(snapshot, "remaining_input_tokens", 0)),
        "context_utilization": (used / capacity) if capacity else 0.0,
        "hot_context_tokens": tokens("hot_conversation"),
        "session_state_tokens": tokens("session_state"),
        "loaded_skills_tokens": tokens("skills"),
        "retrieved_context_tokens": tokens("retrieved_context"),
        "artifact_tokens": tokens("tool_results"),
        "omitted_messages": max(0, int(omitted_messages)),
    }


__all__ = ["OperationalState", "context_budget_payload"]
