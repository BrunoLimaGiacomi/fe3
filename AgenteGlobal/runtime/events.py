"""Eventos locais e assíncronos para observabilidade do runtime.

O barramento deliberadamente não conhece a CLI, a TUI, o modelo ou as
ferramentas. Assim, consumidores de observabilidade não entram no caminho
crítico de uma execução do agente.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any


PHASE3_EVENT_NAMES = frozenset(
    {
        "run.started",
        "run.completed",
        "llm.request_started",
        "llm.first_token",
        "llm.text_delta",
        "llm.tool_call_started",
        "llm.tool_call_completed",
        "llm.request_completed",
        "agent.started",
        "agent.waiting_model",
        "agent.completed",
        "tool.started",
        "tool.stdout",
        "tool.stderr",
        "tool.completed",
        "tool.failed",
    }
)

PHASE5_EVENT_NAMES = frozenset(
    {
        "workflow.state_changed",
        "plan.persisted",
        "plan.approval_recorded",
        "plan.scope_expansion_requested",
    }
)

PHASE6_EVENT_NAMES = frozenset(
    {
        "scheduler.started",
        "scheduler.completed",
        "scheduler.failed",
        "scheduler.cancelled",
        "task.created",
        "task.ready",
        "task.running",
        "task.blocked",
        "task.completed",
        "task.failed_retryable",
        "task.failed_final",
        "task.cancelled",
        "review.started",
        "review.passed",
        "review.failed",
        "repair.started",
        "repair.completed",
        "repair.exhausted",
    }
)

PHASE8_EVENT_NAMES = frozenset(
    {
        "mcp.call.started",
        "mcp.call.completed",
        "mcp.call.failed",
        "browser.action.started",
        "browser.action.completed",
        "browser.action.failed",
    }
)

PHASE9_EVENT_NAMES = frozenset(
    {
        "spec_workflow.stage_changed",
        "analyze.completed",
        "analyze.blocked",
        "convergence.passed",
        "convergence.gaps_found",
        "convergence.repair_started",
        "convergence.failed",
        "context.prepared",
        "context.compacted",
        "retrieval.completed",
        "skill.loaded",
        "artifact.created",
    }
)

PHASE11_EVENT_NAMES = frozenset(
    {
        "code_index.updated",
        "lsp.server.starting",
        "lsp.server.started",
        "lsp.server.stopped",
        "lsp.request.started",
        "lsp.request.completed",
        "lsp.notification.invalid",
        "lsp.document.synced",
        "lsp.document.fallback",
        "lsp.diagnostics.published",
        "code_index.write_synced",
        "code_index.write_sync_skipped",
        "code_index.write_sync_failed",
        "exploration.started",
        "exploration.completed",
    }
)


def _freeze(value: Any) -> Any:
    """Make common payload containers immutable before exposing an event."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """Snapshot imutável de uma ocorrência observável no runtime."""

    name: str
    sequence: int
    timestamp: datetime
    monotonic_timestamp: float
    source: str
    payload: Mapping[str, Any]


EventHandler = Callable[[RuntimeEvent], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class ToolExecutionMetadata:
    """Runtime-owned safety facts used before a tool batch is parallelized."""

    parallel_safe: bool
    side_effect_free: bool
    resource_keys: tuple[str, ...] = ()
    mutation_scope: str = "none"


_READ_ONLY_TOOLS = frozenset(
    {
        "list_dir",
        "read_file",
        "read_artifact",
        "retrieve_context",
        "search_text",
        "codeintel_query",
        "code_intelligence",
        "lsp_query",
    }
)
DEFAULT_MAX_PARALLEL_TOOLS = 4


def tool_execution_metadata(name: str, arguments: Mapping[str, Any] | None = None) -> ToolExecutionMetadata:
    """Classify a tool conservatively from its typed name and bounded arguments."""

    arguments = arguments or {}
    if name in _READ_ONLY_TOOLS:
        path = str(arguments.get("path") or arguments.get("file") or ".").strip()
        reference = str(arguments.get("path_reference") or "").strip()
        resource = f"{reference}:{path}" if reference else path
        if name in {"search_text", "retrieve_context", "codeintel_query", "code_intelligence", "lsp_query"}:
            selector = str(
                arguments.get("pattern")
                or arguments.get("query")
                or arguments.get("symbol")
                or "query"
            ).strip()
            resource = f"{resource}#{selector}"
        return ToolExecutionMetadata(
            parallel_safe=True,
            side_effect_free=True,
            resource_keys=(resource,),
            mutation_scope="none",
        )
    # Preserve the existing read-only spawn behavior, but never infer safety
    # for a mutating child or for arbitrary tools proposed by the model.
    if name == "spawn_subagent" and not bool(arguments.get("allow_mutation", False)):
        return ToolExecutionMetadata(True, True, (), "none")
    return ToolExecutionMetadata(False, False, (), "workspace" if name else "unknown")


def can_parallelize_tools(
    requests: Sequence[tuple[str, Mapping[str, Any]]],
) -> bool:
    """Return true only for independent, side-effect-free requests."""

    if len(requests) < 2:
        return False
    metadata = [tool_execution_metadata(name, arguments) for name, arguments in requests]
    if not all(item.parallel_safe and item.side_effect_free for item in metadata):
        return False
    keys = [key for item in metadata for key in item.resource_keys]
    return len(keys) == len(set(keys))


async def execute_tools_bounded(
    requests: Sequence[tuple[str, Mapping[str, Any]]],
    executor: Callable[[str, dict[str, Any]], Awaitable[str]],
    *,
    max_concurrency: int = DEFAULT_MAX_PARALLEL_TOOLS,
) -> list[str]:
    """Execute an independent batch with bounded concurrency and safe cancel."""

    if max_concurrency < 1:
        raise ValueError("max_concurrency must be positive")
    if not can_parallelize_tools(requests):
        return [await executor(name, dict(arguments)) for name, arguments in requests]

    semaphore = asyncio.Semaphore(max_concurrency)

    async def run_one(name: str, arguments: Mapping[str, Any]) -> str:
        async with semaphore:
            return await executor(name, dict(arguments))

    tasks = [asyncio.create_task(run_one(name, arguments)) for name, arguments in requests]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


@dataclass(slots=True)
class _TimingStats:
    count: int = 0
    total_seconds: float = 0.0
    max_seconds: float = 0.0

    def observe(self, duration: float) -> None:
        self.count += 1
        self.total_seconds += duration
        self.max_seconds = max(self.max_seconds, duration)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "count": self.count,
            "total_seconds": self.total_seconds,
            "average_seconds": self.total_seconds / self.count if self.count else 0.0,
            "max_seconds": self.max_seconds,
        }


class EventBus:
    """Barramento local para eventos do runtime, compatível com ``asyncio``.

    A ordem de inscrição é preservada. Uma falha em um consumidor é isolada e
    nunca impede os consumidores seguintes nem o runtime emissor.
    """

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = logger
        self._next_sequence = 0
        self._next_subscription_id = 0
        self._subscriptions: dict[int, tuple[EventHandler, frozenset[str] | None]] = {}
        self._sequence_lock = asyncio.Lock()
        self._profile_count = 0
        self._profile_total_seconds = 0.0
        self._profile_max_seconds = 0.0
        self._profile_samples: list[float] = []
        self._profile_by_event: dict[str, _TimingStats] = {}
        self._profile_by_subscriber: dict[str, _TimingStats] = {}

    def subscribe(
        self,
        handler_or_name: EventHandler | str,
        handler: EventHandler | None = None,
        *,
        names: Iterable[str] | None = None,
    ) -> int:
        """Subscribe a handler globally or only to selected event names.

        Both ``subscribe(handler, names={...})`` and the concise
        ``subscribe("tool.stdout", handler)`` form are supported.
        """
        if isinstance(handler_or_name, str):
            if handler is None:
                raise TypeError("handler é obrigatório ao informar o nome do evento.")
            if names is not None:
                raise TypeError("Use o nome posicional ou names, não ambos.")
            callback = handler
            filters: frozenset[str] | None = frozenset((handler_or_name,))
        else:
            if handler is not None:
                raise TypeError("handler deve ser informado apenas uma vez.")
            callback = handler_or_name
            filters = frozenset(names) if names is not None else None

        if not callable(callback):
            raise TypeError("handler precisa ser chamável.")

        subscription_id = self._next_subscription_id
        self._next_subscription_id += 1
        self._subscriptions[subscription_id] = (callback, filters)
        return subscription_id

    def unsubscribe(self, subscription_id: int) -> bool:
        """Remove uma inscrição e informa se ela ainda existia."""
        return self._subscriptions.pop(subscription_id, None) is not None

    async def emit(
        self,
        name: str,
        *,
        source: str = "runtime",
        payload: Mapping[str, Any] | None = None,
    ) -> RuntimeEvent:
        """Publica um evento e aguarda consumidores síncronos ou assíncronos."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name do evento precisa ser uma string não vazia.")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source do evento precisa ser uma string não vazia.")

        emit_started = time.perf_counter()
        async with self._sequence_lock:
            self._next_sequence += 1
            event = RuntimeEvent(
                name=name,
                sequence=self._next_sequence,
                timestamp=datetime.now(timezone.utc),
                monotonic_timestamp=time.monotonic(),
                source=source,
                payload=_freeze(dict(payload or {})),
            )
            subscriptions = tuple(self._subscriptions.items())

        for subscription_id, (callback, filters) in subscriptions:
            if filters is not None and event.name not in filters:
                continue
            try:
                callback_started = time.perf_counter()
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
                callback_duration = time.perf_counter() - callback_started
                self._profile_by_subscriber.setdefault(
                    f"subscription:{subscription_id}", _TimingStats()
                ).observe(callback_duration)
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
                if self._logger is not None:
                    self._logger.warning(
                        "Consumidor cancelou apenas a si mesmo; evento=%s subscription=%s",
                        event.name,
                        subscription_id,
                    )
            except Exception:
                if self._logger is not None:
                    self._logger.exception(
                        "Consumidor de evento falhou; evento=%s subscription=%s",
                        event.name,
                        subscription_id,
                    )
        elapsed = time.perf_counter() - emit_started
        self._profile_count += 1
        self._profile_total_seconds += elapsed
        self._profile_max_seconds = max(self._profile_max_seconds, elapsed)
        if len(self._profile_samples) < 4096:
            self._profile_samples.append(elapsed)
        self._profile_by_event.setdefault(event.name, _TimingStats()).observe(elapsed)
        return event

    def profile(self) -> dict[str, Any]:
        """Return bounded timing facts for local before/after benchmarks."""

        samples = sorted(self._profile_samples)
        p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))] if samples else 0.0
        return {
            "count": self._profile_count,
            "total_awaited_seconds": self._profile_total_seconds,
            "average_seconds": self._profile_total_seconds / self._profile_count
            if self._profile_count
            else 0.0,
            "p95_seconds": p95,
            "max_seconds": self._profile_max_seconds,
            "by_subscriber": {
                key: stats.as_dict() for key, stats in sorted(self._profile_by_subscriber.items())
            },
            "by_event_type": {
                key: stats.as_dict() for key, stats in sorted(self._profile_by_event.items())
            },
        }

    def reset_profile(self) -> None:
        self._profile_count = 0
        self._profile_total_seconds = 0.0
        self._profile_max_seconds = 0.0
        self._profile_samples.clear()
        self._profile_by_event.clear()
        self._profile_by_subscriber.clear()
