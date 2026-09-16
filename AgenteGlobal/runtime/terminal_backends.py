"""Optional Herdr terminal sessions with a mandatory local fallback.

The DAG Scheduler remains the canonical owner of tasks, dependencies, states
and locks. Backends only execute or observe terminal processes.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .process import ProcessRequest, ProcessResult, ProcessRunner
from .security_text import safe_subprocess_env


class TerminalBackendError(RuntimeError):
    pass


class HerdrUnavailableError(TerminalBackendError):
    pass


@dataclass(frozen=True, slots=True)
class TerminalBackendStatus:
    name: str
    available: bool
    persistent: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TerminalRequest:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int = 60
    persistent: bool = False
    pane_id: str | None = None

    def __post_init__(self) -> None:
        if not self.argv or any(not str(value) or "\x00" in str(value) for value in self.argv):
            raise ValueError("terminal argv inválido")
        if not self.cwd.is_absolute():
            raise ValueError("terminal cwd precisa ser absoluto")
        if not 1 <= self.timeout_seconds <= 86_400:
            raise ValueError("terminal timeout fora do limite")
        object.__setattr__(self, "argv", tuple(str(value) for value in self.argv))
        object.__setattr__(self, "env", MappingProxyType({str(k): str(v) for k, v in self.env.items()}))


@dataclass(frozen=True, slots=True)
class TerminalResult:
    backend: str
    returncode: int
    stdout: str
    stderr: str
    persistent: bool
    pane_id: str | None = None
    fallback_reason: str | None = None


@runtime_checkable
class TerminalBackend(Protocol):
    name: str

    async def status(self) -> TerminalBackendStatus: ...

    async def run(self, request: TerminalRequest) -> TerminalResult: ...


class LocalTerminalBackend:
    name = "local"

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner or ProcessRunner()

    async def status(self) -> TerminalBackendStatus:
        return TerminalBackendStatus(self.name, True, False)

    async def run(self, request: TerminalRequest) -> TerminalResult:
        result = await self.runner.run_stream(
            ProcessRequest(request.argv, request.cwd, request.env, request.timeout_seconds)
        )
        return TerminalResult(self.name, result.returncode, result.stdout, result.stderr, False)


class HerdrCLIClient:
    """Bounded wrapper over documented Herdr CLI commands; never uses a shell."""

    def __init__(self, *, binary: str = "herdr", runner: ProcessRunner | None = None) -> None:
        if not binary.strip() or "\x00" in binary:
            raise ValueError("Herdr binary inválido")
        self.binary = binary
        self.runner = runner or ProcessRunner()

    def installed(self) -> bool:
        return shutil.which(self.binary) is not None

    async def _run(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: int) -> ProcessResult:
        return await self.runner.run_stream(
            ProcessRequest((self.binary, *tuple(args)), cwd, env, timeout)
        )

    async def status(self, *, cwd: Path, env: Mapping[str, str], timeout: int = 10) -> ProcessResult:
        return await self._run(("status",), cwd=cwd, env=env, timeout=timeout)

    async def create_workspace(
        self,
        *,
        cwd: Path,
        label: str,
        env: Mapping[str, str],
        timeout: int = 30,
    ) -> ProcessResult:
        if not label.strip() or len(label) > 128 or "\x00" in label:
            raise ValueError("Herdr workspace label inválido")
        return await self._run(
            ("workspace", "create", "--cwd", str(cwd), "--label", label),
            cwd=cwd,
            env=env,
            timeout=timeout,
        )

    async def split_pane(
        self,
        pane_id: str,
        *,
        direction: str,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int = 30,
    ) -> ProcessResult:
        if direction not in {"right", "down", "left", "up"}:
            raise ValueError("direção de pane Herdr inválida")
        return await self._run(
            ("pane", "split", pane_id, "--direction", direction),
            cwd=cwd,
            env=env,
            timeout=timeout,
        )

    async def run_in_pane(self, request: TerminalRequest) -> ProcessResult:
        if not request.pane_id or "\x00" in request.pane_id:
            raise ValueError("Herdr exige pane_id para execução persistente")
        command = shlex.join(request.argv)
        return await self._run(
            ("pane", "run", request.pane_id, command),
            cwd=request.cwd,
            env=request.env,
            timeout=request.timeout_seconds,
        )

    async def read_pane(
        self,
        pane_id: str,
        *,
        cwd: Path,
        env: Mapping[str, str],
        lines: int = 80,
        timeout: int = 30,
    ) -> ProcessResult:
        if not 1 <= lines <= 10_000:
            raise ValueError("Herdr read lines fora do limite")
        return await self._run(
            ("pane", "read", pane_id, "--source", "recent-unwrapped", "--lines", str(lines)),
            cwd=cwd,
            env=env,
            timeout=timeout,
        )


class HerdrTerminalBackend:
    name = "herdr"

    def __init__(self, client: HerdrCLIClient | Any | None = None, *, probe_cwd: Path | None = None) -> None:
        self.client = client or HerdrCLIClient()
        self.probe_cwd = (probe_cwd or Path.cwd()).resolve()

    async def status(self) -> TerminalBackendStatus:
        if callable(getattr(self.client, "installed", None)) and not self.client.installed():
            return TerminalBackendStatus(self.name, False, True, "binário Herdr não encontrado")
        try:
            result = await self.client.status(
                cwd=self.probe_cwd,
                env=safe_subprocess_env(),
                timeout=10,
            )
        except (OSError, TimeoutError, subprocess.SubprocessError) as error:
            return TerminalBackendStatus(self.name, False, True, f"{type(error).__name__}: Herdr indisponível")
        available = result.returncode == 0
        return TerminalBackendStatus(
            self.name,
            available,
            True,
            "" if available else (result.stderr.strip()[:500] or "Herdr status falhou"),
        )

    async def run(self, request: TerminalRequest) -> TerminalResult:
        if not request.persistent:
            raise ValueError("HerdrTerminalBackend aceita somente requests persistentes")
        status = await self.status()
        if not status.available:
            raise HerdrUnavailableError(status.reason)
        result = await self.client.run_in_pane(request)
        return TerminalResult(
            self.name,
            result.returncode,
            result.stdout,
            result.stderr,
            True,
            pane_id=request.pane_id,
        )


class TerminalBackendRouter:
    """Select Herdr only for persistent requests and always retain local fallback."""

    def __init__(
        self,
        *,
        local: TerminalBackend | None = None,
        herdr: TerminalBackend | None = None,
        prefer_herdr: bool = False,
    ) -> None:
        self.local = local or LocalTerminalBackend()
        self.herdr = herdr
        self.prefer_herdr = bool(prefer_herdr)

    async def run(self, request: TerminalRequest) -> TerminalResult:
        fallback_reason: str | None = None
        if request.persistent and self.prefer_herdr and self.herdr is not None:
            try:
                status = await self.herdr.status()
                if status.available:
                    return await self.herdr.run(request)
                fallback_reason = status.reason or "Herdr indisponível"
            except (TerminalBackendError, OSError, TimeoutError, subprocess.SubprocessError) as error:
                fallback_reason = f"{type(error).__name__}: Herdr indisponível durante a sessão"
        elif request.persistent:
            fallback_reason = "Herdr não configurado ou não selecionado"

        local_request = TerminalRequest(
            argv=request.argv,
            cwd=request.cwd,
            env=request.env,
            timeout_seconds=request.timeout_seconds,
            persistent=False,
        )
        result = await self.local.run(local_request)
        return TerminalResult(
            result.backend,
            result.returncode,
            result.stdout,
            result.stderr,
            False,
            fallback_reason=fallback_reason,
        )


@dataclass(frozen=True, slots=True)
class DAGRVisualization:
    """Optional renderer payload; it is never canonical scheduler state."""

    run_file: Path
    source: str = "agenteglobal-dag-snapshot"
    canonical: bool = False

    def __post_init__(self) -> None:
        if self.canonical:
            raise ValueError("herdr-dagr não pode ser marcado como estado canônico")


__all__ = [
    "DAGRVisualization",
    "HerdrCLIClient",
    "HerdrTerminalBackend",
    "HerdrUnavailableError",
    "LocalTerminalBackend",
    "TerminalBackend",
    "TerminalBackendError",
    "TerminalBackendRouter",
    "TerminalBackendStatus",
    "TerminalRequest",
    "TerminalResult",
]
