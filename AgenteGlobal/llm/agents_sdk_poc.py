from __future__ import annotations

import os
from typing import Any

from openai.types.shared import Reasoning

from .huawei_maas import HuaweiMaaSAdapter, ProbeStructuredOutput


def configure_agents_sdk() -> None:
    """Desliga tracing/export antes de construir qualquer Agent."""

    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    from agents import set_tracing_disabled

    set_tracing_disabled(True)


def build_agents_model(adapter: HuaweiMaaSAdapter) -> Any:
    configure_agents_sdk()
    from agents import OpenAIChatCompletionsModel

    return OpenAIChatCompletionsModel(
        model=adapter.config.model,
        openai_client=adapter.client,
        strict_feature_validation=False,
    )


def _run_config() -> Any:
    from agents import RunConfig

    return RunConfig(
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        workflow_name="Huawei MaaS Phase 1 POC",
    )


def _model_settings(adapter: HuaweiMaaSAdapter, reasoning_effort: str) -> Any:
    from agents import ModelSettings

    return ModelSettings(
        max_tokens=128,
        reasoning=Reasoning(effort=reasoning_effort),
        extra_body={
            "thinking": {
                "type": "disabled" if reasoning_effort == "none" else "enabled",
            }
        },
        timeout=adapter.config.timeout_seconds,
    )


async def run_basic_agent(adapter: HuaweiMaaSAdapter) -> str:
    from agents import Agent, Runner

    agent = Agent(
        name="Huawei MaaS SDK probe",
        instructions="Responda somente com SDK_OK.",
        model=build_agents_model(adapter),
        model_settings=_model_settings(adapter, "none"),
    )
    result = await Runner.run(
        agent,
        input="Confirme que o runner está operacional.",
        max_turns=2,
        run_config=_run_config(),
    )
    return str(result.final_output or "")


async def run_agent_as_tool(adapter: HuaweiMaaSAdapter) -> str:
    from agents import Agent, Runner

    model = build_agents_model(adapter)
    child = Agent(
        name="Especialista local",
        instructions="Responda somente com CHILD_OK.",
        model=model,
        model_settings=_model_settings(adapter, "max"),
    )
    child_tool = child.as_tool(
        tool_name="consultar_especialista_local",
        tool_description="Consulta obrigatoriamente o especialista local.",
        max_turns=2,
        run_config=_run_config(),
    )
    manager = Agent(
        name="Gerente local",
        instructions=(
            "Use obrigatoriamente consultar_especialista_local uma vez e então "
            "responda somente com MANAGER_OK."
        ),
        model=model,
        model_settings=_model_settings(adapter, "none"),
        tools=[child_tool],
    )
    result = await Runner.run(
        manager,
        input="Consulte o especialista para concluir a verificação.",
        max_turns=4,
        run_config=_run_config(),
    )
    return str(result.final_output or "")


async def run_structured_agent(adapter: HuaweiMaaSAdapter) -> ProbeStructuredOutput:
    from agents import Agent, Runner

    agent = Agent(
        name="Structured output probe",
        instructions="Retorne status igual a SDK_STRUCTURED_OK e value igual a 52.",
        model=build_agents_model(adapter),
        model_settings=_model_settings(adapter, "none"),
        output_type=ProbeStructuredOutput,
    )
    result = await Runner.run(
        agent,
        input="Produza a saída estruturada solicitada.",
        max_turns=2,
        run_config=_run_config(),
    )
    if not isinstance(result.final_output, ProbeStructuredOutput):
        raise TypeError("Agents SDK não retornou o tipo Pydantic esperado.")
    return result.final_output
