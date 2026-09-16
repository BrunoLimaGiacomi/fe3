from __future__ import annotations

import asyncio
import codecs
import inspect
import io
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence


StreamCallback = Callable[[str], Awaitable[None] | None]
_READ_CHUNK_SIZE = 4_096
_TERMINATE_GRACE_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    argv: Sequence[str]
    cwd: Path
    env: Mapping[str, str]
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner:
    """Fronteira substituível para subprocessos, com execução sem shell."""

    def run(self, request: ProcessRequest) -> ProcessResult:
        if not request.argv:
            raise ValueError("O processo precisa de ao menos um argumento executável.")
        completed = subprocess.run(
            list(request.argv),
            cwd=request.cwd,
            env=dict(request.env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=request.timeout_seconds,
            check=False,
            shell=False,
        )
        return ProcessResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    async def run_stream(
        self,
        request: ProcessRequest,
        on_stdout: StreamCallback | None = None,
        on_stderr: StreamCallback | None = None,
    ) -> ProcessResult:
        """Executa um processo e entrega stdout/stderr incrementalmente.

        A captura integral é mantida para compatibilidade com consumidores que
        precisam do resultado final. Falhas dos callbacks são propagadas após o
        encerramento do subprocesso, sem deixar processos órfãos.
        """
        if not request.argv:
            raise ValueError("O processo precisa de ao menos um argumento executável.")

        process = await asyncio.create_subprocess_exec(
            *request.argv,
            cwd=request.cwd,
            env=dict(request.env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        stdout_task = asyncio.create_task(
            self._read_stream(process.stdout, stdout_parts, on_stdout),
            name="process-stdout-reader",
        )
        stderr_task = asyncio.create_task(
            self._read_stream(process.stderr, stderr_parts, on_stderr),
            name="process-stderr-reader",
        )
        readers = (stdout_task, stderr_task)
        completion = asyncio.gather(process.wait(), *readers)

        try:
            await asyncio.wait_for(
                asyncio.shield(completion),
                timeout=request.timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            await self._stop_process(process)
            await asyncio.gather(process.wait(), *readers, return_exceptions=True)
            raise subprocess.TimeoutExpired(
                list(request.argv),
                request.timeout_seconds,
                output="".join(stdout_parts),
                stderr="".join(stderr_parts),
            ) from error
        except asyncio.CancelledError:
            await self._stop_process(process)
            await asyncio.gather(process.wait(), *readers, return_exceptions=True)
            raise
        except BaseException:
            await self._stop_process(process)
            await asyncio.gather(process.wait(), *readers, return_exceptions=True)
            raise

        return ProcessResult(
            returncode=process.returncode,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
        )

    @staticmethod
    async def _read_stream(
        stream: asyncio.StreamReader,
        collected: list[str],
        callback: StreamCallback | None,
    ) -> None:
        # subprocess.run(text=True) normaliza CRLF; mantenha o mesmo contrato
        # também no caminho incremental, inclusive se CR/LF cruzar um bloco.
        decoder = io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder("utf-8")(errors="replace"),
            translate=True,
        )
        while chunk := await stream.read(_READ_CHUNK_SIZE):
            await ProcessRunner._record_chunk(decoder.decode(chunk), collected, callback)
        await ProcessRunner._record_chunk(decoder.decode(b"", final=True), collected, callback)

    @staticmethod
    async def _record_chunk(
        text: str,
        collected: list[str],
        callback: StreamCallback | None,
    ) -> None:
        if not text:
            return
        collected.append(text)
        if callback is not None:
            result = callback(text)
            if inspect.isawaitable(result):
                await result

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            await process.wait()
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=_TERMINATE_GRACE_SECONDS)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
