"""Formatting and protocol helpers shared by goal workflow orchestration."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from pydantic import ValidationError

from .contracts import (
    AgentError,
    AgentErrorCode,
    AgentResult,
    AgentResultStatus,
    Approval,
    ApprovalDecision,
    Plan,
    ReviewResult,
    ScopeExpansion,
    TaskSpec,
)
from .scheduler import SchedulerResult


def create_plan_approval(
    plan: Plan,
    *,
    decision: ApprovalDecision,
    comment: str | None = None,
    decided_by: str = "operador local",
) -> Approval:
    if decision is ApprovalDecision.PENDING:
        raise ValueError("A decisão humana precisa ser approved ou rejected.")
    return Approval(
        approval_id=f"APPROVAL-{time.monotonic_ns()}",
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        decision=decision,
        comment=comment,
        decided_by=decided_by,
        decided_at=datetime.now(timezone.utc),
    )


def resolve_plan_approval(plan: Plan, raw: object) -> tuple[str, Approval]:
    normalized = str(raw or "").strip().lower()
    if normalized in {"a", "aprovar", "approve", "approved", "y", "yes", "sim"}:
        return "approve", create_plan_approval(plan, decision=ApprovalDecision.APPROVED)
    if normalized in {"r", "revisar", "revise", "revision"}:
        return "revise", create_plan_approval(
            plan,
            decision=ApprovalDecision.REJECTED,
            comment="Plano devolvido para revisão.",
        )
    if normalized in {"c", "cancelar", "cancel", "n", "no", "não", "nao", "reject", "rejected"}:
        return "cancel", create_plan_approval(
            plan,
            decision=ApprovalDecision.REJECTED,
            comment="Plano rejeitado/cancelado pelo operador.",
        )
    raise ValueError("Escolha A para aprovar, R para revisar ou C para cancelar.")


def parse_goal_command(command_body: str, *, default_max_iterations: int = 5) -> tuple[int, str]:
    parts = command_body.split()
    max_iterations = default_max_iterations
    remaining: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--max":
            if index + 1 >= len(parts):
                raise ValueError("Use /goal --max <número> <objetivo>.")
            try:
                max_iterations = int(parts[index + 1])
            except ValueError as exc:
                raise ValueError("--max precisa ser um número inteiro.") from exc
            index += 2
            continue
        remaining.append(part)
        index += 1
    if max_iterations < 1 or max_iterations > 20:
        raise ValueError("--max precisa estar entre 1 e 20.")
    objective = " ".join(remaining).strip()
    if not objective:
        raise ValueError("Informe um objetivo após /goal.")
    return max_iterations, objective


def build_plan_prompt(
    objective: str,
    plan_reference: str,
    previous_plan: Plan | None = None,
    *,
    spec_directive: str = "",
    exploration_directive: str = "",
) -> str:
    previous = (
        "\nPlano anterior a revisar:\n" + previous_plan.model_dump_json(indent=2) + "\n"
        if previous_plan is not None
        else ""
    )
    specification = f"\nSpec Kit / quality workflow:\n{spec_directive}\n" if spec_directive else ""
    exploration = f"\nCodebase Exploration (evidence-based):\n{exploration_directive}\n" if exploration_directive else ""
    return f"""Modo /plan ativo para {plan_reference}.

Objetivo do usuário:
{objective}
{previous}{specification}{exploration}
Regras obrigatórias:
- use Deep Thinking explicitamente;
- permaneça estritamente read-only: não escreva arquivos do projeto e não execute CLI/PowerShell;
- pode investigar com list_dir/read_file/search_text;
- pode usar spawn_subagent/delegate_task somente com read_only=true e sem grant de mutação;
- quando uma decisão legítima depender do operador, chame ask_user_question com UserQuestion tipada;
- quando o plano estiver pronto, chame submit_plan com Plan tipado e plan_id={plan_reference.split('-r', 1)[0]};
- não use texto livre como protocolo de conclusão do plano.
- trate Specification como WHAT/WHY, Exploration como funcionamento atual e Plan como HOW;
- cite os EvidenceRefs da exploração no contexto/metadata do plano e não releia o repositório inteiro sem evidência de staleness.

O Plan deve registrar objetivo, contexto, premissas, perguntas respondidas, TaskSpecs, dependências,
read_set e write_set de cada task,
agentes/capabilities sugeridos, áreas/arquivos, tools/comandos previstos, riscos, rollback, impacto e validação.
Não implemente mudanças durante este fluxo."""


def format_plan_summary(plan: Plan) -> str:
    tasks = "\n".join(
        f"{index}. {task.objective} ({'read-only' if task.read_only else 'mutação'}; "
        f"deps={','.join(task.dependencies) or '-'}; reads={','.join(task.read_set) or '-'}; "
        f"writes={','.join(task.write_set) or '-'})"
        for index, task in enumerate(plan.tasks, start=1)
    )
    return (
        f"Plano {plan.reference} pronto.\n"
        f"Objetivo: {plan.objective}\n"
        f"Escopo: {', '.join(plan.scope) if plan.scope else 'não informado'}\n"
        f"Áreas: {', '.join(plan.planned_areas) if plan.planned_areas else 'nenhuma'}\n"
        f"Arquivos: {', '.join(plan.planned_files) if plan.planned_files else 'nenhum'}\n"
        f"Tools: {', '.join(plan.planned_tools) if plan.planned_tools else 'nenhuma'}\n"
        f"Comandos: {', '.join(plan.planned_commands) if plan.planned_commands else 'nenhum'}\n"
        f"Tarefas:\n{tasks or '- nenhuma'}\n"
        f"Impacto: {plan.impact or 'não informado'}\n"
        f"Riscos: {', '.join(plan.risks) if plan.risks else 'não informados'}\n"
        f"Rollback: {plan.rollback or 'não aplicável/informado'}"
    )


def scope_expansion_replanning_objective(
    objective: str,
    plan: Plan,
    expansion: ScopeExpansion,
) -> str:
    return (
        f"{objective}\n\n"
        f"A execução de {plan.reference} foi pausada por expansão material. "
        "Replaneje integralmente antes de qualquer nova mutação.\n"
        f"Expansão tipada proposta:\n{expansion.model_dump_json(indent=2)}"
    )


def format_execution_review(
    scheduler_result: SchedulerResult,
    review: ReviewResult,
    repairs_used: int,
) -> str:
    task_lines = []
    for state in scheduler_result.states:
        summary = state.result.summary if state.result is not None else (state.reason or "sem resultado")
        task_lines.append(f"- {state.task_id}: {state.status.value} — {summary}")
    return (
        "DAG concluído e aprovado pelo Reviewer.\n"
        + "\n".join(task_lines)
        + f"\nReviewer: {review.summary}\nRepairs utilizados: {repairs_used}."
    )


def aggregate_delegated_task_result(task: TaskSpec, payload: str) -> AgentResult | None:
    """Collapse one or more admitted specialist results into the parent task."""

    try:
        decoded = json.loads(payload)
        raw_results = decoded.get("results", []) if isinstance(decoded, dict) else []
        results = [AgentResult.model_validate(item["agent_result"]) for item in raw_results]
    except (KeyError, TypeError, ValidationError, json.JSONDecodeError) as exc:
        return AgentResult(
            task_id=task.task_id,
            status=AgentResultStatus.FAILED,
            summary="A delegação retornou um envelope inválido.",
            errors=[
                AgentError(
                    code=AgentErrorCode.PROTOCOL_ERROR,
                    message=f"Delegation result validation failed: {type(exc).__name__}.",
                )
            ],
        )
    if not results:
        return None
    if all(result.status is AgentResultStatus.COMPLETED for result in results):
        status = AgentResultStatus.COMPLETED
    elif any(result.status is AgentResultStatus.FAILED for result in results):
        status = AgentResultStatus.FAILED
    elif any(result.status is AgentResultStatus.BLOCKED for result in results):
        status = AgentResultStatus.BLOCKED
    elif any(result.status is AgentResultStatus.CANCELLED for result in results):
        status = AgentResultStatus.CANCELLED
    else:
        status = AgentResultStatus.INCOMPLETE
    return AgentResult(
        task_id=task.task_id,
        status=status,
        summary="\n".join(result.summary for result in results),
        findings=[finding for result in results for finding in result.findings],
        artifacts=[artifact for result in results for artifact in result.artifacts],
        validation=[validation for result in results for validation in result.validation],
        risks=[risk for result in results for risk in result.risks],
        errors=[error for result in results for error in result.errors],
    )


__all__ = [
    "aggregate_delegated_task_result",
    "build_plan_prompt",
    "create_plan_approval",
    "format_execution_review",
    "format_plan_summary",
    "parse_goal_command",
    "resolve_plan_approval",
    "scope_expansion_replanning_objective",
]
