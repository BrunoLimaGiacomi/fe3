"""Single-writer terminal UI facade for EventBus and read-only exploration."""

from __future__ import annotations

from typing import Any, TextIO

from .events import RuntimeEvent
from .observers import TerminalEventConsumer
from .terminal_task_board import TerminalTaskBoard, TerminalWriter, _strip_terminal_controls
from .terminal_tool_views import render_exploration_report


class TerminalOutputCoordinator:
    """Own one writer, one event consumer and one task panel per session.

    ``EventBus.subscribe(ui)`` is enough to connect the coordinator.  The
    explicit ``subscribe`` helper is convenient for Core integration and keeps
    the subscription ID available for deterministic teardown.
    """

    def __init__(
        self,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        enabled: bool = True,
        max_tasks: int = 1,
        max_visible_tasks: int = TerminalTaskBoard.MAX_VISIBLE_TASKS,
        color: bool | None = None,
        unicode: bool | None = None,
        cursor: bool | None = None,
        task_board: TerminalTaskBoard | None = None,
        writer: TerminalWriter | None = None,
    ) -> None:
        if writer is None and task_board is not None:
            writer = task_board.writer
        self.writer = writer or TerminalWriter(
            stdout=stdout,
            stderr=stderr,
            color=color,
            cursor=cursor,
        )
        self.task_board = task_board or TerminalTaskBoard(
            max_tasks,
            enabled=enabled,
            writer=self.writer,
            unicode=unicode,
            max_visible_tasks=max_visible_tasks,
        )
        self.consumer = TerminalEventConsumer(
            enabled=enabled,
            writer=self.writer,
            task_board=self.task_board,
        )
        self.enabled = bool(enabled)

    def __call__(self, event: RuntimeEvent) -> None:
        self.consumer(event)

    handle = __call__

    def subscribe(self, event_bus: Any) -> int:
        return self.consumer.subscribe_to(event_bus)

    def unsubscribe(self) -> None:
        self.consumer.close()

    def close(self) -> None:
        self.consumer.close()

    def prepare_for_input(self) -> None:
        """Hand cursor ownership to the interactive prompt safely."""

        self.consumer._stop_loading()
        self.consumer._close_response()
        self.task_board.prepare_for_input()

    def begin_turn(self) -> None:
        """Reset transient per-request UI state after input was accepted."""

        self.task_board.begin_turn()

    def write(self, text: str, *, stream: TextIO | None = None, style: str | None = None) -> None:
        """Write a non-event message without colliding with a live board."""

        self.task_board.clear_live()
        if style:
            target = stream or self.writer.stdout
            text = self.writer.style(_strip_terminal_controls(text), style, stream=target)
            self.writer.write(text, stream=stream, sanitize=False)
            return
        self.writer.write(text, stream=stream, sanitize=True)

    def line(self, text: str = "", *, stream: TextIO | None = None, style: str | None = None) -> None:
        self.task_board.clear_live()
        self.writer.line(text, stream=stream, style=style)

    def render_exploration(self, report: Any, *, use_unicode: bool | None = None) -> str:
        """Render a typed ExplorationReport with terminal-safe diagrams."""

        value = render_exploration_report(
            report,
            use_unicode=self.task_board.unicode if use_unicode is None else use_unicode,
        )
        self.line(value)
        return value

    def __enter__(self) -> "TerminalOutputCoordinator":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class TerminalUI(TerminalOutputCoordinator):
    """Named facade used by the Core integration boundary."""


# Keep a short alias for integrations that call this a terminal coordinator.
TerminalCoordinator = TerminalOutputCoordinator


__all__ = ["TerminalCoordinator", "TerminalOutputCoordinator", "TerminalUI"]
