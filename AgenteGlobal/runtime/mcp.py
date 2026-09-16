"""Policy-governed registry for optional Model Context Protocol providers."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .artifacts import ArtifactStore
from .hooks import HookManager, HookPoint, emit_if_configured
from .policies import PolicyEngine, PolicyRequest
from .security_text import redact_sensitive_text


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_INLINE_BYTES = 64 * 1024


class MCPError(RuntimeError):
    pass


class MCPUnavailableError(MCPError):
    pass


class MCPToolNotFoundError(MCPError, KeyError):
    pass


@dataclass(frozen=True, slots=True)
class MCPStatus:
    provider: str
    available: bool
    reason: str = ""
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MCPTool:
    provider: str
    name: str
    description: str = ""
    permissions: tuple[str, ...] = ()
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    input_schema: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if _NAME_RE.fullmatch(self.provider) is None or _NAME_RE.fullmatch(self.name) is None:
            raise ValueError("MCP provider/tool name inválido")
        if len(self.description) > 2_000:
            raise ValueError("MCP description excede o limite")
        if not 0.1 <= float(self.timeout_seconds) <= 600:
            raise ValueError("MCP timeout precisa estar entre 0.1 e 600 segundos")
        object.__setattr__(self, "permissions", tuple(str(item) for item in self.permissions))
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))

    @property
    def qualified_name(self) -> str:
        return f"{self.provider}:{self.name}"


@dataclass(frozen=True, slots=True)
class MCPCallResult:
    provider: str
    tool: str
    status: str
    data: Any = None
    artifact_id: str | None = None
    size_bytes: int = 0
    duration_seconds: float = 0.0


@runtime_checkable
class MCPProvider(Protocol):
    async def status(self) -> MCPStatus: ...

    async def list_tools(self) -> Sequence[MCPTool | Mapping[str, Any]]: ...

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any: ...


@dataclass(slots=True)
class _ProviderRecord:
    name: str
    provider: MCPProvider
    enabled: bool
    tools: dict[str, MCPTool] = field(default_factory=dict)
    status: MCPStatus | None = None


class MCPRegistry:
    """One non-privileged path for discovery and invocation of MCP tools."""

    def __init__(
        self,
        *,
        policy_engine: PolicyEngine | None = None,
        hooks: HookManager | None = None,
        event_bus: Any = None,
        artifact_store: ArtifactStore | None = None,
        max_inline_bytes: int = DEFAULT_MAX_INLINE_BYTES,
    ) -> None:
        if max_inline_bytes < 1 or max_inline_bytes > 16 * 1024 * 1024:
            raise ValueError("max_inline_bytes fora do limite")
        self.policy_engine = policy_engine or PolicyEngine()
        self.hooks = hooks
        self.event_bus = event_bus
        self.artifact_store = artifact_store
        self.max_inline_bytes = max_inline_bytes
        self._providers: dict[str, _ProviderRecord] = {}

    def register(self, name: str, provider: MCPProvider, *, enabled: bool = True) -> None:
        normalized = name.strip().lower()
        if _NAME_RE.fullmatch(normalized) is None:
            raise ValueError("nome de provider MCP inválido")
        if normalized in self._providers:
            raise ValueError(f"provider MCP duplicado: {normalized}")
        for method in ("status", "list_tools", "call_tool"):
            if not callable(getattr(provider, method, None)):
                raise TypeError(f"provider MCP precisa implementar {method}()")
        self._providers[normalized] = _ProviderRecord(normalized, provider, bool(enabled))

    def enable(self, name: str, enabled: bool = True) -> None:
        self._record(name).enabled = bool(enabled)

    def _record(self, name: str) -> _ProviderRecord:
        try:
            return self._providers[name.strip().lower()]
        except KeyError as error:
            raise MCPUnavailableError(f"provider MCP não registrado: {name}") from error

    @staticmethod
    async def _resolve(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    async def _emit(self, name: str, *, provider: str, tool: str = "", status: str) -> None:
        if self.event_bus is not None:
            await self.event_bus.emit(
                name,
                source="mcp",
                payload={"provider": provider, "tool": tool, "status": status},
            )

    async def refresh(self, name: str) -> MCPStatus:
        record = self._record(name)
        if not record.enabled:
            status = MCPStatus(record.name, False, "provider desativado por configuração opt-in")
            record.status = status
            record.tools.clear()
            return status
        try:
            status = await self._resolve(record.provider.status())
        except Exception as error:
            status = MCPStatus(record.name, False, f"{type(error).__name__}: provider indisponível")
        if not isinstance(status, MCPStatus):
            raise MCPError("provider MCP retornou status inválido")
        if status.provider != record.name:
            status = MCPStatus(record.name, status.available, status.reason, status.capabilities)
        record.status = status
        record.tools.clear()
        if not status.available:
            return status
        try:
            discovered = await self._resolve(record.provider.list_tools())
            for item in tuple(discovered):
                tool = item if isinstance(item, MCPTool) else MCPTool(provider=record.name, **dict(item))
                if tool.provider != record.name:
                    raise MCPError("tool MCP declarou provider divergente")
                if tool.name in record.tools:
                    raise MCPError(f"tool MCP duplicada: {tool.name}")
                record.tools[tool.name] = tool
        except Exception as error:
            record.status = MCPStatus(record.name, False, f"{type(error).__name__}: discovery falhou")
            record.tools.clear()
        return record.status

    async def status(self, name: str) -> MCPStatus:
        record = self._record(name)
        return record.status or await self.refresh(name)

    async def list_statuses(self, *, refresh: bool = False) -> tuple[MCPStatus, ...]:
        """Return provider status snapshots through the public registry boundary."""
        statuses: list[MCPStatus] = []
        for name in sorted(self._providers):
            record = self._providers[name]
            statuses.append(await self.refresh(name) if refresh or record.status is None else record.status)
        return tuple(statuses)

    async def list_tools(self, name: str | None = None) -> tuple[MCPTool, ...]:
        names = (name.strip().lower(),) if name else tuple(sorted(self._providers))
        tools: list[MCPTool] = []
        for provider_name in names:
            record = self._record(provider_name)
            if record.status is None:
                await self.refresh(provider_name)
            tools.extend(record.tools.values())
        return tuple(sorted(tools, key=lambda item: item.qualified_name))

    async def call(
        self,
        provider_name: str,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        approval_granted: bool = False,
        validation_passed: bool = False,
        timeout_seconds: float | None = None,
    ) -> MCPCallResult:
        record = self._record(provider_name)
        status = await self.status(record.name)
        if not record.enabled or not status.available:
            raise MCPUnavailableError(status.reason or f"provider MCP indisponível: {record.name}")
        if tool_name not in record.tools:
            await self.refresh(record.name)
        try:
            tool = record.tools[tool_name]
        except KeyError as error:
            raise MCPToolNotFoundError(f"tool MCP não encontrada: {record.name}:{tool_name}") from error
        request = PolicyRequest(
            action="mcp.tool",
            resource=tool.qualified_name,
            attributes={"permissions": tool.permissions},
            approval_granted=approval_granted,
            validation_passed=validation_passed,
        )
        self.policy_engine.enforce(request)
        metadata = {"provider": record.name, "tool": tool.name, "permissions": tool.permissions}
        await emit_if_configured(self.hooks, HookPoint.BEFORE_TOOL, tool.qualified_name, metadata=metadata)
        await self._emit("mcp.call.started", provider=record.name, tool=tool.name, status="running")
        selected_timeout = tool.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if not 0.1 <= selected_timeout <= 600:
            raise ValueError("MCP timeout precisa estar entre 0.1 e 600 segundos")
        started = time.monotonic()
        try:
            async with asyncio.timeout(selected_timeout):
                data = await self._resolve(record.provider.call_tool(tool.name, dict(arguments or {})))
        except Exception:
            await self._emit("mcp.call.failed", provider=record.name, tool=tool.name, status="failed")
            raise
        serialized = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
        safe_serialized = redact_sensitive_text(serialized)
        encoded = safe_serialized.encode("utf-8")
        artifact_id: str | None = None
        returned_data: Any = safe_serialized if safe_serialized != serialized else data
        if len(encoded) > self.max_inline_bytes and self.artifact_store is not None:
            artifact = self.artifact_store.put(
                safe_serialized,
                summary=f"Resultado MCP externalizado: {tool.qualified_name}",
                media_type="application/json; charset=utf-8",
                metadata={"provider": record.name, "tool": tool.name, "untrusted": True},
            )
            artifact_id = artifact.artifact_id
            returned_data = {"artifact_id": artifact_id, "summary": artifact.summary, "preview": artifact.preview}
        duration = time.monotonic() - started
        await emit_if_configured(
            self.hooks,
            HookPoint.AFTER_TOOL,
            tool.qualified_name,
            metadata={**metadata, "status": "completed", "artifact_id": artifact_id},
            validation_passed=True,
        )
        await self._emit("mcp.call.completed", provider=record.name, tool=tool.name, status="completed")
        return MCPCallResult(record.name, tool.name, "completed", returned_data, artifact_id, len(encoded), duration)


__all__ = [
    "MCPCallResult",
    "MCPError",
    "MCPProvider",
    "MCPRegistry",
    "MCPStatus",
    "MCPTool",
    "MCPToolNotFoundError",
    "MCPUnavailableError",
]
