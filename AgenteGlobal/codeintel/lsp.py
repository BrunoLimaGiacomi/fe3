"""Optional Language Server Protocol client and safe semantic fallback chain."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, cast

from pydantic import Field, field_validator
from runtime.security_text import (
    is_sensitive_key_name,
    redact_sensitive_text,
    safe_subprocess_env,
    truncate_single_line,
)

from .documents import (
    DiagnosticPublishResult,
    DiagnosticRecord,
    DiagnosticSnapshot,
    DocumentRegistry,
    DocumentState,
    DocumentSyncResult,
    LSPDocumentStateError,
)
from .index import CodeIndex
from .parsers import language_for
from .models import Confidence, EvidenceRef, RelationshipKind, StrictModel


MAX_LSP_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_LSP_HEADER_BYTES = 8 * 1024
MAX_LSP_CONFIG_BYTES = 64 * 1024
MAX_LSP_STDERR_BYTES = 64 * 1024
MAX_LSP_PENDING_REQUESTS = 128
MAX_LSP_SERVER_REQUESTS = 64
MAX_LSP_WATCHED_FILES = 256
MAX_LSP_CONFIGURATION_ITEMS = 64
MAX_LSP_PROGRESS_TOKENS = 128
DEFAULT_LSP_TIMEOUT_SECONDS = 10.0


def _validate_safe_json(value: Any, *, depth: int = 0) -> None:
    """Reject unbounded or credential-bearing provider settings.

    Provider configuration is operator supplied and is the only configuration
    sent to a language server.  It is deliberately validated here instead of
    accepting arbitrary environment/configuration data from the host process.
    """

    if depth > 12:
        raise ValueError("LSP provider settings are too deeply nested")
    if isinstance(value, dict):
        if len(value) > 256:
            raise ValueError("LSP provider settings contain too many entries")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise ValueError("LSP provider setting keys must be short strings")
            if is_sensitive_key_name(key):
                raise ValueError("LSP provider settings cannot contain credential-bearing names")
            _validate_safe_json(child, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise ValueError("LSP provider settings contain too many items")
        for child in value:
            _validate_safe_json(child, depth=depth + 1)
        return
    if isinstance(value, str):
        if len(value) > 16_384:
            raise ValueError("LSP provider setting value is too large")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise ValueError("LSP provider settings must be JSON values")


def _safe_settings(value: dict[str, Any]) -> dict[str, Any]:
    _validate_safe_json(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("LSP provider settings must be JSON serializable") from error
    if len(encoded.encode("utf-8")) > MAX_LSP_CONFIG_BYTES:
        raise ValueError("LSP provider settings exceed safe size")
    return value


class LSPError(RuntimeError):
    pass


class LSPUnavailableError(LSPError):
    pass


class LSPTimeoutError(LSPError):
    pass


class LSPCapabilityError(LSPError):
    pass


class LSPCapabilities(StrictModel):
    document_symbols: bool = False
    workspace_symbols: bool = False
    definition: bool = False
    declaration: bool = False
    references: bool = False
    implementation: bool = False
    type_definition: bool = False
    hover: bool = False
    signature_help: bool = False
    call_hierarchy: bool = False

    @staticmethod
    def _provider_enabled(value: dict[str, Any], key: str) -> bool:
        """Interpret the LSP ``boolean | options`` provider capability form.

        An empty options object is a valid advertisement and must not be
        rejected because ``bool({})`` is false.  Other values, including an
        explicit ``false`` or ``null``, are not valid support declarations.
        """

        advertised = value.get(key)
        return advertised is True or isinstance(advertised, dict)

    @classmethod
    def from_server(cls, value: dict[str, Any]) -> "LSPCapabilities":
        return cls(
            document_symbols=cls._provider_enabled(value, "documentSymbolProvider"),
            workspace_symbols=cls._provider_enabled(value, "workspaceSymbolProvider"),
            definition=cls._provider_enabled(value, "definitionProvider"),
            declaration=cls._provider_enabled(value, "declarationProvider"),
            references=cls._provider_enabled(value, "referencesProvider"),
            implementation=cls._provider_enabled(value, "implementationProvider"),
            type_definition=cls._provider_enabled(value, "typeDefinitionProvider"),
            hover=cls._provider_enabled(value, "hoverProvider"),
            signature_help=cls._provider_enabled(value, "signatureHelpProvider"),
            call_hierarchy=cls._provider_enabled(value, "callHierarchyProvider"),
        )

    def supports(self, operation: str) -> bool:
        field = operation.replace("incoming_calls", "call_hierarchy").replace(
            "outgoing_calls", "call_hierarchy"
        )
        return bool(getattr(self, field, False))


class LanguageServerProvider(StrictModel):
    language: str = Field(min_length=1, max_length=64)
    command: tuple[str, ...] = Field(min_length=1)
    initialization_options: dict[str, Any] = Field(default_factory=dict)
    configuration: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, str] = Field(default_factory=dict)

    @field_validator("initialization_options", "configuration")
    @classmethod
    def _safe_provider_settings(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _safe_settings(value)

    @field_validator("environment")
    @classmethod
    def _environment_has_no_credentials(cls, value: dict[str, str]) -> dict[str, str]:
        if any(is_sensitive_key_name(name) for name in value):
            raise ValueError("LSP provider environment cannot contain credential-bearing names")
        if len(value) > 64 or any(len(name) > 256 or len(item) > 16_384 for name, item in value.items()):
            raise ValueError("LSP provider environment is too large")
        return value


def load_lsp_providers(path: Path | str) -> tuple[LanguageServerProvider, ...]:
    """Load an explicit, bounded provider map without starting any process."""

    supplied = Path(path)
    if supplied.is_symlink():
        raise ValueError("LSP config must be a regular non-symlink file")
    candidate = supplied.resolve(strict=True)
    if not candidate.is_file():
        raise ValueError("LSP config must be a regular non-symlink file")
    if candidate.stat().st_size > MAX_LSP_CONFIG_BYTES:
        raise ValueError("LSP config exceeds safe size")
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("invalid LSP provider config") from error
    if not isinstance(payload, dict):
        raise ValueError("LSP provider config must be an object keyed by language")
    providers: list[LanguageServerProvider] = []
    for language, settings in payload.items():
        if not isinstance(language, str) or not isinstance(settings, dict):
            raise ValueError("invalid LSP provider entry")
        providers.append(LanguageServerProvider(language=language, **settings))
    return tuple(sorted(providers, key=lambda item: item.language))


class NavigationEvidence(StrictModel):
    evidence: EvidenceRef
    label: str = ""
    sources: tuple[str, ...]
    detail: str = Field(default="", max_length=4_000)


class NavigationResponse(StrictModel):
    operation: str
    evidence: tuple[NavigationEvidence, ...] = ()
    source_chain: tuple[str, ...] = ()
    fallback_used: bool = False
    latency_seconds: float = Field(ge=0.0)
    error: str = Field(default="", max_length=1_000)


class EventEmitter(Protocol):
    async def emit(self, name: str, *, source: str = "runtime", payload: dict[str, Any] | None = None) -> Any: ...


NotificationHandler = Callable[[str, Any], Awaitable[None]]


class LSPClient:
    """Bounded JSON-RPC/LSP client with an explicit subprocess lifecycle."""

    _METHODS = {
        "document_symbols": "textDocument/documentSymbol",
        "workspace_symbols": "workspace/symbol",
        "definition": "textDocument/definition",
        "declaration": "textDocument/declaration",
        "references": "textDocument/references",
        "implementation": "textDocument/implementation",
        "type_definition": "textDocument/typeDefinition",
        "hover": "textDocument/hover",
        "signature_help": "textDocument/signatureHelp",
        "call_hierarchy": "textDocument/prepareCallHierarchy",
        "incoming_calls": "callHierarchy/incomingCalls",
        "outgoing_calls": "callHierarchy/outgoingCalls",
    }
    _OPERATION_BY_METHOD = {method: operation for operation, method in _METHODS.items()}
    _CLIENT_CAPABILITIES = {
        "workspace": {
            "configuration": True,
            "workspaceFolders": True,
            "didChangeWatchedFiles": {"dynamicRegistration": True},
        },
        "window": {"workDoneProgress": True},
        "textDocument": {"synchronization": {"dynamicRegistration": False}},
    }

    def __init__(
        self,
        provider: LanguageServerProvider,
        workspace: Path | str,
        *,
        event_bus: EventEmitter | None = None,
        request_timeout: float = DEFAULT_LSP_TIMEOUT_SECONDS,
        notification_handler: NotificationHandler | None = None,
    ) -> None:
        self.provider = provider
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise NotADirectoryError(f"LSP workspace is not a directory: {self.workspace}")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self.event_bus = event_bus
        self.request_timeout = request_timeout
        self.notification_handler = notification_handler
        self.capabilities = LSPCapabilities()
        self._base_capabilities = LSPCapabilities()
        self._dynamic_registrations: dict[str, str] = {}
        self._watched_registration_ids: set[str] = set()
        self._progress_tokens: set[str] = set()
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._server_request_tasks: dict[int | str, asyncio.Task[None]] = {}
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._stderr_buffer = bytearray()
        self._stderr_bytes_seen = 0
        self._stderr_truncated = False
        self._stderr_text = ""

    @property
    def running(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    @property
    def stderr(self) -> str:
        """Bounded, redacted stderr collected from the current/last process."""

        return self._stderr_text

    @property
    def stderr_output(self) -> str:
        return self._stderr_text

    @property
    def stderr_truncated(self) -> bool:
        return self._stderr_truncated

    @property
    def registered_capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(self._dynamic_registrations.values()))

    @property
    def watched_files_registered(self) -> bool:
        return bool(self._watched_registration_ids)

    def available(self) -> bool:
        executable = self.provider.command[0]
        candidate = Path(executable)
        return (candidate.is_absolute() and candidate.is_file()) or shutil.which(executable) is not None

    async def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.event_bus is not None:
            try:
                await self.event_bus.emit(name, source="codeintel.lsp", payload=payload)
            except Exception:
                # Observability must never take down the protocol reader.
                return

    def _subprocess_environment(self) -> dict[str, str]:
        # Keep only process essentials.  In particular, do not pass the full
        # host environment even after filtering credential-looking names.
        allowed = {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "LANG",
            "LC_ALL",
        }
        minimal = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        environment = safe_subprocess_env(minimal)
        environment.update(self.provider.environment)
        return environment

    async def start(self) -> LSPCapabilities:
        async with self._lifecycle_lock:
            if self.running:
                return self.capabilities
            if self._process is not None:
                await self._stop_impl(force=True)
            if not self.available():
                raise LSPUnavailableError(f"language server unavailable: {self.provider.command[0]}")
            self._stderr_buffer.clear()
            self._stderr_bytes_seen = 0
            self._stderr_truncated = False
            self._stderr_text = ""
            self._dynamic_registrations.clear()
            self._watched_registration_ids.clear()
            self._progress_tokens.clear()
            await self._emit("lsp.server.starting", {"language": self.provider.language})
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self.provider.command,
                    cwd=self.workspace,
                    env=self._subprocess_environment(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._reader_task = asyncio.create_task(self._reader_loop(), name="lsp-reader")
                assert self._process.stderr is not None
                self._stderr_task = asyncio.create_task(self._stderr_loop(self._process.stderr), name="lsp-stderr")
                result = await self._request_raw(
                    "initialize",
                    {
                        "processId": os.getpid(),
                        "rootUri": self.workspace.as_uri(),
                        "capabilities": self._CLIENT_CAPABILITIES,
                        "initializationOptions": self.provider.initialization_options,
                        "workspaceFolders": [{"uri": self.workspace.as_uri(), "name": self.workspace.name}],
                    },
                )
                server_capabilities = result.get("capabilities", {}) if isinstance(result, dict) else {}
                self._base_capabilities = LSPCapabilities.from_server(
                    server_capabilities if isinstance(server_capabilities, dict) else {}
                )
                self.capabilities = self._base_capabilities
                await self.notify("initialized", {})
                await self._emit(
                    "lsp.server.started",
                    {"language": self.provider.language, "capabilities": self.capabilities.model_dump()},
                )
                return self.capabilities
            except BaseException:
                await self._stop_impl(force=True)
                raise

    async def _stderr_loop(self, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                self._stderr_bytes_seen += len(chunk)
                remaining = MAX_LSP_STDERR_BYTES - len(self._stderr_buffer)
                if remaining > 0:
                    self._stderr_buffer.extend(chunk[:remaining])
                if len(chunk) > max(0, remaining):
                    self._stderr_truncated = True
        except asyncio.CancelledError:
            raise
        except Exception:
            self._stderr_truncated = True
        finally:
            text = self._stderr_buffer.decode("utf-8", errors="replace")
            text = redact_sensitive_text(text)
            if len(text.encode("utf-8")) > MAX_LSP_STDERR_BYTES:
                text = text.encode("utf-8")[:MAX_LSP_STDERR_BYTES].decode("utf-8", errors="ignore")
                self._stderr_truncated = True
            self._stderr_text = text
            if self._stderr_text or self._stderr_truncated:
                await self._emit(
                    "lsp.server.stderr",
                    {
                        "language": self.provider.language,
                        "text": self._stderr_text,
                        "bytes": self._stderr_bytes_seen,
                        "truncated": self._stderr_truncated,
                    },
                )

    async def _reader_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stream = self._process.stdout
        try:
            while True:
                content_length: int | None = None
                header_bytes = 0
                while True:
                    line = await stream.readline()
                    if not line:
                        raise EOFError("language server closed stdout")
                    header_bytes += len(line)
                    if header_bytes > MAX_LSP_HEADER_BYTES:
                        raise LSPError("LSP header exceeds safe size")
                    if line in {b"\r\n", b"\n"}:
                        break
                    try:
                        decoded = line.decode("ascii", errors="strict").strip()
                    except UnicodeDecodeError:
                        continue
                    name, separator, value = decoded.partition(":")
                    if separator and name.casefold() == "content-length":
                        try:
                            content_length = int(value.strip())
                        except ValueError:
                            content_length = None
                if content_length is None or not 0 <= content_length <= MAX_LSP_MESSAGE_BYTES:
                    await self._emit(
                        "lsp.notification.invalid",
                        {"language": self.provider.language, "error": "invalid_content_length"},
                    )
                    continue
                raw = await stream.readexactly(content_length)
                try:
                    message = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await self._emit(
                        "lsp.notification.invalid",
                        {"language": self.provider.language, "error": "invalid_json"},
                    )
                    continue
                if not isinstance(message, dict):
                    continue
                method = message.get("method")
                identifier = message.get("id")
                if isinstance(method, str):
                    if "id" not in message:
                        self._schedule_notification(method, message.get("params"))
                    elif isinstance(identifier, (int, str)) and not isinstance(identifier, bool):
                        self._schedule_server_request(identifier, method, message.get("params"))
                    continue
                if isinstance(identifier, int) and not isinstance(identifier, bool) and identifier in self._pending:
                    future = self._pending.pop(identifier)
                    if future.done():
                        continue
                    if "error" in message:
                        error_value = message.get("error")
                        future.set_exception(
                            LSPError(redact_sensitive_text(truncate_single_line(str(error_value), limit=1_000)))
                        )
                    else:
                        future.set_result(message.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail_pending(f"LSP reader stopped: {type(error).__name__}")
            await self._emit(
                "lsp.server.failure",
                {
                    "language": self.provider.language,
                    "error": type(error).__name__,
                    "message": redact_sensitive_text(truncate_single_line(str(error), limit=300)),
                },
            )
            process = self._process
            if process is not None and process.returncode is None:
                await self._terminate_process(process)

    def _schedule_notification(self, method: str, params: Any) -> None:
        if len(self._server_request_tasks) >= MAX_LSP_SERVER_REQUESTS:
            return
        task = asyncio.create_task(self._handle_notification(method, params), name="lsp-notification")
        # Notification tasks are not protocol requests, but keeping them in
        # the same bounded set makes shutdown deterministic.
        identifier = f"notification:{id(task)}"
        self._server_request_tasks[identifier] = task  # type: ignore[assignment]
        task.add_done_callback(lambda _: self._server_request_tasks.pop(identifier, None))

    def _schedule_server_request(self, identifier: int | str, method: str, params: Any) -> None:
        if len(self._server_request_tasks) >= MAX_LSP_SERVER_REQUESTS:
            asyncio.create_task(self._send_error(identifier, -32000, "server request capacity exceeded"))
            return
        task = asyncio.create_task(
            self._handle_server_request(identifier, method, params), name="lsp-server-request"
        )
        self._server_request_tasks[identifier] = task
        task.add_done_callback(lambda _: self._server_request_tasks.pop(identifier, None))

    async def _handle_notification(self, method: str, params: Any) -> None:
        if method == "$/cancelRequest":
            await self._cancel_server_request(params)
        if self.notification_handler is not None:
            try:
                await self.notification_handler(method, params)
            except Exception as error:
                await self._emit(
                    "lsp.notification.invalid",
                    {
                        "language": self.provider.language,
                        "method": method,
                        "error": type(error).__name__,
                    },
                )

    async def _handle_server_request(self, identifier: int | str, method: str, params: Any) -> None:
        try:
            if method == "workspace/configuration":
                result = self._configuration(params)
            elif method == "workspace/workspaceFolders":
                result = [{"uri": self.workspace.as_uri(), "name": self.workspace.name}]
            elif method == "window/workDoneProgress/create":
                result = self._create_progress(params)
            elif method == "client/registerCapability":
                self._register_capabilities(params)
                result = None
            elif method == "client/unregisterCapability":
                self._unregister_capabilities(params)
                result = None
            else:
                await self._send_error(identifier, -32601, "method not found")
                return
            await self._send_response(identifier, result)
        except asyncio.CancelledError:
            await self._send_error(identifier, -32800, "request cancelled")
        except ValueError:
            await self._send_error(identifier, -32602, "invalid request parameters")
        except Exception as error:
            await self._send_error(identifier, -32603, type(error).__name__)

    def _configuration(self, params: Any) -> list[Any]:
        if not isinstance(params, dict) or not isinstance(params.get("items"), list):
            raise ValueError("workspace/configuration requires items")
        result: list[Any] = []
        for item in params["items"][:MAX_LSP_CONFIGURATION_ITEMS]:
            if not isinstance(item, dict):
                result.append(None)
                continue
            section = item.get("section")
            value: Any = self.provider.configuration
            if isinstance(section, str) and section:
                if section in self.provider.configuration:
                    value = self.provider.configuration[section]
                else:
                    for part in section.split("."):
                        if not isinstance(value, dict) or part not in value:
                            value = None
                            break
                        value = value[part]
            _validate_safe_json(value)
            result.append(value)
        _validate_safe_json(result)
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_LSP_CONFIG_BYTES:
            raise ValueError("workspace/configuration response exceeds safe size")
        return result

    def _create_progress(self, params: Any) -> None:
        if not isinstance(params, dict):
            raise ValueError("invalid workDoneProgress/create parameters")
        token = params.get("token")
        if not isinstance(token, (str, int)) or isinstance(token, bool):
            raise ValueError("invalid workDoneProgress token")
        if len(self._progress_tokens) < MAX_LSP_PROGRESS_TOKENS:
            self._progress_tokens.add(str(token)[:256])
        return None

    def _register_capabilities(self, params: Any) -> None:
        if not isinstance(params, dict) or not isinstance(params.get("registrations"), list):
            raise ValueError("invalid capability registration parameters")
        for item in params["registrations"][:MAX_LSP_CONFIGURATION_ITEMS]:
            if not isinstance(item, dict):
                continue
            registration_id = item.get("id")
            method = item.get("method")
            if not isinstance(registration_id, (str, int)) or isinstance(registration_id, bool):
                continue
            if not isinstance(method, str):
                continue
            registration_key = str(registration_id)[:256]
            operation = self._OPERATION_BY_METHOD.get(method)
            if operation is not None:
                self._dynamic_registrations[registration_key] = operation
            elif method == "workspace/didChangeWatchedFiles":
                self._watched_registration_ids.add(registration_key)
        self._refresh_capabilities()

    def _unregister_capabilities(self, params: Any) -> None:
        if not isinstance(params, dict):
            raise ValueError("invalid capability unregister parameters")
        entries = params.get("unregisterations", params.get("unregistrations"))
        if not isinstance(entries, list):
            raise ValueError("invalid capability unregister parameters")
        for item in entries[:MAX_LSP_CONFIGURATION_ITEMS]:
            if not isinstance(item, dict):
                continue
            registration_id = item.get("id")
            if not isinstance(registration_id, (str, int)) or isinstance(registration_id, bool):
                continue
            key = str(registration_id)[:256]
            self._dynamic_registrations.pop(key, None)
            self._watched_registration_ids.discard(key)
        self._refresh_capabilities()

    def _refresh_capabilities(self) -> None:
        values = self._base_capabilities.model_dump()
        for operation in self._dynamic_registrations.values():
            if operation in values:
                values[operation] = True
        self.capabilities = LSPCapabilities(**values)

    async def _cancel_server_request(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        identifier = params.get("id")
        if isinstance(identifier, (int, str)) and not isinstance(identifier, bool):
            task = self._server_request_tasks.get(identifier)
            if task is not None and not task.done():
                task.cancel()
            future = self._pending.get(identifier) if isinstance(identifier, int) else None
            if future is not None and not future.done():
                future.cancel()

    async def _send_response(self, identifier: int | str, result: Any) -> None:
        await self._send({"jsonrpc": "2.0", "id": identifier, "result": result})

    async def _send_error(self, identifier: int | str, code: int, message: str) -> None:
        try:
            await self._send({"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}})
        except (LSPError, OSError):
            return

    async def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise LSPUnavailableError("language server is not running")
        try:
            payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise LSPError("LSP message is not JSON serializable") from error
        if len(payload) > MAX_LSP_MESSAGE_BYTES:
            raise LSPError("LSP request exceeds safe size")
        framed = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload
        async with self._write_lock:
            try:
                process.stdin.write(framed)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionError, OSError) as error:
                raise LSPUnavailableError("language server stdin is closed") from error

    async def _request_raw(self, method: str, params: dict[str, Any]) -> Any:
        if len(self._pending) >= MAX_LSP_PENDING_REQUESTS:
            raise LSPError("too many pending LSP requests")
        self._next_id += 1
        identifier = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        started = time.perf_counter()
        await self._emit("lsp.request.started", {"language": self.provider.language, "method": method})
        try:
            await self._send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
            try:
                result = await asyncio.wait_for(future, timeout=self.request_timeout)
            except asyncio.TimeoutError as error:
                await self.cancel_request(identifier)
                raise LSPTimeoutError(f"LSP request timed out: {method}") from error
            except asyncio.CancelledError:
                await self.cancel_request(identifier)
                raise
            await self._emit(
                "lsp.request.completed",
                {
                    "language": self.provider.language,
                    "method": method,
                    "duration_seconds": time.perf_counter() - started,
                },
            )
            return result
        finally:
            self._pending.pop(identifier, None)

    async def request(self, operation: str, params: dict[str, Any]) -> Any:
        method = self._METHODS.get(operation)
        if method is None:
            raise ValueError(f"unknown LSP operation: {operation}")
        if not self.capabilities.supports(operation):
            raise LSPCapabilityError(f"LSP capability unsupported: {operation}")
        return await self._request_raw(method, params)

    async def cancel_request(self, identifier: int) -> None:
        future = self._pending.get(identifier)
        if future is not None and not future.done():
            future.cancel()
        process = self._process
        if process is not None and process.returncode is None and process.stdin is not None:
            try:
                await self._send({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": identifier}})
            except LSPError:
                return

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def notify_watched_files(self, changes: list[dict[str, Any]]) -> None:
        """Send a bounded watched-file batch, retaining only workspace URIs."""

        if not isinstance(changes, list):
            raise ValueError("watched file changes must be a list")
        bounded: list[dict[str, Any]] = []
        for item in changes[:MAX_LSP_WATCHED_FILES]:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri")
            change_type = item.get("type")
            if (
                isinstance(uri, str)
                and isinstance(change_type, int)
                and not isinstance(change_type, bool)
                and change_type in {1, 2, 3}
                and self._workspace_uri(uri)
            ):
                bounded.append({"uri": uri, "type": change_type})
        await self.notify("workspace/didChangeWatchedFiles", {"changes": bounded})

    async def did_change_watched_files(self, changes: list[dict[str, Any]]) -> None:
        await self.notify_watched_files(changes)

    def _workspace_uri(self, uri: str) -> bool:
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        if parsed.scheme.casefold() != "file":
            return False
        raw_path = unquote(parsed.path)
        if os.name == "nt" and raw_path.startswith("/") and len(raw_path) > 2 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        if parsed.netloc and parsed.netloc.casefold() not in {"", "localhost"}:
            raw_path = f"//{parsed.netloc}{raw_path}"
        try:
            Path(raw_path).resolve(strict=False).relative_to(self.workspace)
            return True
        except (OSError, ValueError):
            return False

    def set_notification_handler(self, handler: NotificationHandler) -> None:
        self.notification_handler = handler

    def _fail_pending(self, message: str) -> None:
        error = LSPUnavailableError(message)
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except (ProcessLookupError, OSError):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=min(self.request_timeout, 3.0))
        except asyncio.TimeoutError:
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                await process.wait()
            except (ProcessLookupError, OSError):
                pass

    async def _stop_impl(self, *, force: bool) -> None:
        process = self._process
        reader = self._reader_task
        stderr_task = self._stderr_task
        graceful_exit_requested = False
        if process is None:
            self._fail_pending("language server stopped")
            return
        if reader is None or reader.done():
            force = True
        if process.returncode is None and not force:
            try:
                await self._request_raw("shutdown", {})
                await self.notify("exit", {})
                graceful_exit_requested = True
            except (LSPError, asyncio.CancelledError):
                force = True
        if process.returncode is None:
            if graceful_exit_requested:
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=min(self.request_timeout, 1.0)
                    )
                except asyncio.TimeoutError:
                    await self._terminate_process(process)
            else:
                await self._terminate_process(process)
        current = asyncio.current_task()
        for task in tuple(self._server_request_tasks.values()):
            if task is not current and not task.done():
                task.cancel()
        server_tasks = tuple(task for task in self._server_request_tasks.values() if task is not current)
        if server_tasks:
            await asyncio.gather(*server_tasks, return_exceptions=True)
        if reader is not None and reader is not current and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if stderr_task is not None and stderr_task is not current and not stderr_task.done():
            await asyncio.gather(stderr_task, return_exceptions=True)
        self._fail_pending("language server stopped")
        self._process = None
        self._reader_task = None
        self._stderr_task = None
        self._dynamic_registrations.clear()
        self._watched_registration_ids.clear()
        self._progress_tokens.clear()
        self.capabilities = self._base_capabilities
        await self._emit("lsp.server.stopped", {"language": self.provider.language, "forced": force})

    async def stop(self, *, force: bool = False) -> None:
        async with self._lifecycle_lock:
            await self._stop_impl(force=force)

    async def restart(self) -> LSPCapabilities:
        await self.stop(force=True)
        return await self.start()


ClientFactory = Callable[[LanguageServerProvider, Path], LSPClient]


class LSPManager:
    def __init__(
        self,
        workspace: Path | str,
        index: CodeIndex,
        *,
        providers: tuple[LanguageServerProvider, ...] = (),
        event_bus: EventEmitter | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        self.index = index
        self.providers = {provider.language: provider for provider in providers}
        self.event_bus = event_bus
        self.client_factory = client_factory or (
            lambda provider, workspace: LSPClient(
                provider,
                workspace,
                event_bus=event_bus,
                notification_handler=lambda method, params: self._handle_notification(
                    provider.language, method, params
                ),
            )
        )
        self.clients: dict[str, LSPClient] = {}
        self.documents = DocumentRegistry(self.workspace)
        self._synced_documents: set[str] = set()

    def configured_languages(self) -> tuple[str, ...]:
        return tuple(sorted(self.providers))

    async def _client(self, language: str) -> LSPClient:
        provider = self.providers.get(language)
        if provider is None:
            raise LSPUnavailableError(f"no language server configured for {language}")
        client = self.clients.get(language)
        if client is None:
            client = self.client_factory(provider, self.workspace)
            setter = getattr(client, "set_notification_handler", None)
            if callable(setter):
                setter(lambda method, params: self._handle_notification(language, method, params))
            self.clients[language] = client
        if not client.running:
            await client.start()
        return client

    async def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.event_bus is not None:
            await self.event_bus.emit(name, source="codeintel.lsp", payload=payload)

    def _language(self, path: Path | str, language: str | None) -> str:
        if language:
            return language
        _, relative = self.documents.resolve_path(path)
        record = self.index.files.get(relative)
        return record.language if record is not None else language_for(Path(relative))

    async def _notify_document(
        self,
        state: DocumentState,
        method: str,
        params: dict[str, Any],
        *,
        action: str,
    ) -> DocumentSyncResult:
        try:
            client = await self._client(state.language)
            await client.notify(method, params)
            if method == "textDocument/didOpen":
                self._synced_documents.add(state.path)
            elif method == "textDocument/didClose":
                self._synced_documents.discard(state.path)
            await self._emit(
                "lsp.document.synced",
                {
                    "action": action,
                    "path": state.path,
                    "language": state.language,
                    "version": state.version,
                },
            )
            return DocumentSyncResult(action=action, state=state, lsp_synced=True)
        except Exception as error:
            await self._emit(
                "lsp.document.fallback",
                {
                    "action": action,
                    "path": state.path,
                    "language": state.language,
                    "version": state.version,
                    "error": type(error).__name__,
                },
            )
            return DocumentSyncResult(
                action=action,
                state=state,
                error=str(error)[:1_000],
            )

    async def open_document(
        self,
        path: Path | str,
        *,
        language: str | None = None,
        content: str | None = None,
    ) -> DocumentSyncResult:
        text = self.documents.read_content(path) if content is None else content
        selected_language = self._language(path, language)
        state, _ = self.documents.open_document(path, language=selected_language, content=text)
        return await self._notify_document(
            state,
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": state.uri,
                    "languageId": state.language,
                    "version": state.version,
                    "text": text,
                }
            },
            action="open",
        )

    async def change_document(
        self,
        path: Path | str,
        content: str,
        *,
        version: int | None = None,
    ) -> DocumentSyncResult:
        state = self.documents.change_document(path, content=content, version=version)
        if state.path not in self._synced_documents:
            # A provider may have become available after a previous fallback.
            reopened = await self._notify_document(
                state,
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": state.uri,
                        "languageId": state.language,
                        "version": state.version,
                        "text": content,
                    }
                },
                action="open",
            )
            if not reopened.lsp_synced:
                return DocumentSyncResult(action="change", state=state, error=reopened.error)
            return DocumentSyncResult(action="change", state=state, lsp_synced=True)
        return await self._notify_document(
            state,
            "textDocument/didChange",
            {
                "textDocument": {"uri": state.uri, "version": state.version},
                "contentChanges": [{"text": content}],
            },
            action="change",
        )

    async def save_document(
        self,
        path: Path | str,
        *,
        content: str | None = None,
    ) -> DocumentSyncResult:
        state = self.documents.get(path)
        if state is None or not state.open:
            raise LSPDocumentStateError("document must be open before save")
        text = self.documents.read_content(path) if content is None else content
        if self.documents.content_hash(text) != state.content_hash:
            changed = await self.change_document(path, text)
            state = changed.state
            if not changed.lsp_synced:
                return DocumentSyncResult(action="save", state=state, error=changed.error)
        if state.path not in self._synced_documents:
            opened = await self._notify_document(
                state,
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": state.uri,
                        "languageId": state.language,
                        "version": state.version,
                        "text": text,
                    }
                },
                action="open",
            )
            if not opened.lsp_synced:
                return DocumentSyncResult(action="save", state=state, error=opened.error)
        return await self._notify_document(
            state,
            "textDocument/didSave",
            {"textDocument": {"uri": state.uri}, "text": text},
            action="save",
        )

    async def close_document(self, path: Path | str) -> DocumentSyncResult:
        state = self.documents.close_document(path)
        self.documents.clear_diagnostics(path)
        if state.path not in self._synced_documents:
            return DocumentSyncResult(action="close", state=state)
        return await self._notify_document(
            state,
            "textDocument/didClose",
            {"textDocument": {"uri": state.uri}},
            action="close",
        )

    async def sync_saved_file(
        self,
        path: Path | str,
        *,
        language: str | None = None,
    ) -> DocumentSyncResult:
        content = self.documents.read_content(path)
        state = self.documents.get(path)
        if state is None or not state.open:
            opened = await self.open_document(path, language=language, content=content)
            if not opened.lsp_synced:
                return DocumentSyncResult(action="save", state=opened.state, error=opened.error)
            return await self.save_document(path, content=content)
        return await self.save_document(path, content=content)

    async def _handle_notification(self, language: str, method: str, params: Any) -> None:
        if method != "textDocument/publishDiagnostics":
            return
        await self.publish_diagnostics(language, params)

    async def notify_watched_files(self, language: str, changes: list[dict[str, Any]]) -> None:
        """Forward an explicit bounded watched-file batch to one provider."""

        client = await self._client(language)
        sender = getattr(client, "notify_watched_files", None)
        if not callable(sender):
            raise LSPUnavailableError("language server client does not support watched files")
        await cast(Callable[[list[dict[str, Any]]], Awaitable[None]], sender)(changes)

    async def did_change_watched_files(self, language: str, changes: list[dict[str, Any]]) -> None:
        await self.notify_watched_files(language, changes)

    async def publish_diagnostics(self, language: str, params: Any) -> DiagnosticPublishResult:
        """Accept a server notification through the same bounded normalization path."""

        result = self.documents.publish_diagnostics(params)
        snapshot = result.snapshot
        await self._emit(
            "lsp.diagnostics.published",
            {
                "language": language,
                "accepted": result.accepted,
                "reason": result.reason,
                "path": snapshot.path if snapshot is not None else "",
                "version": snapshot.version if snapshot is not None else None,
                "diagnostic_count": len(snapshot.diagnostics) if snapshot is not None else 0,
                "discarded": snapshot.discarded if snapshot is not None else 0,
                "truncated": snapshot.truncated if snapshot is not None else False,
            },
        )
        return result

    def document_state(self, path: Path | str) -> DocumentState | None:
        return self.documents.get(path)

    def diagnostic_snapshot(self, path: Path | str) -> DiagnosticSnapshot | None:
        return self.documents.diagnostic_snapshot(path)

    def diagnostics(
        self,
        path: Path | str | None = None,
        *,
        limit: int = 100,
    ) -> tuple[DiagnosticRecord, ...]:
        return self.documents.diagnostics(path, limit=limit)

    def _safe_uri_path(self, uri: str) -> str | None:
        resolved = self.documents.path_from_uri(uri)
        return resolved[1] if resolved is not None else None

    def _normalize_lsp(self, value: Any, *, label: str = "") -> tuple[NavigationEvidence, ...]:
        results: list[NavigationEvidence] = []

        def visit(item: Any, inherited_label: str = "") -> None:
            if isinstance(item, list):
                for child in item:
                    visit(child, inherited_label)
                return
            if not isinstance(item, dict):
                return
            candidate_location = item.get("location")
            location = candidate_location if isinstance(candidate_location, dict) else item
            uri = location.get("uri") or item.get("uri")
            range_value = location.get("range") or item.get("selectionRange") or item.get("range")
            if isinstance(uri, str) and isinstance(range_value, dict):
                path = self._safe_uri_path(uri)
                start = range_value.get("start", {})
                end = range_value.get("end", start)
                if path is not None and isinstance(start, dict) and isinstance(end, dict):
                    name = str(item.get("name") or inherited_label or label)
                    evidence = EvidenceRef(
                        path=path,
                        symbol=name,
                        start_line=int(start.get("line", 0)) + 1,
                        end_line=max(int(start.get("line", 0)), int(end.get("line", 0))) + 1,
                        source="lsp",
                        confidence=Confidence.CONFIRMED,
                    )
                    results.append(NavigationEvidence(evidence=evidence, label=name, sources=("lsp",)))
            for key in ("children", "from", "to"):
                if key in item:
                    visit(item[key], str(item.get("name") or inherited_label))

        visit(value)
        return tuple(results)

    def _static_evidence(self, operation: str, query: str, path: str) -> tuple[NavigationEvidence, ...]:
        items: list[NavigationEvidence] = []
        if operation in {"definition", "declaration", "implementation", "type_definition", "workspace_symbols"}:
            symbols = self.index.search_symbols(query) if operation == "workspace_symbols" else self.index.find_definitions(query)
            for symbol in symbols:
                evidence = EvidenceRef(
                    path=symbol.path,
                    symbol=symbol.qualified_name,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    source=symbol.source,
                    confidence=Confidence.CONFIRMED,
                )
                items.append(NavigationEvidence(evidence=evidence, label=symbol.name, sources=(symbol.source,)))
        elif operation == "document_symbols":
            record = self.index.files.get(path.replace("\\", "/"))
            for symbol in record.symbols if record else ():
                evidence = EvidenceRef(
                    path=symbol.path,
                    symbol=symbol.qualified_name,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    source=symbol.source,
                    confidence=Confidence.CONFIRMED,
                )
                items.append(NavigationEvidence(evidence=evidence, label=symbol.name, sources=(symbol.source,)))
        elif operation == "references":
            for reference in self.index.find_references(query):
                evidence = EvidenceRef(
                    path=reference.path,
                    symbol=reference.context_symbol,
                    start_line=reference.start_line,
                    end_line=reference.end_line,
                    source=reference.source,
                    confidence=Confidence.CONFIRMED,
                )
                items.append(NavigationEvidence(evidence=evidence, label=reference.name, sources=(reference.source,)))
        elif operation in {"call_hierarchy", "incoming_calls", "outgoing_calls"}:
            for relation in self.index.all_relationships():
                if relation.kind is not RelationshipKind.CALLS:
                    continue
                matches = (
                    query.casefold() in relation.target_symbol.casefold()
                    if operation == "incoming_calls"
                    else query.casefold() in relation.source_symbol.casefold()
                    if operation == "outgoing_calls"
                    else query.casefold() in (relation.source_symbol + relation.target_symbol).casefold()
                )
                if matches and relation.evidence:
                    ev = relation.evidence[0]
                    items.append(
                        NavigationEvidence(
                            evidence=ev,
                            label=f"{relation.source_symbol} -> {relation.target_symbol}",
                            sources=(relation.origin,),
                        )
                    )
        return tuple(items)

    @staticmethod
    def _merge(*collections: tuple[NavigationEvidence, ...]) -> tuple[NavigationEvidence, ...]:
        merged: dict[tuple[str, int, int, str], NavigationEvidence] = {}
        for collection in collections:
            for item in collection:
                key = (
                    item.evidence.path.casefold(),
                    item.evidence.start_line,
                    item.evidence.end_line,
                    item.label.casefold(),
                )
                prior = merged.get(key)
                if prior is None:
                    merged[key] = item
                else:
                    sources = tuple(dict.fromkeys((*prior.sources, *item.sources)))
                    best = prior if prior.evidence.source == "lsp" else item
                    merged[key] = best.model_copy(update={"sources": sources})
        return tuple(sorted(merged.values(), key=lambda item: (item.evidence.path, item.evidence.start_line, item.label)))

    async def navigate(
        self,
        operation: str,
        *,
        language: str,
        query: str = "",
        path: str = "",
        line: int = 0,
        character: int = 0,
    ) -> NavigationResponse:
        started = time.perf_counter()
        chain: list[str] = []
        errors: list[str] = []
        lsp_evidence: tuple[NavigationEvidence, ...] = ()
        try:
            client = await self._client(language)
            if operation in {"workspace_symbols"}:
                params = {"query": query}
            else:
                candidate = (self.workspace / Path(path)).resolve(strict=False)
                try:
                    candidate.relative_to(self.workspace)
                except ValueError as error:
                    raise ValueError("navigation path escapes workspace") from error
                params = {
                    "textDocument": {"uri": candidate.as_uri()},
                    "position": {"line": max(0, line), "character": max(0, character)},
                }
                if operation == "references":
                    params["context"] = {"includeDeclaration": True}
            if operation in {"incoming_calls", "outgoing_calls"}:
                # The call hierarchy endpoints do not accept the position
                # payload used by prepareCallHierarchy.  The LSP contract is
                # a two-step exchange: prepare one or more CallHierarchyItem
                # values, then pass each item under the `item` key.
                prepared = await client.request("call_hierarchy", params)
                if isinstance(prepared, dict):
                    prepared_items: list[Any] = [prepared]
                elif isinstance(prepared, list):
                    prepared_items = prepared
                else:
                    prepared_items = []

                raw_results: list[Any] = []
                for item in prepared_items:
                    if not isinstance(item, dict):
                        continue
                    result = await client.request(operation, {"item": item})
                    if result is not None:
                        raw_results.append(result)
                raw = raw_results
            else:
                raw = await client.request(operation, params)
            chain.append("lsp")
            lsp_evidence = self._normalize_lsp(raw, label=query)
        except Exception as error:  # optional provider boundary; cancellation remains BaseException
            errors.append(str(error))

        static = self._static_evidence(operation, query, path)
        if static:
            chain.append("code_index")
        merged = self._merge(lsp_evidence, static)
        if not merged and query:
            lexical = tuple(
                NavigationEvidence(evidence=item, label=query, sources=("lexical",))
                for item in self.index.lexical_search(query)
            )
            if lexical:
                chain.append("lexical")
                merged = lexical
        return NavigationResponse(
            operation=operation,
            evidence=merged,
            source_chain=tuple(chain),
            fallback_used=not bool(lsp_evidence),
            latency_seconds=max(0.0, time.perf_counter() - started),
            error="; ".join(errors)[:1_000],
        )

    async def restart(self, language: str) -> LSPCapabilities:
        client = await self._client(language)
        capabilities = await client.restart()
        self._synced_documents = {
            path
            for path in self._synced_documents
            if (state := self.documents.get(path)) is not None and state.language != language
        }
        for state in self.documents.open_documents(language):
            self.documents.clear_diagnostics(state.path)
            try:
                content = self.documents.read_content(state.path)
                await self._notify_document(
                    state,
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": state.uri,
                            "languageId": state.language,
                            "version": state.version,
                            "text": content,
                        }
                    },
                    action="reopen",
                )
            except (OSError, ValueError):
                await self._emit(
                    "lsp.document.fallback",
                    {"action": "reopen", "path": state.path, "language": language, "error": "read_failed"},
                )
        return capabilities

    async def shutdown(self) -> None:
        clients = tuple(self.clients.values())
        self.clients.clear()
        self._synced_documents.clear()
        self.documents.clear_diagnostics()
        await asyncio.gather(*(client.stop() for client in clients), return_exceptions=True)


__all__ = [
    "DEFAULT_LSP_TIMEOUT_SECONDS",
    "DiagnosticRecord",
    "DiagnosticSnapshot",
    "DocumentState",
    "DocumentSyncResult",
    "LanguageServerProvider",
    "LSPCapabilities",
    "LSPCapabilityError",
    "LSPClient",
    "LSPError",
    "LSPDocumentStateError",
    "LSPManager",
    "LSPTimeoutError",
    "LSPUnavailableError",
    "NavigationEvidence",
    "NavigationResponse",
    "load_lsp_providers",
]
