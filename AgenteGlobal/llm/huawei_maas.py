from __future__ import annotations

import json
import os
import re
from pathlib import Path
from time import perf_counter
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx2
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .base import ModelRequest
from .capabilities import ModelCapabilities, load_capabilities_snapshot
from .contracts import ModelCapabilityError, ModelResponse, ModelStreamEvent, ToolCall
from .reasoning import DEFAULT_REASONING_POLICY, ReasoningMode


DEFAULT_BASE_URL = "https://api-ap-southeast-1.modelarts-maas.com/openai/v1"
DEFAULT_MODEL = "glm-5.2"
DEFAULT_API_KEY_FILE = Path.home() / "cred" / "AgentA.txt"
DEFAULT_CAPABILITIES_FILE = Path(__file__).with_name("model-capabilities-live.json")
API_KEY_ENV = "HUAWEI_MAAS_API_KEY"
API_KEY_FILE_ENV = "HUAWEI_MAAS_API_KEY_FILE"
MAX_API_KEY_FILE_BYTES = 10_000
_ALLOWED_HOST_SUFFIX = ".modelarts-maas.com"
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*[^\s,;]+"
)


class HuaweiMaaSConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: SecretStr = Field(exclude=True, repr=False)
    timeout_seconds: float = Field(default=90.0, ge=5.0, le=300.0)
    # Retentativas ficam na política genérica do runtime; o SDK permanece em zero.
    max_retries: int = Field(default=0, ge=0, le=5)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlparse(normalized)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https":
            raise ValueError("O endpoint MaaS deve usar HTTPS.")
        if parsed.username or parsed.password:
            raise ValueError("O endpoint MaaS não pode conter credenciais.")
        if parsed.query or parsed.fragment:
            raise ValueError("O endpoint MaaS não pode conter query string ou fragmento.")
        if host == "api.openai.com" or host.endswith(".openai.com"):
            raise ValueError("Infraestrutura OpenAI não é permitida.")
        if not host.endswith(_ALLOWED_HOST_SUFFIX):
            raise ValueError("O endpoint deve pertencer ao domínio modelarts-maas.com.")
        return normalized

    @property
    def allowed_host(self) -> str:
        host = urlparse(self.base_url).hostname
        if not host:
            raise ValueError("Endpoint MaaS sem hostname.")
        return host.lower()


class HostGuardTransport(httpx2.AsyncBaseTransport):
    """Bloqueia qualquer destino diferente do host MaaS configurado."""

    def __init__(
        self,
        allowed_host: str,
        inner: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.allowed_host = allowed_host.lower()
        self.seen_hosts: list[str] = []
        self._inner = inner or httpx2.AsyncHTTPTransport(retries=0, trust_env=True)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        host = (request.url.host or "").lower()
        self.seen_hosts.append(host)
        if host != self.allowed_host:
            raise httpx2.RequestError(
                f"Destino de rede bloqueado: {host or '(sem host)'}",
                request=request,
            )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class ProbeStructuredOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    value: int


class HuaweiMaaSAdapter:
    """Provider assíncrono restrito ao endpoint Huawei MaaS configurado."""

    def __init__(
        self,
        config: HuaweiMaaSConfig,
        *,
        transport: HostGuardTransport | None = None,
        capabilities: ModelCapabilities | None = None,
        capabilities_file: Path = DEFAULT_CAPABILITIES_FILE,
    ) -> None:
        self.config = config
        self.transport = transport or HostGuardTransport(config.allowed_host)
        self.http_client = httpx2.AsyncClient(
            transport=self.transport,
            timeout=config.timeout_seconds,
            follow_redirects=False,
            trust_env=True,
        )
        self.client = AsyncOpenAI(
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
            http_client=self.http_client,
        )
        self._capabilities = capabilities or load_capabilities_snapshot(
            capabilities_file,
            model=config.model,
            base_url=config.base_url,
        )

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    @property
    def timeout(self) -> float:
        """Compatibilidade de introspecção com a antiga factory build_client."""
        return self.config.timeout_seconds

    async def __aenter__(self) -> "HuaweiMaaSAdapter":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.close()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._validate_request_capabilities(request)
        response = await self.client.chat.completions.create(**self._request_options(request))
        if not response.choices:
            return ModelResponse(usage=self._usage_dict(getattr(response, "usage", None)))
        message = response.choices[0].message
        tool_calls = tuple(
            ToolCall(
                id=str(tool_call.id),
                name=str(tool_call.function.name),
                arguments=str(tool_call.function.arguments),
            )
            for tool_call in (message.tool_calls or [])
        )
        return ModelResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            usage=self._usage_dict(getattr(response, "usage", None)),
            finish_reason=getattr(response.choices[0], "finish_reason", None),
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """Converte SSE OpenAI-compatible em eventos seguros para o runtime."""
        self._validate_request_capabilities(request, streaming=True)
        options = self._request_options(request)
        options.update({"stream": True, "stream_options": {"include_usage": True}})
        started_at = perf_counter()
        stream = await self.client.chat.completions.create(**options)
        text_fragments: list[str] = []
        tool_fragments: dict[int, dict[str, str]] = {}
        first_token_latency_ms: int | None = None
        finish_reason: str | None = None
        usage: dict[str, Any] = {}
        async for chunk in stream:
            chunk_usage = self._usage_dict(getattr(chunk, "usage", None))
            if chunk_usage:
                usage = chunk_usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            if getattr(choice, "finish_reason", None) is not None:
                finish_reason = choice.finish_reason
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            # Do not read reasoning_content: it must never reach a user-facing event.
            content = getattr(delta, "content", None)
            if content:
                if first_token_latency_ms is None:
                    first_token_latency_ms = int((perf_counter() - started_at) * 1000)
                    yield ModelStreamEvent(
                        type="first_token", first_token_latency_ms=first_token_latency_ms
                    )
                text_fragments.append(content)
                yield ModelStreamEvent(type="text_delta", text=content)
            for tool_delta in getattr(delta, "tool_calls", None) or []:
                if first_token_latency_ms is None:
                    first_token_latency_ms = int((perf_counter() - started_at) * 1000)
                    yield ModelStreamEvent(
                        type="first_token", first_token_latency_ms=first_token_latency_ms
                    )
                index = int(getattr(tool_delta, "index", 0) or 0)
                state = tool_fragments.setdefault(
                    index, {"id": f"stream-{index}", "name": "", "arguments": ""}
                )
                is_new = state["id"] == f"stream-{index}" and not state["name"] and not state["arguments"]
                tool_id = getattr(tool_delta, "id", None)
                if tool_id:
                    state["id"] = str(tool_id)
                function = getattr(tool_delta, "function", None)
                if function is not None:
                    name = getattr(function, "name", None)
                    arguments = getattr(function, "arguments", None)
                    if name:
                        state["name"] += str(name)
                    if arguments:
                        state["arguments"] += str(arguments)
                if is_new:
                    yield ModelStreamEvent(type="tool_call_started", tool_call=self._tool_call(state))

        tool_calls = tuple(self._tool_call(tool_fragments[index]) for index in sorted(tool_fragments))
        for tool_call in tool_calls:
            yield ModelStreamEvent(type="tool_call_completed", tool_call=tool_call)
        response = ModelResponse(
            content="".join(text_fragments),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
        )
        yield ModelStreamEvent(
            type="completed",
            response=response,
            first_token_latency_ms=first_token_latency_ms,
        )

    async def complete_text(
        self,
        prompt: str,
        *,
        reasoning_effort: str | None = None,
        thinking_enabled: bool | None = None,
        max_tokens: int = 96,
    ) -> str:
        if reasoning_effort in (None, "none") and thinking_enabled in (None, False):
            mode = ReasoningMode.NORMAL
        elif reasoning_effort == "max" and thinking_enabled is False:
            mode = ReasoningMode.MAX_ONLY
        elif reasoning_effort == "max" and thinking_enabled is True:
            mode = ReasoningMode.DEEP
        else:
            raise ValueError("Combinação de reasoning não comprovada pela Fase 1.")
        response = await self.complete(
            ModelRequest(
                messages=[
                    {"role": "system", "content": "Responda de forma curta e objetiva."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                reasoning_mode=mode,
                capability_probe=True,
            )
        )
        return response.content

    async def stream_text(self, prompt: str, *, max_tokens: int = 96) -> tuple[str, int]:
        fragments: list[str] = []
        chunk_count = 0
        async for event in self.stream(
            ModelRequest(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                capability_probe=True,
            )
        ):
            if event.type == "text_delta":
                chunk_count += 1
                fragments.append(event.text)
        return "".join(fragments), chunk_count

    async def request_tool_calls(
        self,
        prompt: str,
        tools: list[dict[str, Any]],
        *,
        tool_choice: str | dict[str, Any] = "auto",
        parallel_tool_calls: bool | None = None,
    ) -> list[ToolCall]:
        response = await self.complete(
            ModelRequest(
                messages=[{"role": "user", "content": prompt}],
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                max_tokens=128,
                capability_probe=True,
            )
        )
        return list(response.tool_calls)

    async def request_json_object(self, prompt: str) -> dict[str, Any]:
        response = await self.complete(
            ModelRequest(
                messages=[
                    {"role": "system", "content": "Responda somente com um objeto JSON válido."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=96,
                response_format={"type": "json_object"},
                capability_probe=True,
            )
        )
        parsed = json.loads(self._response_content(response))
        if not isinstance(parsed, dict):
            raise ValueError("json_object não retornou um objeto.")
        return parsed

    async def request_json_schema(self, prompt: str) -> ProbeStructuredOutput:
        """Mantido somente para reexecutar o probe da capability degradada."""
        schema = ProbeStructuredOutput.model_json_schema()
        response = await self.complete(
            ModelRequest(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=96,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "probe_structured_output",
                        "strict": True,
                        "schema": schema,
                    },
                },
                capability_probe=True,
            )
        )
        return ProbeStructuredOutput.model_validate_json(self._response_content(response))

    async def request_structured_via_tool(self, prompt: str) -> ProbeStructuredOutput:
        schema = ProbeStructuredOutput.model_json_schema()
        tool_name = "emit_probe_structured_output"
        tool_calls = await self.request_tool_calls(
            prompt,
            [
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": "Emite a resposta estruturada solicitada.",
                        "parameters": schema,
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": tool_name}},
        )
        if len(tool_calls) != 1 or tool_calls[0].name != tool_name:
            raise ValueError("Fallback estruturado não retornou a função obrigatória.")
        return ProbeStructuredOutput.model_validate_json(tool_calls[0].arguments)

    @staticmethod
    def _response_content(response: ModelResponse) -> str:
        if not response.content:
            raise ValueError("MaaS retornou conteúdo vazio.")
        return response.content

    def _validate_request_capabilities(
        self,
        request: ModelRequest,
        *,
        streaming: bool = False,
    ) -> None:
        if request.capability_probe:
            return

        reasoning_capability = {
            ReasoningMode.NORMAL: "reasoning_none",
            ReasoningMode.MAX_ONLY: "reasoning_max_only",
            ReasoningMode.DEEP: "reasoning_max",
        }[request.reasoning_mode]
        required: list[str] = [reasoning_capability]
        if streaming:
            required.append("streaming")
        if request.tools is not None:
            required.append("tools")
        if request.parallel_tool_calls:
            required.append("parallel_tools")
        response_format_type = (request.response_format or {}).get("type")
        if response_format_type == "json_object":
            required.append("json_object")
        elif response_format_type == "json_schema":
            required.append("json_schema")

        unavailable = [name for name in required if not self.capabilities.supports(name)]
        if unavailable:
            raise ModelCapabilityError(
                "Capabilities não comprovadas para este modelo/endpoint: "
                + ", ".join(unavailable)
                + ". Execute e registre um novo capability probe antes de usar."
            )

    def _request_options(self, request: ModelRequest) -> dict[str, Any]:
        """Options shared by normal and SSE paths, preventing contract drift."""
        reasoning = DEFAULT_REASONING_POLICY.resolve(request.reasoning_mode)
        options: dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "reasoning_effort": reasoning.effort,
            "extra_body": {
                "thinking": {"type": "enabled" if reasoning.thinking_enabled else "disabled"}
            },
        }
        if request.tools is not None:
            options["tools"] = request.tools
        if request.tool_choice is not None:
            options["tool_choice"] = request.tool_choice
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.max_tokens is not None:
            options["max_tokens"] = request.max_tokens
        if request.timeout_seconds is not None:
            options["timeout"] = request.timeout_seconds
        if request.response_format is not None:
            options["response_format"] = request.response_format
        if request.parallel_tool_calls is not None:
            options["parallel_tool_calls"] = request.parallel_tool_calls
        return options

    @staticmethod
    def _tool_call(values: dict[str, str]) -> ToolCall:
        return ToolCall(id=values["id"], name=values["name"], arguments=values["arguments"])

    @staticmethod
    def _usage_dict(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump(exclude_none=True)
            return dict(dumped) if isinstance(dumped, dict) else {}
        attributes = getattr(value, "__dict__", None)
        if isinstance(attributes, dict):
            return {key: item for key, item in attributes.items() if item is not None}
        return {}


def load_api_key(api_key_file: Path | None = None) -> SecretStr:
    env_value = os.getenv(API_KEY_ENV, "").strip()
    if env_value:
        return SecretStr(_normalize_api_key(env_value))

    configured_file = api_key_file
    if configured_file is None:
        configured_file = Path(os.getenv(API_KEY_FILE_ENV, str(DEFAULT_API_KEY_FILE))).expanduser()
    if not configured_file.is_file():
        raise FileNotFoundError(
            f"Credencial MaaS ausente. Defina {API_KEY_ENV} ou use {configured_file}."
        )
    if configured_file.stat().st_size > MAX_API_KEY_FILE_BYTES:
        raise ValueError("Arquivo de credencial MaaS excede o limite permitido.")
    return SecretStr(_normalize_api_key(configured_file.read_text(encoding="utf-8")))


def safe_exception_summary(exc: BaseException) -> str:
    status_code = getattr(exc, "status_code", None)
    prefix = f"HTTP {status_code}: " if isinstance(status_code, int) else ""
    message = " ".join(str(exc).split())
    message = _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", message)
    message = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer [REDACTED]", message)
    return f"{exc.__class__.__name__}: {prefix}{message[:500]}"


def _normalize_api_key(raw_value: str) -> str:
    lines = [line.strip() for line in raw_value.replace("\ufeff", "").splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("A credencial MaaS deve ocupar uma única linha não vazia.")
    candidate = lines[0]
    if "=" in candidate:
        name, value = candidate.split("=", 1)
        if name.strip().upper() in {API_KEY_ENV, "API_KEY", "MAAS_API_KEY"}:
            candidate = value.strip().strip("'\"")
    if not candidate or any(char.isspace() for char in candidate):
        raise ValueError("Formato inválido de credencial MaaS.")
    return candidate
