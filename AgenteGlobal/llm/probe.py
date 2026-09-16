from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import platform
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from openai import APIStatusError
from pydantic import ValidationError

from .agents_sdk_poc import run_agent_as_tool, run_basic_agent, run_structured_agent
from .capabilities import CapabilityStatus, ModelCapabilities, SnapshotFreshness, _endpoint_identity_text
from .huawei_maas import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    HuaweiMaaSAdapter,
    HuaweiMaaSConfig,
    load_api_key,
    safe_exception_summary,
)


ProbeCall = Callable[[], Awaitable[str]]


class CapabilityDegraded(RuntimeError):
    pass


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _tools() -> list[dict[str, Any]]:
    integer_schema = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "record_alpha",
                "description": "Registra o valor alpha solicitado.",
                "parameters": integer_schema,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "record_beta",
                "description": "Registra o valor beta solicitado.",
                "parameters": integer_schema,
            },
        },
    ]


def _status_for_exception(exc: BaseException) -> CapabilityStatus:
    if isinstance(exc, APIStatusError) and exc.status_code in {400, 404, 405, 415, 422}:
        return CapabilityStatus.UNSUPPORTED
    return CapabilityStatus.ERROR


async def _record(
    capabilities: ModelCapabilities,
    name: str,
    operation: ProbeCall,
    *,
    success_detail: str,
) -> None:
    started = time.perf_counter()
    try:
        detail = await operation()
    except CapabilityDegraded as exc:
        capabilities.set_evidence(
            name,
            CapabilityStatus.DEGRADED,
            str(exc),
            int((time.perf_counter() - started) * 1000),
        )
        return
    except Exception as exc:
        capabilities.set_evidence(
            name,
            _status_for_exception(exc),
            safe_exception_summary(exc),
            int((time.perf_counter() - started) * 1000),
        )
        return
    capabilities.set_evidence(
        name,
        CapabilityStatus.SUPPORTED,
        detail or success_detail,
        int((time.perf_counter() - started) * 1000),
    )


async def run_live_probe(config: HuaweiMaaSConfig) -> ModelCapabilities:
    capabilities = ModelCapabilities(
        model=config.model,
        base_url=config.base_url,
        endpoint_identity=_endpoint_identity_text(config.base_url),
        runtime_version=f"python-{platform.python_version()}",
        freshness=SnapshotFreshness.UNKNOWN,
        sdk_versions={
            "openai": _version("openai"),
            "openai-agents": _version("openai-agents"),
            "pydantic": _version("pydantic"),
        },
    )

    async with HuaweiMaaSAdapter(config) as adapter:
        async def simple_chat() -> str:
            answer = await adapter.complete_text(
                "Responda somente com MAAS_OK.",
                reasoning_effort="none",
                thinking_enabled=False,
            )
            if not answer.strip():
                raise ValueError("Chamada simples retornou texto vazio.")
            return "Chat Completions respondeu com conteúdo não vazio."

        async def streaming() -> str:
            answer, chunks = await adapter.stream_text("Responda somente com STREAM_OK.")
            if not answer.strip() or chunks < 2:
                raise ValueError(f"Streaming sem evidência incremental: chunks={chunks}.")
            return f"Stream concluído com {chunks} chunks e conteúdo agregado."

        async def tools() -> str:
            tool_calls = await adapter.request_tool_calls(
                "Chame record_alpha com value 7. Não responda em texto.",
                [_tools()[0]],
                tool_choice={"type": "function", "function": {"name": "record_alpha"}},
            )
            if len(tool_calls) != 1 or tool_calls[0].name != "record_alpha":
                raise ValueError("Tool call nomeada não foi retornada como solicitado.")
            json.loads(tool_calls[0].arguments)
            return "Tool call nomeada retornou argumentos JSON válidos."

        async def parallel_tools() -> str:
            tool_calls = await adapter.request_tool_calls(
                (
                    "Chame record_alpha com value 7 e record_beta com value 9 "
                    "na mesma resposta. Não responda em texto."
                ),
                _tools(),
                parallel_tool_calls=True,
            )
            names = {call.name for call in tool_calls}
            if names != {"record_alpha", "record_beta"}:
                raise CapabilityDegraded(
                    f"Parâmetro aceito, mas foram observadas {len(tool_calls)} "
                    f"tool calls: {sorted(names)}."
                )
            return "Duas tool calls independentes retornaram na mesma resposta."

        async def reasoning_none() -> str:
            await adapter.complete_text(
                "Responda somente com NONE_OK.",
                reasoning_effort="none",
                thinking_enabled=False,
            )
            return "reasoning_effort=none e thinking=disabled foram aceitos."

        async def reasoning_max() -> str:
            await adapter.complete_text(
                "Calcule 17 vezes 19 e responda somente com o número.",
                reasoning_effort="max",
                thinking_enabled=True,
            )
            return "reasoning_effort=max e thinking=enabled foram aceitos."

        async def json_object() -> str:
            value = await adapter.request_json_object(
                'Retorne exatamente as chaves "status" com "JSON_OK" e "value" com 52.'
            )
            if value.get("status") != "JSON_OK" or value.get("value") != 52:
                raise ValueError("json_object retornou conteúdo diferente do solicitado.")
            return "response_format=json_object retornou objeto JSON válido."

        async def json_schema() -> str:
            try:
                value = await adapter.request_json_schema(
                    'Retorne status igual a "SCHEMA_OK" e value igual a 52.'
                )
            except ValidationError as exc:
                raise CapabilityDegraded(
                    "O endpoint aceitou json_schema, mas devolveu conteúdo fora do "
                    "schema estrito (observado JSON cercado por Markdown)."
                ) from exc
            if value.status != "SCHEMA_OK" or value.value != 52:
                raise ValueError("json_schema não respeitou os valores solicitados.")
            return "response_format=json_schema respeitou e validou o schema Pydantic."

        async def structured_fallback() -> str:
            value = await adapter.request_structured_via_tool(
                'Emita status igual a "FALLBACK_OK" e value igual a 52.'
            )
            if value.status != "FALLBACK_OK" or value.value != 52:
                raise ValueError("Fallback via function call não respeitou os valores.")
            return "Function call nomeada produziu payload validado por Pydantic."

        async def agents_sdk() -> str:
            output = await run_basic_agent(adapter)
            if not output.strip():
                raise ValueError("Runner retornou saída vazia.")
            return "Agent e Runner executaram usando OpenAIChatCompletionsModel customizado."

        async def agent_as_tool() -> str:
            output = await run_agent_as_tool(adapter)
            if not output.strip():
                raise ValueError("Manager/agent-as-tool retornou saída vazia.")
            return "Manager executou especialista local por Agent.as_tool()."

        async def agents_sdk_structured() -> str:
            try:
                output = await run_structured_agent(adapter)
            except Exception as exc:
                if exc.__class__.__name__ == "ModelBehaviorError":
                    raise CapabilityDegraded(
                        "Agent(output_type=Pydantic) falhou porque o MaaS não retornou JSON estrito."
                    ) from exc
                raise
            if output.status != "SDK_STRUCTURED_OK" or output.value != 52:
                raise ValueError("Agents SDK structured output não respeitou os valores.")
            return "Agents SDK validou output_type Pydantic via json_schema."

        await _record(capabilities, "simple_chat", simple_chat, success_detail="OK")
        await _record(capabilities, "streaming", streaming, success_detail="OK")
        await _record(capabilities, "tools", tools, success_detail="OK")
        await _record(capabilities, "parallel_tools", parallel_tools, success_detail="OK")
        await _record(capabilities, "reasoning_none", reasoning_none, success_detail="OK")
        await _record(capabilities, "reasoning_max", reasoning_max, success_detail="OK")
        await _record(capabilities, "json_object", json_object, success_detail="OK")
        await _record(capabilities, "json_schema", json_schema, success_detail="OK")
        await _record(capabilities, "structured_fallback", structured_fallback, success_detail="OK")
        await _record(capabilities, "agents_sdk", agents_sdk, success_detail="OK")
        await _record(capabilities, "agent_as_tool", agent_as_tool, success_detail="OK")
        await _record(
            capabilities,
            "agents_sdk_structured_output",
            agents_sdk_structured,
            success_detail="OK",
        )

        capabilities.observed_hosts = sorted(set(adapter.transport.seen_hosts))
        if capabilities.observed_hosts and capabilities.observed_hosts == [config.allowed_host]:
            capabilities.set_evidence(
                "no_openai_network",
                CapabilityStatus.SUPPORTED,
                (
                    "O transporte allowlist observou somente o host MaaS configurado; "
                    "tracing global e por run permaneceram desativados."
                ),
            )
        else:
            capabilities.set_evidence(
                "no_openai_network",
                CapabilityStatus.ERROR,
                f"Hosts inesperados ou nenhuma requisição observada: {capabilities.observed_hosts}.",
            )

    # A completed local probe, including per-capability failures, is a fresh
    # observation. Failed capabilities remain explicitly failed in evidence.
    capabilities.freshness = SnapshotFreshness.FRESH
    return capabilities


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capability probe isolado do Huawei MaaS.")
    parser.add_argument("--live", action="store_true", help="Executa chamadas reais ao MaaS.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--timeout", type=float, default=90.0)
    return parser


async def async_main() -> int:
    args = build_parser().parse_args()
    if not args.live:
        capabilities = ModelCapabilities(
            model=args.model,
            base_url=args.base_url,
            sdk_versions={
                "openai": _version("openai"),
                "openai-agents": _version("openai-agents"),
                "pydantic": _version("pydantic"),
            },
        )
        print(capabilities.model_dump_json(indent=2))
        return 0

    config = HuaweiMaaSConfig(
        base_url=args.base_url,
        model=args.model,
        api_key=load_api_key(args.api_key_file),
        timeout_seconds=args.timeout,
        max_retries=0,
    )
    capabilities = await run_live_probe(config)
    print(capabilities.model_dump_json(indent=2))
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
