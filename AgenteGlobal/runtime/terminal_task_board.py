"""Thread-safe terminal output primitives and the task progress board.

The runtime can emit events from more than one asyncio task.  This module owns
the small amount of terminal state needed to make those events readable: one
writer lock, optional ANSI colour, and one live region for the task board.
Nothing in here knows about the model or about tool permissions.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, TextIO

from .security_text import redact_sensitive_text, truncate_single_line
from .terminal_tool_views import describe_tool_activity, summarize_tool_result


# Keep these as real escape characters.  A previous implementation used the
# two-character sequence ``\\033`` and consequently printed the escape text.
ANSI_BY_STYLE = {
    "green": "\033[92m",
    "yellow": "\033[93m",
    "cyan": "\033[96m",
    "red": "\033[91m",
    "gray": "\033[90m",
    "white": "\033[97m",
}
# Private compatibility name retained for callers that imported the old map.
_ANSI = ANSI_BY_STYLE
RESET = "\033[0m"


def _is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _strip_terminal_controls(text: str) -> str:
    """Remove terminal control sequences from untrusted displayed text.

    Newlines are intentionally retained for command output.  Carriage returns
    are normalized so a process cannot rewrite the board or a previous line.
    The writer's own ANSI sequences are added after this function returns.
    """

    value = str(text).replace("\r\n", "\n").replace("\r", "\n")
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\033":
            # CSI and OSC sequences are the common terminal control forms.
            if index + 1 < len(value) and value[index + 1] == "[":
                index += 2
                while index < len(value) and not ("@" <= value[index] <= "~"):
                    index += 1
                index += 1
                continue
            if index + 1 < len(value) and value[index + 1] == "]":
                index += 2
                while index < len(value):
                    if value[index] == "\a":
                        index += 1
                        break
                    if value[index] == "\033" and index + 1 < len(value) and value[index + 1] == "\\":
                        index += 2
                        break
                    index += 1
                continue
            index += 1
            continue
        if ord(char) < 32 and char not in "\n\t":
            index += 1
            continue
        result.append(char)
        index += 1
    return "".join(result)


class TerminalWriter:
    """The single serialized writer used by terminal observers and boards."""

    def __init__(
        self,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        color: bool | None = None,
        cursor: bool | None = None,
    ) -> None:
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self._color_override = color
        self._cursor_override = cursor
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """Expose the lock for a coordinator that must group writes."""

        return self._lock

    def color_enabled(self, stream: TextIO | None = None) -> bool:
        if os.getenv("NO_COLOR"):
            return False
        if self._color_override is not None:
            return self._color_override
        return _is_tty(stream or self.stdout)

    def cursor_enabled(self) -> bool:
        if self._cursor_override is not None:
            return self._cursor_override
        return _is_tty(self.stdout)

    def interactive_enabled(self) -> bool:
        """Whether transient text is suitable, independent of cursor redraw."""

        return _is_tty(self.stdout)

    def style(self, text: str, style: str = "white", *, stream: TextIO | None = None) -> str:
        value = str(text)
        if not self.color_enabled(stream):
            return value
        prefix = ANSI_BY_STYLE.get(style)
        return f"{prefix or ''}{value}{RESET if prefix else ''}"

    def write(
        self,
        text: str,
        *,
        stream: TextIO | None = None,
        flush: bool = True,
        sanitize: bool = False,
    ) -> None:
        target = stream or self.stdout
        value = _strip_terminal_controls(text) if sanitize else str(text)
        with self._lock:
            target.write(value)
            if flush:
                target.flush()

    def line(
        self,
        text: str = "",
        *,
        stream: TextIO | None = None,
        style: str | None = None,
        sanitize: bool = True,
    ) -> None:
        target = stream or self.stdout
        value = _strip_terminal_controls(text) if sanitize else str(text)
        if style:
            value = self.style(value, style, stream=target)
        self.write(value + "\n", stream=target, sanitize=False)

    def clear_lines(self, line_count: int) -> None:
        """Clear a previously rendered live region when cursor control is safe."""

        if line_count <= 0 or not self.cursor_enabled():
            return
        with self._lock:
            self.stdout.write(f"\033[{line_count}A")
            for _ in range(line_count):
                self.stdout.write("\033[2K\r\033[1B")
            self.stdout.write(f"\033[{line_count}A")
            self.stdout.flush()

    def replace_lines(self, lines: list[str], *, previous_lines: int = 0) -> int:
        """Replace a live board atomically and return its line count."""

        with self._lock:
            if previous_lines > 0 and self.cursor_enabled():
                self.stdout.write(f"\033[{previous_lines}A")
                for _ in range(previous_lines):
                    self.stdout.write("\033[2K\r\033[1B")
                self.stdout.write(f"\033[{previous_lines}A")
            self.stdout.write("\n".join(lines) + "\n")
            self.stdout.flush()
        return len(lines)


def _style(text: str, style: str, *, writer: TerminalWriter | None = None, stream: TextIO | None = None) -> str:
    """Backward-compatible styling helper for local callers and tests."""

    active = writer or TerminalWriter(stdout=stream or sys.stdout)
    return active.style(text, style, stream=stream)


class LoadingIndicator:
    """Small yellow, serialized ``Processando...`` indicator."""

    def __init__(
        self,
        message: str = "Processando",
        enabled: bool = True,
        *,
        stream: TextIO | None = None,
        writer: TerminalWriter | None = None,
    ) -> None:
        self.writer = writer or TerminalWriter(stdout=stream or sys.stdout)
        normalized = _strip_terminal_controls(message).rstrip(".")
        self.message = f"{normalized}..."
        self.enabled = bool(enabled) and self.writer.interactive_enabled()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "LoadingIndicator":
        if not self.enabled:
            return self
        self.writer.write(self.writer.style(self.message, "yellow"), flush=True)
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self.writer.line("")

    def _animate(self) -> None:
        while not self._stop.wait(0.5):
            self.writer.write(self.writer.style(".", "yellow"), flush=True)


@dataclass
class TerminalTask:
    task_id: str
    title: str
    deps: tuple[str, ...]
    status: str = "pending"
    started_at: float | None = None
    finished_at: float | None = None
    elapsed_seconds: int = 0
    result_style: str = "white"
    external_id: str | None = None


class TerminalTaskBoard:
    """A bounded task panel with safe live re-rendering and event adapters."""

    BAR_WIDTH = 28
    RULE_WIDTH = 72
    MAX_VISIBLE_TASKS = 32
    PENDING_TITLE = "aguardando proxima acao do modelo"
    ACTIVE_FOOTER = "Executing... (Ctrl+C to exit)"
    COMPLETE_FOOTER = "Complete."
    WARNING_FOOTER = "Complete with recovered failures."
    FAILED_FOOTER = "Execution stopped."
    APPROVAL_FOOTER = "Aguardando aprovação do operador..."

    def __init__(
        self,
        max_slots: int,
        enabled: bool = True,
        *,
        writer: TerminalWriter | None = None,
        stdout: TextIO | None = None,
        unicode: bool | None = None,
        color: bool | None = None,
        refresh_interval: float = 1.0,
        max_visible_tasks: int = MAX_VISIBLE_TASKS,
    ) -> None:
        self.max_slots = max(1, int(max_slots))
        self._initial_max_slots = self.max_slots
        self.enabled = bool(enabled)
        self.writer = writer or TerminalWriter(stdout=stdout or sys.stdout, color=color)
        self.unicode = True if unicode is None else bool(unicode)
        self.refresh_interval = max(0.1, float(refresh_interval))
        if max_visible_tasks < 8 or max_visible_tasks > self.MAX_VISIBLE_TASKS:
            raise ValueError(f"max_visible_tasks precisa estar entre 8 e {self.MAX_VISIBLE_TASKS}")
        self.max_visible_tasks = int(max_visible_tasks)
        self.tasks: list[TerminalTask] = []
        self._rendered_lines = 0
        self._last_batch_task_ids: list[str] = []
        self._event_tasks: dict[str, TerminalTask] = {}
        self._final = False
        self._lock = threading.RLock()
        self._stop_refresh = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self._live_refresh_disabled = False

    @property
    def live_lines(self) -> int:
        with self._lock:
            return self._rendered_lines

    def add_batch(self, tool_entries: list[tuple[str, dict[str, Any], int, int]]) -> list[TerminalTask]:
        with self._lock:
            deps = tuple(self._last_batch_task_ids)
            batch: list[TerminalTask] = []
            for tool_name, arguments, step, max_steps in tool_entries:
                task = TerminalTask(
                    task_id=f"TASK-{len(self.tasks) + 1:03d}",
                    title=self._tool_title(tool_name, arguments, step, max_steps),
                    deps=deps,
                )
                self.tasks.append(task)
                batch.append(task)
            self._last_batch_task_ids = [task.task_id for task in batch]
            return batch

    def start_batch(self, batch: list[TerminalTask]) -> None:
        with self._lock:
            now = time.monotonic()
            for task in batch:
                task.status = "running"
                task.started_at = task.started_at or now
        self.render()
        self._start_refresh()

    def start_event(
        self,
        external_id: str,
        tool_name: str,
        *,
        activity: str = "",
        arguments: dict[str, Any] | None = None,
    ) -> TerminalTask:
        """Create/start a task from a ``tool.started`` EventBus event."""

        key = str(external_id or f"{tool_name}-{time.monotonic_ns()}")
        with self._lock:
            task = self._event_tasks.get(key)
            if task is None:
                title = activity or self._tool_title(tool_name, arguments or {}, 1, self.max_slots)
                task = TerminalTask(
                    task_id=f"TASK-{len(self.tasks) + 1:03d}",
                    title=truncate_single_line(redact_sensitive_text(title), limit=140),
                    deps=tuple(self._last_batch_task_ids),
                    external_id=key,
                )
                self.tasks.append(task)
                self._event_tasks[key] = task
                self._last_batch_task_ids = [task.task_id]
        self.start_batch([task])
        return task

    def _event_task(self, external_id: str, tool_name: str, *, activity: str = "") -> TerminalTask:
        key = str(external_id or f"{tool_name}-{time.monotonic_ns()}")
        with self._lock:
            task = self._event_tasks.get(key)
        if task is not None:
            return task
        return self.start_event(key, tool_name, activity=activity)

    def complete_event(self, external_id: str, tool_name: str, payload: dict[str, Any]) -> TerminalTask:
        """Complete an event-created task, retaining non-zero/error details."""

        task = self._event_task(external_id, tool_name)
        status = str(payload.get("status") or "completed")
        summary = str(payload.get("error") or payload.get("message") or "")
        result: dict[str, Any] = {
            "returncode": payload.get("returncode"),
            "status": status,
        }
        if status in {"failed", "error", "cancelled"}:
            result["error"] = payload.get("error") or status
            result["message"] = summary or status
        self.complete_task(task, tool_name, json.dumps(result, ensure_ascii=False))
        if status in {"failed", "error", "cancelled"} and summary:
            with self._lock:
                if summary not in task.title:
                    task.title = truncate_single_line(f"{task.title} -> {redact_sensitive_text(summary)}", limit=140)
            self.render()
        return task

    def expand_slots(self, max_slots: int) -> None:
        with self._lock:
            self.max_slots = max(self.max_slots, int(max_slots))
        self.render()

    def complete_task(self, task: TerminalTask, tool_name: str, result: str) -> None:
        with self._lock:
            task.finished_at = time.monotonic()
            task.elapsed_seconds = self._elapsed(task)
            style, summary = summarize_tool_result(tool_name, result)
            task.result_style = style
            task.status = "failed" if style == "red" else "warning" if style == "yellow" else "completed"
            if style in {"red", "yellow"}:
                task.title = truncate_single_line(
                    redact_sensitive_text(f"{task.title} -> {summary}"), limit=140
                )
            active = any(item.status == "running" for item in self.tasks)
        self.render()
        if not active:
            self._stop_refresh_loop()

    def fail_task(self, task: TerminalTask, summary: str) -> None:
        with self._lock:
            task.finished_at = time.monotonic()
            task.elapsed_seconds = self._elapsed(task)
            task.status = "failed"
            task.result_style = "red"
            task.title = truncate_single_line(
                redact_sensitive_text(f"{task.title} -> {summary}"), limit=140
            )
            active = any(item.status == "running" for item in self.tasks)
        self.render()
        if not active:
            self._stop_refresh_loop()

    def finish(self, failed: bool = False) -> None:
        self._stop_refresh_loop()
        with self._lock:
            if not self.tasks:
                return
            self._final = True
            self.max_slots = max(1, len(self.tasks))
            has_task_failures = any(task.status in {"failed", "warning"} for task in self.tasks)
        footer = (
            self.FAILED_FOOTER
            if failed
            else self.WARNING_FOOTER
            if has_task_failures
            else self.COMPLETE_FOOTER
        )
        self.render(footer=footer)

    def pause_for_approval(self) -> None:
        """Stop refresh before a blocking prompt and leave a clean terminal line."""

        with self._lock:
            self._live_refresh_disabled = True
        self._stop_refresh_loop()
        self.render(footer=self.APPROVAL_FOOTER)
        with self._lock:
            self._rendered_lines = 0

    def prepare_for_input(self) -> None:
        """Freeze the current panel before prompt-toolkit owns the cursor.

        The rendered panel remains visible as ordinary scrollback, but future
        events can no longer move the cursor over the operator's prompt.
        """

        self._stop_refresh_loop()
        with self._lock:
            self._rendered_lines = 0
            self._live_refresh_disabled = True

    def begin_turn(self) -> None:
        """Start one request with an empty, independently rendered task panel."""

        self._stop_refresh_loop()
        with self._lock:
            self.tasks.clear()
            self._event_tasks.clear()
            self._last_batch_task_ids.clear()
            self._rendered_lines = 0
            self._final = False
            self._live_refresh_disabled = False
            self.max_slots = self._initial_max_slots

    def resume_after_approval(self) -> None:
        with self._lock:
            active = not self._final and any(task.status == "running" for task in self.tasks)
        if active:
            self._start_refresh()

    def clear_live(self) -> None:
        """Suspend the board before writing a stream or an operator prompt."""

        with self._lock:
            if self._rendered_lines:
                self.writer.clear_lines(self._rendered_lines)
                self._rendered_lines = 0

    def render(self, footer: str = ACTIVE_FOOTER) -> None:
        with self._lock:
            if not self.enabled or not self.tasks:
                return
            lines = self._build_lines(footer)
            self._rendered_lines = self.writer.replace_lines(lines, previous_lines=self._rendered_lines)

    def _start_refresh(self) -> None:
        if not self.enabled or self._live_refresh_disabled or not self.writer.cursor_enabled():
            return
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            return
        self._stop_refresh.clear()
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

    def _stop_refresh_loop(self) -> None:
        self._stop_refresh.set()
        thread = self._refresh_thread
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=0.2)

    def _refresh_loop(self) -> None:
        while not self._stop_refresh.wait(self.refresh_interval):
            with self._lock:
                if self._final or not any(task.status == "running" for task in self.tasks):
                    return
            self.render()

    def _build_lines(self, footer: str) -> list[str]:
        total = self._total_slots()
        completed = sum(1 for task in self.tasks if task.status == "completed")
        warning = sum(1 for task in self.tasks if task.status == "warning")
        failed = sum(1 for task in self.tasks if task.status == "failed")
        running = sum(1 for task in self.tasks if task.status == "running")
        done = completed + warning + failed
        pending = max(total - done - running, 0)
        percent = round((done / total) * 100) if total else 100
        progress_suffix = f" {percent}% ({done}/{total})"
        rows = [
            f"{self._progress_bar(completed, warning, failed, total, available_width=self._terminal_width() - len(progress_suffix))}{progress_suffix}",
            "",
            self._counter_line(completed, running, warning, failed, pending),
            "",
            self._rule_line(),
        ]
        rows.extend(self._task_line(task) for task in self._visible_tasks(total))
        footer_style = "red" if footer == self.FAILED_FOOTER else "yellow" if footer == self.WARNING_FOOTER else "green"
        rows.extend(["", self._rule_line(), "", self._styled(truncate_single_line(footer, limit=self._terminal_width()), footer_style)])
        return rows

    def _total_slots(self) -> int:
        with self._lock:
            return max(1, len(self.tasks)) if self._final else max(self.max_slots, len(self.tasks))

    def _visible_tasks(self, total: int) -> list[TerminalTask]:
        visible = list(self.tasks)
        for index in range(len(self.tasks) + 1, total + 1):
            visible.append(
                TerminalTask(
                    task_id=f"TASK-{index:03d}",
                    title=self.PENDING_TITLE,
                    deps=tuple(self._last_batch_task_ids[-1:]),
                )
            )
        if len(visible) <= self.max_visible_tasks:
            return visible
        hidden = len(visible) - (self.max_visible_tasks - 1)
        summary = TerminalTask(
            task_id="...",
            title=f"{hidden} etapas anteriores neste pedido",
            deps=(),
            status="completed",
        )
        return [summary, *visible[-(self.max_visible_tasks - 1):]]

    def _tool_title(self, tool_name: str, arguments: dict[str, Any], step: int, max_steps: int) -> str:
        return truncate_single_line(redact_sensitive_text(describe_tool_activity(tool_name, arguments, step, max_steps)), limit=140)

    def _counter_line(self, completed: int, running: int, warning: int, failed: int, pending: int) -> str:
        check, arrow, cross, circle = ("✓", "▶", "✕", "○") if self.unicode else ("+", ">", "!", "o")
        if self._terminal_width() < 72:
            # Compact mode keeps the whole status row on one physical line.
            compact = f"{check}{completed} {arrow}{running}"
            if warning:
                compact += f" !{warning}"
            if failed:
                compact += f" {cross}{failed}"
            compact += f" {circle}{pending}"
            return self._styled(compact[: self._terminal_width()], "white")
        parts = [
            self._styled(f"{check} {completed} completed", "green"),
            self._styled(f"{arrow} {running} running in parallel", "cyan"),
        ]
        if warning:
            parts.append(self._styled(f"! {warning} warning", "yellow"))
        if failed:
            parts.append(self._styled(f"{cross} {failed} failed", "red"))
        parts.append(self._styled(f"{circle} {pending} pending", "gray"))
        return "  ".join(parts)

    def _task_line(self, task: TerminalTask) -> str:
        status_style = {
            "completed": "green", "warning": "yellow", "running": "cyan", "failed": "red", "pending": "gray",
        }.get(task.status, "white")
        if self.unicode:
            mark = {"completed": "✓", "warning": "!", "running": "▶", "failed": "✕", "pending": "○"}.get(task.status, "○")
            dependency_arrow = "←"
        else:
            mark = {"completed": "+", "warning": "!", "running": ">", "failed": "!", "pending": "o"}.get(task.status, "o")
            dependency_arrow = "<-"
        deps = f" {dependency_arrow} {', '.join(task.deps)}" if task.deps else ""
        elapsed = self._elapsed(task)
        timer = f" [{self._format_elapsed(elapsed)}]" if task.status != "pending" else ""
        width = self._terminal_width()
        prefix = f"{mark} {task.task_id} "
        fixed = len(prefix) + len(deps) + len(timer)
        if fixed >= width:
            # Keep the row physical-line-safe on small terminals.  The task
            # identifier remains useful; dependency/timer details are clipped.
            suffix_limit = max(0, width - len(prefix) - 1)
            suffix = truncate_single_line(deps + timer, limit=suffix_limit)
            deps, timer = suffix, ""
            fixed = len(prefix) + len(deps)
        title = truncate_single_line(task.title, limit=max(4, width - fixed))
        detail_style = status_style if task.status in {"completed", "warning", "failed"} else "white"
        if task.status == "pending":
            detail_style = "gray"
        return (
            f"{self._styled(mark, status_style)} "
            f"{self._styled(task.task_id, detail_style)} "
            f"{self._styled(title, detail_style)}"
            f"{self._styled(deps, detail_style)}"
            f"{self._styled(timer, detail_style)}"
        )

    def _progress_bar(
        self,
        completed: int,
        warning: int,
        failed: int,
        total: int,
        *,
        available_width: int | None = None,
    ) -> str:
        total = max(1, total)
        bar_width = max(1, min(self.BAR_WIDTH, available_width if available_width is not None else self._terminal_width() - 12))
        green_slots = min(bar_width, max(0, round((completed / total) * bar_width)))
        yellow_slots = min(bar_width - green_slots, max(0, round((warning / total) * bar_width)))
        red_slots = min(bar_width - green_slots - yellow_slots, max(0, round((failed / total) * bar_width)))
        open_slots = bar_width - green_slots - yellow_slots - red_slots
        full, empty = ("█", "░") if self.unicode else ("#", ".")
        return (
            self._styled(full * green_slots, "green")
            + self._styled(full * yellow_slots, "yellow")
            + self._styled(full * red_slots, "red")
            + self._styled(empty * open_slots, "gray")
        )

    def _rule_line(self) -> str:
        width = self._terminal_width()
        width = min(self.RULE_WIDTH, width)
        return self._styled(("─" if self.unicode else "-") * width, "gray")

    def _terminal_width(self) -> int:
        return max(1, min(shutil.get_terminal_size((80, 24)).columns, 132))

    def _elapsed(self, task: TerminalTask) -> int:
        if task.started_at is None:
            return task.elapsed_seconds
        end = task.finished_at if task.finished_at is not None else time.monotonic()
        return max(task.elapsed_seconds, int(end - task.started_at))

    def _format_elapsed(self, seconds: int) -> str:
        minutes, remaining = divmod(max(0, seconds), 60)
        return f"{minutes:02d}:{remaining:02d}"

    def _styled(self, text: str, style: str) -> str:
        return self.writer.style(text, style)


__all__ = [
    "ANSI_BY_STYLE",
    "RESET",
    "_ANSI",
    "LoadingIndicator",
    "TerminalTask",
    "TerminalTaskBoard",
    "TerminalWriter",
    "_style",
]
