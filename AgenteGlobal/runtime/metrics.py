"""Secret-free local operational metrics derived from RuntimeEvent metadata."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .events import RuntimeEvent


METRICS_SCHEMA_VERSION = "1.0"


@dataclass(slots=True)
class LatencySeries:
    count: int = 0
    total_seconds: float = 0.0
    min_seconds: float | None = None
    max_seconds: float | None = None

    def observe(self, value: Any) -> None:
        if not isinstance(value, int | float) or isinstance(value, bool):
            return
        selected = max(0.0, float(value))
        self.count += 1
        self.total_seconds += selected
        self.min_seconds = selected if self.min_seconds is None else min(self.min_seconds, selected)
        self.max_seconds = selected if self.max_seconds is None else max(self.max_seconds, selected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "total_seconds": self.total_seconds,
            "average_seconds": self.total_seconds / self.count if self.count else None,
            "min_seconds": self.min_seconds,
            "max_seconds": self.max_seconds,
        }


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    schema_version: str
    generated_at: str
    counters: Mapping[str, int]
    gauges: Mapping[str, float]
    latencies: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "counters", MappingProxyType(dict(self.counters)))
        object.__setattr__(self, "gauges", MappingProxyType(dict(self.gauges)))
        object.__setattr__(
            self,
            "latencies",
            MappingProxyType({name: MappingProxyType(dict(value)) for name, value in self.latencies.items()}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "counters": dict(self.counters),
            "gauges": dict(self.gauges),
            "latencies": {name: dict(value) for name, value in self.latencies.items()},
        }


class LocalMetricsCollector:
    """EventBus consumer retaining only numeric aggregates and stable labels."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.gauges: dict[str, float] = {
            "context_utilization_latest": 0.0,
            "context_utilization_max": 0.0,
            "parallelism_current": 0.0,
            "parallelism_peak": 0.0,
        }
        self.latencies: dict[str, LatencySeries] = {
            name: LatencySeries()
            for name in (
                "ttft",
                "model",
                "tool",
                "agent",
                "review",
                "runtime_overhead",
                "context_build",
                "index_build",
                "lsp",
                "exploration",
            )
        }
        self._review_started: dict[str, float] = {}

    def _increment(self, name: str, value: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + int(value)

    @staticmethod
    def _number(payload: Mapping[str, Any], name: str) -> float | None:
        value = payload.get(name)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        return None

    def __call__(self, event: RuntimeEvent) -> None:
        payload = event.payload
        name = event.name
        if name == "llm.request_started":
            self._increment("model_requests")
            if isinstance(payload.get("attempt"), int) and int(payload["attempt"]) > 1:
                self._increment("retries")
        elif name == "llm.first_token":
            self.latencies["ttft"].observe(payload.get("first_token_latency_seconds"))
        elif name == "llm.request_completed":
            self.latencies["model"].observe(payload.get("duration_seconds"))
            if payload.get("status") != "completed":
                self._increment("model_failures")
            for source, target in (
                ("prompt_tokens", "prompt_tokens"),
                ("completion_tokens", "completion_tokens"),
                ("total_tokens", "total_tokens"),
            ):
                value = payload.get(source)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self._increment(target, value)
            utilization = self._number(payload, "context_utilization")
            if utilization is not None:
                self.gauges["context_utilization_latest"] = utilization
                self.gauges["context_utilization_max"] = max(
                    self.gauges["context_utilization_max"], utilization
                )
        elif name == "tool.started":
            self._increment("tool_calls")
        elif name in {"tool.completed", "tool.failed"}:
            self.latencies["tool"].observe(payload.get("duration_seconds"))
            if name == "tool.failed":
                self._increment("tool_failures")
        elif name == "agent.started":
            self._increment("agent_runs")
        elif name == "agent.completed":
            self.latencies["agent"].observe(payload.get("duration_seconds"))
            if payload.get("status") not in {"completed", "approved"}:
                self._increment("agent_failures")
        elif name == "review.started":
            self._increment("reviews")
            self._review_started[self._review_key(payload)] = event.monotonic_timestamp
        elif name in {"review.passed", "review.failed"}:
            started = self._review_started.pop(self._review_key(payload), None)
            if started is not None:
                self.latencies["review"].observe(event.monotonic_timestamp - started)
            if name == "review.failed":
                self._increment("review_failures")
        elif name == "context.compacted":
            self._increment("context_compactions")
        elif name == "context.prepared":
            duration = payload.get("duration_seconds")
            self.latencies["context_build"].observe(duration)
            self.latencies["runtime_overhead"].observe(duration)
            utilization = self._number(payload, "context_utilization")
            if utilization is not None:
                self.gauges["context_utilization_latest"] = utilization
                self.gauges["context_utilization_max"] = max(
                    self.gauges["context_utilization_max"], utilization
                )
        elif name == "retrieval.completed":
            hits = payload.get("hit_count")
            if isinstance(hits, int) and hits > 0:
                self._increment("retrieval_hits", hits)
            else:
                self._increment("retrieval_misses")
        elif name == "skill.loaded":
            self._increment("skills_loaded")
        elif name == "artifact.created":
            self._increment("artifacts")
        elif name == "task.running":
            self._increment("tasks_started")
            self.gauges["parallelism_current"] += 1
            self.gauges["parallelism_peak"] = max(
                self.gauges["parallelism_peak"], self.gauges["parallelism_current"]
            )
        elif name.startswith("task.") and name.partition(".")[2] in {
            "completed",
            "blocked",
            "failed_retryable",
            "failed_final",
            "cancelled",
        }:
            if name == "task.completed":
                self._increment("tasks_completed")
            elif name != "task.failed_retryable":
                self._increment("tasks_failed")
            self.gauges["parallelism_current"] = max(0.0, self.gauges["parallelism_current"] - 1)
        elif name.startswith("convergence.") and name in {
            "convergence.passed",
            "convergence.gaps_found",
        }:
            self._increment("convergence_passes")
        elif name == "browser.action.started":
            self._increment("browser_actions")
        elif name == "browser.action.failed":
            self._increment("browser_failures")
        elif name == "mcp.call.started":
            self._increment("mcp_calls")
        elif name == "mcp.call.failed":
            self._increment("mcp_failures")
        elif name == "code_index.updated":
            self._increment("code_index_updates")
            self._increment("code_files_indexed", int(payload.get("indexed_files", 0) or 0))
            self._increment("code_index_cache_hits", int(payload.get("cache_hits", 0) or 0))
            self._increment("code_index_cache_misses", int(payload.get("cache_misses", 0) or 0))
            self.latencies["index_build"].observe(payload.get("duration_seconds"))
        elif name == "lsp.request.started":
            self._increment("lsp_requests")
        elif name == "lsp.request.completed":
            self.latencies["lsp"].observe(payload.get("duration_seconds"))
        elif name == "exploration.completed":
            self._increment("explorations")
            if payload.get("cache_hit"):
                self._increment("exploration_cache_hits")
            else:
                self._increment("exploration_cache_misses")
            self._increment("exploration_files_read", int(payload.get("files_read", 0) or 0))
            self._increment("exploration_symbols_inspected", int(payload.get("symbols_inspected", 0) or 0))
            self._increment("exploration_relationships", int(payload.get("relationships_found", 0) or 0))
            self.latencies["exploration"].observe(payload.get("duration_seconds"))

    @staticmethod
    def _review_key(payload: Mapping[str, Any]) -> str:
        return f"{payload.get('plan_id', '')}:{payload.get('repair_attempt', 0)}"

    def snapshot(self) -> MetricsSnapshot:
        return MetricsSnapshot(
            METRICS_SCHEMA_VERSION,
            datetime.now(timezone.utc).isoformat(),
            self.counters,
            self.gauges,
            {name: series.to_dict() for name, series in self.latencies.items()},
        )


class LocalMetricsStore:
    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace de métricas precisa ser diretório")
        self.path = self.workspace / ".agenteglobal" / "metrics" / "runtime-metrics.json"

    def save(self, snapshot: MetricsSnapshot) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=".runtime-metrics.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return self.path


__all__ = [
    "LatencySeries",
    "LocalMetricsCollector",
    "LocalMetricsStore",
    "METRICS_SCHEMA_VERSION",
    "MetricsSnapshot",
]
