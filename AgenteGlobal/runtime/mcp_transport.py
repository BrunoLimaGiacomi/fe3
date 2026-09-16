"""Interoperable MCP stdio transport backed by the official Python SDK."""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from .mcp import MCPError, MCPStatus, MCPTool, MCPUnavailableError
from .security_text import redact_sensitive_text


DEFAULT_STDERR_LIMIT = 64 * 1024


class _BoundedTextSink(io.TextIOBase):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._parts: list[str] = []
        self._size = 0

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        text = str(value)
        remaining = max(0, self.limit - self._size)
        if remaining:
            accepted = text[:remaining]
            self._parts.append(accepted)
            self._size += len(accepted)
        return len(text)

    def text(self) -> str:
        return redact_sensitive_text("".join(self._parts))

    def append_bytes(self, value: bytes) -> None:
        self.write(value.decode("utf-8", errors="replace"))


class StdioMCPProvider:
    """Expose an MCP server process through the runtime's governed registry.

    Every operation uses the official SDK handshake and a short-lived stdio
    session. This keeps lifecycle/cleanup deterministic and avoids orphaned
    provider processes when the optional transport is idle.
    """

    def __init__(
        self,
        provider: str,
        *,
        command: str,
        args: Sequence[str] = (),
        workspace: Path | str,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float = 60.0,
        stderr_limit: int = DEFAULT_STDERR_LIMIT,
    ) -> None:
        self.provider = provider.strip().lower()
        if not self.provider or len(self.provider) > 128:
            raise ValueError("provider MCP inválido")
        self.command = str(command).strip()
        if not self.command or len(self.command) > 32_768:
            raise ValueError("command MCP inválido")
        self.args = tuple(str(item) for item in args)
        if len(self.args) > 256 or any(len(item) > 32_768 for item in self.args):
            raise ValueError("args MCP excedem o limite")
        self.workspace = Path(workspace).resolve(strict=True)
        selected_cwd = self.workspace if cwd is None else Path(cwd).resolve(strict=True)
        try:
            selected_cwd.relative_to(self.workspace)
        except ValueError as error:
            raise ValueError("cwd MCP deve permanecer dentro do workspace") from error
        if not selected_cwd.is_dir():
            raise ValueError("cwd MCP precisa ser um diretório")
        self.cwd = selected_cwd
        self.env = {str(key): str(value) for key, value in dict(env or {}).items()}
        if len(self.env) > 128 or any(not key or "\x00" in key or "\x00" in value for key, value in self.env.items()):
            raise ValueError("env MCP inválido ou excessivo")
        self.timeout_seconds = float(timeout_seconds)
        if not 0.1 <= self.timeout_seconds <= 600:
            raise ValueError("timeout MCP precisa estar entre 0.1 e 600 segundos")
        if not 1 <= stderr_limit <= 1024 * 1024:
            raise ValueError("stderr_limit MCP fora do limite")
        self._stderr = _BoundedTextSink(stderr_limit)

    @property
    def stderr_text(self) -> str:
        """Return only bounded, redacted diagnostic text."""

        return self._stderr.text()

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[Any]:
        try:
            from mcp import Client
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ImportError as error:
            raise MCPUnavailableError("SDK MCP oficial não está instalado") from error

        parameters = StdioServerParameters(
            command=self.command,
            args=list(self.args),
            env=dict(self.env) or None,
            cwd=self.cwd,
            encoding="utf-8",
            encoding_error_handler="replace",
        )
        read_fd, write_fd = os.pipe()
        read_stream = os.fdopen(read_fd, "rb", buffering=0)
        write_stream = os.fdopen(
            write_fd,
            "w",
            encoding="utf-8",
            errors="replace",
            buffering=1,
        )

        async def drain_stderr() -> None:
            while True:
                chunk = await asyncio.to_thread(read_stream.read, 8 * 1024)
                if not chunk:
                    return
                self._stderr.append_bytes(chunk)

        drain_task = asyncio.create_task(drain_stderr(), name=f"mcp:{self.provider}:stderr")
        try:
            transport = stdio_client(parameters, errlog=write_stream)
            async with Client(
                transport,
                read_timeout_seconds=self.timeout_seconds,
                raise_exceptions=True,
            ) as client:
                yield client
        finally:
            write_stream.close()
            try:
                await asyncio.wait_for(drain_task, timeout=2.0)
            except TimeoutError:
                drain_task.cancel()
                await asyncio.gather(drain_task, return_exceptions=True)
            finally:
                read_stream.close()

    async def status(self) -> MCPStatus:
        try:
            async with self._client() as client:
                raw = client.server_capabilities.model_dump(exclude_none=True)
                capabilities = tuple(sorted(str(name) for name in raw))
                return MCPStatus(self.provider, True, capabilities=capabilities)
        except Exception as error:
            return MCPStatus(
                self.provider,
                False,
                f"{type(error).__name__}: transporte MCP stdio indisponível",
            )

    async def list_tools(self) -> tuple[MCPTool, ...]:
        async with self._client() as client:
            result = await client.list_tools()
        tools: list[MCPTool] = []
        for item in result.tools:
            annotations = item.annotations
            permissions = ["mcp.external"]
            if annotations is not None and annotations.read_only_hint is True:
                permissions.append("mcp.read_only")
            else:
                permissions.append("mcp.mutation_unknown")
            if annotations is not None and annotations.destructive_hint is True:
                permissions.append("mcp.destructive")
            if annotations is not None and annotations.open_world_hint is True:
                permissions.append("mcp.open_world")
            tools.append(
                MCPTool(
                    provider=self.provider,
                    name=item.name,
                    description=item.description or "",
                    permissions=tuple(permissions),
                    timeout_seconds=self.timeout_seconds,
                    input_schema=item.input_schema,
                )
            )
        return tuple(tools)

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        async with self._client() as client:
            result = await client.call_tool(
                name,
                dict(arguments),
                read_timeout_seconds=self.timeout_seconds,
            )
        if result.is_error:
            raise MCPError(f"tool MCP retornou erro: {self.provider}:{name}")
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)


__all__ = ["DEFAULT_STDERR_LIMIT", "StdioMCPProvider"]
