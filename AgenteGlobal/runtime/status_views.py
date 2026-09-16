"""Compact human-facing operational views for slash commands."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


SPEC_STAGES = (
    "Specification", "Clarification", "Checklist", "Plan", "Analyze",
    "Approval", "Implementation", "Review", "Converge",
)


def _value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def section(title: str, rows: Iterable[tuple[str, Any]]) -> str:
    selected = [(str(label), _value(value)) for label, value in rows]
    width = max((len(label) for label, _ in selected), default=0)
    lines = [f"{title}"]
    lines.extend(f"  {label:<{width}}  {value}" for label, value in selected)
    return "\n".join(lines)


def render_overview(snapshot: Mapping[str, Any]) -> str:
    workflow = snapshot.get("workflow") or {}
    spec = snapshot.get("spec") or {}
    waiting = snapshot.get("waiting") or {}
    codeintel = snapshot.get("code_intelligence") or {}
    return section(
        "Operational status",
        (
            ("Run", (snapshot.get("run") or {}).get("status", "idle")),
            ("Goal/Plan", workflow.get("status", "idle")),
            ("Plan ID", workflow.get("plan_id")),
            ("Spec", f"{spec.get('stage', '-')} / {spec.get('status', '-') }"),
            ("Scheduler", (snapshot.get("scheduler") or {}).get("status", "idle")),
            ("Tasks", len(snapshot.get("tasks") or ())),
            ("Dependencies", snapshot.get("dependencies", 0)),
            ("Subagents", sum(1 for item in (snapshot.get("agents") or ()) if item.get("agent") != "AgenteGlobal")),
            ("Artifacts (session)", snapshot.get("artifact_count", 0)),
            ("Code intelligence", codeintel.get("status", "idle")),
            ("Exploration", codeintel.get("depth", "-")),
            ("Retries", snapshot.get("retries", 0)),
            ("Convergence", (snapshot.get("convergence") or {}).get("status", "not_started")),
            ("Elapsed", f"{float(snapshot.get('elapsed_seconds', 0)):.1f}s"),
            ("Waiting", waiting.get("reason", "no")),
            ("Intervention", waiting.get("intervention_required", False)),
        ),
    )


def render_spec(snapshot: Mapping[str, Any]) -> str:
    spec = snapshot.get("spec") or {}
    active = str(spec.get("stage") or "").lower().replace("human_", "")
    aliases = {"specify": 0, "clarify": 1, "checklist": 2, "plan": 3,
               "analyze": 4, "approval": 5, "implement": 6, "review": 7, "converge": 8}
    index = aliases.get(active, -1)
    rendered = []
    for position, label in enumerate(SPEC_STAGES):
        marker = "[>]" if position == index else "[x]" if index >= 0 and position < index else "[ ]"
        rendered.append(f"{marker} {label}")
    return "Spec Kit workflow\n  " + " -> ".join(rendered)


def render_tasks(snapshot: Mapping[str, Any]) -> str:
    tasks = snapshot.get("tasks") or ()
    if not tasks:
        return "Tasks\n  No tasks observed in this session."
    lines = ["Tasks"]
    for item in tasks:
        retry = f", attempt {item.get('attempt')}" if item.get("attempt") else ""
        deps = f", deps {item.get('dependency_count')}" if item.get("dependency_count") else ""
        lines.append(f"  {item.get('task_id', 'task')}: {item.get('status', 'unknown')}{retry}{deps}")
    return "\n".join(lines)


def render_agents(snapshot: Mapping[str, Any]) -> str:
    agents = snapshot.get("agents") or ()
    if not agents:
        return "Agents\n  No agent activity observed in this session."
    return "\n".join(
        ["Agents"]
        + [f"  {item.get('agent', 'agent')}: {item.get('status', 'unknown')} | model={item.get('model', '-')}" for item in agents]
    )


def render_context(snapshot: Mapping[str, Any], engine_status: Mapping[str, Any]) -> str:
    budget = snapshot.get("context") or {}
    capacity = int(budget.get("capacity_tokens") or 0)
    used = int(budget.get("used_tokens") or 0)
    utilization = f"{(100 * used / capacity):.1f}%" if capacity else "not measured yet"
    return section(
        "Context and token budget (last real request)",
        (
            ("Context window", budget.get("context_window_tokens", 0) or "not measured yet"),
            ("Capacity", capacity or "not measured yet"),
            ("Used", used or "not measured yet"),
            ("Output reserve", budget.get("output_reserve_tokens", 0)),
            ("Remaining", budget.get("remaining_tokens", 0)),
            ("Utilization", utilization),
            ("Hot context", budget.get("hot_context_tokens", 0)),
            ("SessionState", budget.get("session_state_tokens", 0)),
            ("Loaded skills", budget.get("loaded_skills_tokens", 0)),
            ("Retrieved chunks", budget.get("retrieved_context_tokens", 0)),
            ("Artifacts/tool results", budget.get("artifact_tokens", 0)),
            ("Indexed chunks", engine_status.get("indexed_chunks", engine_status.get("chunk_count", 0))),
            ("Retrieval", engine_status.get("semantic_backend", "local")),
        ),
    )


def render_skills(metadata: Sequence[Any], loaded: Sequence[Mapping[str, Any]]) -> str:
    if not metadata:
        return "Skills\n  No skills discovered."
    lines = ["Skills"]
    for item in metadata:
        row = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        lines.append(f"  {row.get('name', 'skill')}: source={row.get('origin', 'unknown')}")
    return "\n".join(lines)


def render_trace(snapshot: Mapping[str, Any]) -> str:
    trace = snapshot.get("trace") or ()
    if not trace:
        return "Trace\n  No operational events observed."
    return "\n".join(
        ["Trace (metadata only)"]
        + [f"  #{item.get('sequence')} {item.get('event')} [{item.get('status') or '-'}]" for item in trace]
    )


def render_usage(snapshot: Mapping[str, Any], metrics: Mapping[str, Any]) -> str:
    counters = metrics.get("counters") or {}
    gauges = metrics.get("gauges") or {}
    return section(
        "Session usage",
        (
            ("Model requests", counters.get("model_requests", 0)),
            ("Tokens total", counters.get("total_tokens", 0)),
            ("Tool calls", counters.get("tool_calls", 0)),
            ("Agent runs", counters.get("agent_runs", 0)),
            ("Reviews", counters.get("reviews", 0)),
            ("Retries", counters.get("retries", snapshot.get("retries", 0))),
            ("Context compactions", counters.get("context_compactions", 0)),
            ("Retrieval hits/misses", f"{counters.get('retrieval_hits', 0)}/{counters.get('retrieval_misses', 0)}"),
            ("Browser actions", counters.get("browser_actions", 0)),
            ("MCP calls", counters.get("mcp_calls", 0)),
            ("Parallelism peak", gauges.get("parallelism_peak", 0)),
            ("Convergence passes", counters.get("convergence_passes", 0)),
            ("Elapsed", f"{float(snapshot.get('elapsed_seconds', 0)):.1f}s"),
        ),
    )


def render_artifacts(artifacts: Sequence[Any]) -> str:
    if not artifacts:
        return "Artifacts\n  No artifacts in this session."
    lines = ["Artifacts"]
    for artifact in artifacts:
        row = artifact.model_dump(mode="json") if hasattr(artifact, "model_dump") else dict(artifact)
        lines.append(f"  {row.get('artifact_id', row.get('id', '-'))}: {row.get('media_type', '-')} | {row.get('size_bytes', row.get('size', '-'))} bytes")
    return "\n".join(lines)


def estimate_skill_tokens(content: str) -> int:
    return max(1, math.ceil(len(content.encode("utf-8")) / 4))


__all__ = [
    "SPEC_STAGES", "estimate_skill_tokens", "render_agents", "render_artifacts",
    "render_context", "render_overview", "render_skills", "render_spec",
    "render_tasks", "render_trace", "render_usage", "section",
]
