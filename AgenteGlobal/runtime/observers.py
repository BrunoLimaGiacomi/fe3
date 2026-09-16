"""EventBus observers for safe, single-writer terminal output."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from typing import Any, TextIO

from .events import RuntimeEvent
from .security_text import redact_sensitive_text, truncate_single_line
from .terminal_task_board import LoadingIndicator, TerminalTaskBoard, TerminalWriter
from .terminal_text import StreamingTerminalMarkdownFilter


_LOGGABLE_FIELDS = frozenset(
    {
        "agent", "attempt", "blocker_count", "convergence_pass", "context_utilization",
        "duration_seconds", "finish_reason", "first_token_latency_seconds", "model",
        "gap_count", "hit_count", "plan_id", "plan_revision", "previous_status",
        "repair_attempt", "returncode", "status", "stage", "task_id", "task_count",
        "tool", "total_tokens", "workflow",
    }
)


class StructuredEventLogger:
    """Registra somente metadados operacionais, nunca prompts ou deltas."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def __call__(self, event: RuntimeEvent) -> None:
        metadata = {
            key: value
            for key, value in event.payload.items()
            if key in _LOGGABLE_FIELDS and isinstance(value, str | int | float | bool | type(None))
        }
        self._logger.info(
            "runtime_event name=%s sequence=%s source=%s metadata=%s",
            event.name,
            event.sequence,
            event.source,
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        )


class TerminalEventConsumer:
    """Render operational events without exposing reasoning or first-token noise.

    The consumer is synchronous by design, so it can be subscribed directly to
    ``runtime.events.EventBus``.  All writes, including task-board redraws, use
    the same ``TerminalWriter`` lock.
    """

    def __init__(
        self,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        enabled: bool = True,
        writer: TerminalWriter | None = None,
        task_board: TerminalTaskBoard | None = None,
        show_task_board: bool = False,
        max_tasks: int = 1,
        color: bool | None = None,
        unicode: bool | None = None,
    ) -> None:
        if writer is None and task_board is not None:
            writer = task_board.writer
        self.writer = writer or TerminalWriter(stdout=stdout or sys.stdout, stderr=stderr or sys.stderr, color=color)
        self.stdout = self.writer.stdout
        self.stderr = self.writer.stderr
        self.enabled = bool(enabled)
        self.task_board = task_board
        if self.task_board is None and show_task_board:
            self.task_board = TerminalTaskBoard(
                max_tasks,
                enabled=enabled,
                writer=self.writer,
                unicode=unicode,
            )
        self._response_open = False
        self._response_filter = StreamingTerminalMarkdownFilter()
        self._open_tool_streams: dict[str, TextIO] = {}
        self._loading_indicators: dict[str, LoadingIndicator] = {}
        self._subscription_id: int | None = None
        self._event_bus: Any | None = None

    def subscribe_to(self, event_bus: Any) -> int:
        """Subscribe this consumer and return the EventBus subscription ID."""

        if self._event_bus is not None:
            self.close()
        self._event_bus = event_bus
        self._subscription_id = event_bus.subscribe(self)
        return self._subscription_id

    def close(self) -> None:
        if self._event_bus is not None and self._subscription_id is not None:
            self._event_bus.unsubscribe(self._subscription_id)
        self._event_bus = None
        self._subscription_id = None
        self._stop_loading()
        self._close_response()
        self._close_tool_streams()
        if self.task_board is not None:
            self.task_board.finish()

    def __call__(self, event: RuntimeEvent) -> None:
        if not self.enabled:
            return
        # EventBus can dispatch events from concurrent tasks.  Keep all event
        # transitions and related writes together to prevent cursor corruption.
        with self.writer.lock:
            self._handle(event)

    def _handle(self, event: RuntimeEvent) -> None:
        payload = event.payload
        visible = bool(payload.get("visible", True))
        agent = _safe(payload.get("agent") or "AgenteGlobal")

        if event.name == "spec_workflow.stage_changed":
            labels = {
                "specify": "Specification", "clarify": "Clarification", "checklist": "Checklist",
                "plan": "Plan", "analyze": "Analyze", "human_approval": "Approval",
                "implement": "Implementation", "review": "Review", "converge": "Converge",
            }
            stage = _safe(payload.get("stage") or "stage")
            status = _safe(payload.get("status") or "running")
            suffix = "; intervention required" if status == "blocked" else ""
            self._line(f"Spec> {labels.get(stage, stage)}: {status}{suffix}", style="yellow" if suffix else "cyan")
            return
        if event.name.startswith("convergence."):
            status = _safe(payload.get("status") or event.name.partition(".")[2])
            details = []
            if isinstance(payload.get("convergence_pass"), int):
                details.append(f"pass {payload['convergence_pass']}")
            if isinstance(payload.get("gap_count"), int):
                details.append(f"gaps {payload['gap_count']}")
            detail_suffix = f" ({', '.join(details)})" if details else ""
            self._line(f"Convergence> {status}{detail_suffix}", style="yellow" if "gap" in status else "green")
            return
        if event.name == "retrieval.completed":
            hits = payload.get("hit_count")
            self._line(f"Context> Retrieved {hits if isinstance(hits, int) else 0} relevant chunks.", style="cyan")
            return
        if event.name == "skill.loaded":
            self._line(f"Skills> Loaded {_safe(payload.get('skill_name') or 'skill')}.", style="cyan")
            return
        if event.name.startswith("browser.action."):
            status = _safe(payload.get("status") or event.name.rpartition(".")[2])
            self._line(f"Browser> {status}; web content remains untrusted.", style="yellow")
            return
        if event.name.startswith("mcp.call."):
            status = _safe(payload.get("status") or event.name.rpartition(".")[2])
            provider = _safe(payload.get("provider") or "provider")
            self._line(f"MCP {provider}> {status}", style="cyan")
            return

        if event.name == "workflow.state_changed":
            workflow = _safe(payload.get("workflow") or "workflow").capitalize()
            status = _safe(payload.get("status") or "unknown")
            plan_id = _safe(payload.get("plan_id") or "")
            plan_suffix = f" ({plan_id})" if plan_id else ""
            self._line(f"{workflow}> {status}{plan_suffix}", style="yellow" if "approval" in status else "cyan")
            return
        if event.name.startswith("scheduler."):
            status = _safe(payload.get("status") or event.name.partition(".")[2])
            self._line(f"Scheduler> {status}", style="red" if "fail" in status else "cyan")
            return
        if event.name.startswith("task."):
            if event.name in {"task.created", "task.ready"}:
                return
            task_id = _safe(payload.get("task_id") or "task")
            status = _safe(payload.get("status") or event.name.partition(".")[2])
            reason = _safe(payload.get("reason") or "")
            reason_suffix = f" ({reason})" if reason else ""
            self._line(f"Task {task_id}> {status}{reason_suffix}", style="red" if "fail" in status else "cyan")
            return
        if event.name.startswith("review."):
            status = _safe(payload.get("status") or event.name.partition(".")[2])
            self._line(f"Review> {status}", style="red" if "fail" in status else "cyan")
            return
        if event.name.startswith("repair."):
            status = _safe(payload.get("status") or event.name.partition(".")[2])
            attempt = payload.get("repair_attempt")
            suffix = f" #{attempt}" if isinstance(attempt, int) else ""
            self._line(f"Repair{suffix}> {status}", style="yellow")
            return

        if event.name == "agent.started":
            if agent != "AgenteGlobal":
                self._line(f"{agent}> Started.", style="cyan")
            return
        if event.name == "agent.waiting_model":
            self._line(f"{agent}> Waiting for {_safe(payload.get('model') or 'model')}...", style="cyan")
            return
        if event.name == "llm.request_started":
            self._close_response()
            self._response_filter = StreamingTerminalMarkdownFilter()
            request_key = f"{agent}:{payload.get('attempt', 1)}"
            self._stop_loading(request_key)
            if self.task_board is not None:
                self.task_board.clear_live()
            indicator = LoadingIndicator(writer=self.writer, enabled=True)
            indicator.__enter__()
            self._loading_indicators[request_key] = indicator
            return
        # first_token remains an operational metric for StructuredEventLogger,
        # but must never create a visible status line.
        if event.name == "llm.first_token":
            self._stop_loading()
            return
        if event.name in {"llm.tool_call_started", "llm.tool_call_completed"}:
            self._stop_loading()
            return
        if event.name == "llm.text_delta" and visible:
            self._stop_loading()
            text = payload.get("text")
            if isinstance(text, str) and text:
                if self.task_board is not None:
                    self.task_board.clear_live()
                if not self._response_open:
                    self.writer.write(self.writer.style("Assistente> ", "cyan"))
                    self._response_open = True
                rendered = self._response_filter.feed(redact_sensitive_text(text))
                if rendered:
                    self.writer.write(rendered, sanitize=True)
            return
        if event.name == "llm.request_completed":
            self._stop_loading()
            self._close_response()
            if visible and payload.get("status") == "failed":
                self._line("Model> Request failed.", stream=self.stderr, style="red")
            return
        if event.name == "tool.started":
            self._stop_loading()
            self._close_response()
            if self.task_board is not None:
                self.task_board.start_event(
                    _safe(payload.get("tool_id") or payload.get("tool") or "tool"),
                    _safe(payload.get("tool") or "tool"),
                    activity=_safe(payload.get("activity") or payload.get("tool") or "tool"),
                )
            else:
                self._line(f"Tool> Running: {_safe(payload.get('activity') or payload.get('tool') or 'tool')}", style="cyan")
            return
        if event.name in {"tool.stdout", "tool.stderr"}:
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                return
            if self.task_board is not None:
                self.task_board.clear_live()
            stream = self.stderr if event.name == "tool.stderr" else self.stdout
            tool_id = _safe(payload.get("tool_id") or payload.get("tool") or "tool")
            stream_key = f"{event.name}:{tool_id}"
            if stream_key not in self._open_tool_streams:
                self._line(
                    f"Tool> Receiving {'stderr' if event.name == 'tool.stderr' else 'output'}...",
                    stream=stream,
                    style="yellow" if event.name == "tool.stderr" else "cyan",
                )
                self._open_tool_streams[stream_key] = stream
            self.writer.write(redact_sensitive_text(text), stream=stream, sanitize=True)
            return
        if event.name in {"tool.completed", "tool.failed"}:
            self._close_tool_streams()
            tool = _safe(payload.get("tool") or "tool")
            code = payload.get("returncode")
            failed = event.name == "tool.failed" or payload.get("status") in {"failed", "error", "cancelled"} or (isinstance(code, int) and code != 0)
            if self.task_board is not None:
                if failed:
                    self.task_board.clear_live()
                    self._line(self._tool_error_line(tool, code, payload), stream=self.stderr, style="red")
                self.task_board.complete_event(_safe(payload.get("tool_id") or tool), tool, dict(payload))
                return
            # A clean process completion is represented by the task/status
            # flow; avoid repeating the universally unhelpful ``exit 0``.
            if not failed:
                if isinstance(code, int) and code == 0:
                    return
                self._line(f"Tool> Completed: {tool}", style="green")
                return
            self._line(self._tool_error_line(tool, code, payload), stream=self.stderr, style="red")
            return
        if event.name == "run.completed" and self.task_board is not None:
            self.task_board.finish(failed=payload.get("status") in {"failed", "error", "cancelled"})
            return
        if event.name == "agent.completed" and agent != "AgenteGlobal":
            self._stop_loading()
            duration = _format_seconds(payload.get("duration_seconds"))
            duration_suffix = f" ({duration})" if duration else ""
            self._line(f"{agent}> Completed{duration_suffix}.", style="green")
            return
        if event.name == "exploration.started":
            self._stop_loading("exploration")
            if self.task_board is not None:
                self.task_board.clear_live()
            indicator = LoadingIndicator(message="Explorando", writer=self.writer, enabled=True)
            indicator.__enter__()
            self._loading_indicators["exploration"] = indicator
            return
        if event.name == "exploration.completed":
            self._stop_loading("exploration")
            details = []
            for name, label in (("files_read", "files"), ("relationships_found", "relations")):
                if isinstance(payload.get(name), int):
                    details.append(f"{label}={payload[name]}")
            detail_suffix = f" ({', '.join(details)})" if details else ""
            self._line(f"Explore> Completed{detail_suffix}.", style="green")

    def _tool_error_line(self, tool: str, code: object, payload: Any) -> str:
        detail = payload.get("message") or payload.get("error") if isinstance(payload, Mapping) else ""
        suffix = f": {_safe(detail)}" if detail else ""
        if isinstance(code, int) and code != 0:
            suffix += f" (exit {code})"
        return f"Tool> Failed: {tool}{suffix}"

    def _close_response(self) -> None:
        if self._response_open:
            pending = self._response_filter.flush()
            if pending:
                self.writer.write(pending, sanitize=True)
            self.writer.line("")
            self._response_open = False

    def _stop_loading(self, request_key: str | None = None) -> None:
        keys = [request_key] if request_key is not None else list(self._loading_indicators)
        for key in keys:
            indicator = self._loading_indicators.pop(key, None)
            if indicator is not None:
                indicator.__exit__(None, None, None)

    def _close_tool_streams(self) -> None:
        if not self._open_tool_streams:
            return
        for stream in tuple(dict.fromkeys(self._open_tool_streams.values())):
            self.writer.line("", stream=stream)
        self._open_tool_streams.clear()

    def _line(self, text: str, *, stream: TextIO | None = None, style: str | None = None) -> None:
        if self.task_board is not None:
            self.task_board.clear_live()
        self.writer.line(text, stream=stream, style=style)


def _safe(value: object, limit: int = 180) -> str:
    return truncate_single_line(redact_sensitive_text(str(value)), limit=limit)


def _format_seconds(value: object) -> str:
    if not isinstance(value, int | float):
        return ""
    return f"{max(0.0, float(value)):.2f}s"


__all__ = ["StructuredEventLogger", "TerminalEventConsumer"]
