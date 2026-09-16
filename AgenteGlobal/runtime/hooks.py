"""Deterministic lifecycle hooks for runtime operations.

Hooks receive bounded metadata, never implicit credentials or raw tool payloads.
They may block an operation or require an explicit validation result.
"""

from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class HookPoint(StrEnum):
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_WRITE = "before_write"
    AFTER_WRITE = "after_write"
    BEFORE_AGENT_SPAWN = "before_agent_spawn"
    AFTER_AGENT_SPAWN = "after_agent_spawn"
    BEFORE_TASK = "before_task"
    AFTER_TASK = "after_task"
    BEFORE_BROWSER_ACTION = "before_browser_action"
    AFTER_BROWSER_ACTION = "after_browser_action"
    BEFORE_SKILL_INSTALL = "before_skill_install"
    AFTER_SKILL_INSTALL = "after_skill_install"
    BEFORE_FINAL = "before_final"


class HookEffect(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    REQUIRE_VALIDATION = "require_validation"


class HookError(RuntimeError):
    pass


class HookBlockedError(PermissionError, HookError):
    pass


class HookValidationError(HookError):
    pass


@dataclass(frozen=True, slots=True)
class HookContext:
    point: HookPoint
    action: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    validation_passed: bool | None = None
    correlation_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.action or len(self.action) > 256:
            raise ValueError("hook action must be a bounded non-empty string")
        if len(self.metadata) > 64:
            raise ValueError("hook metadata exceeds 64 entries")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class HookResult:
    effect: HookEffect = HookEffect.ALLOW
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.reason) > 2_000:
            raise ValueError("hook reason exceeds 2000 characters")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class HookExecution:
    point: HookPoint
    action: str
    results: tuple[HookResult, ...]

    @property
    def requires_validation(self) -> bool:
        return any(result.effect is HookEffect.REQUIRE_VALIDATION for result in self.results)


HookCallback = Callable[[HookContext], HookResult | bool | None | Awaitable[HookResult | bool | None]]


@dataclass(frozen=True, slots=True)
class _Registration:
    name: str
    callback: HookCallback
    priority: int


class HookManager:
    """Ordered hook registry with fail-closed blocking and validation semantics."""

    def __init__(self) -> None:
        self._hooks: dict[HookPoint, list[_Registration]] = defaultdict(list)

    def register(
        self,
        point: HookPoint | str,
        callback: HookCallback,
        *,
        name: str | None = None,
        priority: int = 100,
    ) -> None:
        selected = HookPoint(point)
        if not callable(callback):
            raise TypeError("hook callback must be callable")
        hook_name = (name or getattr(callback, "__name__", "hook")).strip()
        if not hook_name or len(hook_name) > 128:
            raise ValueError("hook name must be bounded")
        registration = _Registration(hook_name, callback, int(priority))
        self._hooks[selected].append(registration)
        self._hooks[selected].sort(key=lambda item: (item.priority, item.name))

    def unregister(self, point: HookPoint | str, name: str) -> bool:
        selected = HookPoint(point)
        before = len(self._hooks[selected])
        self._hooks[selected] = [item for item in self._hooks[selected] if item.name != name]
        return len(self._hooks[selected]) != before

    def names(self, point: HookPoint | str) -> tuple[str, ...]:
        return tuple(item.name for item in self._hooks[HookPoint(point)])

    @staticmethod
    def _coerce(value: HookResult | bool | None) -> HookResult:
        if value is None or value is True:
            return HookResult()
        if value is False:
            return HookResult(HookEffect.BLOCK, "hook returned false")
        if not isinstance(value, HookResult):
            raise TypeError("hook callback must return HookResult, bool or None")
        return value

    async def emit(
        self,
        point: HookPoint | str,
        action: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        validation_passed: bool | None = None,
        correlation_id: str | None = None,
    ) -> HookExecution:
        context = HookContext(
            point=HookPoint(point),
            action=action,
            metadata=metadata or {},
            validation_passed=validation_passed,
            correlation_id=correlation_id,
        )
        results: list[HookResult] = []
        for registration in tuple(self._hooks[context.point]):
            value = registration.callback(context)
            if inspect.isawaitable(value):
                value = await value
            result = self._coerce(value)
            results.append(result)
            if result.effect is HookEffect.BLOCK:
                raise HookBlockedError(f"Hook {registration.name} bloqueou {action}: {result.reason}")
            if result.effect is HookEffect.REQUIRE_VALIDATION and validation_passed is not True:
                raise HookValidationError(
                    f"Hook {registration.name} exige validação aprovada para {action}: {result.reason}"
                )
        return HookExecution(context.point, action, tuple(results))


async def emit_if_configured(
    manager: HookManager | None,
    point: HookPoint | str,
    action: str,
    **kwargs: Any,
) -> HookExecution | None:
    if manager is None:
        return None
    return await manager.emit(point, action, **kwargs)


__all__ = [
    "HookBlockedError",
    "HookContext",
    "HookEffect",
    "HookError",
    "HookExecution",
    "HookManager",
    "HookPoint",
    "HookResult",
    "HookValidationError",
    "emit_if_configured",
]
