"""Admission and workflow contracts for targeted or deep exploration."""

from __future__ import annotations

import asyncio
import re
from enum import StrEnum
from typing import Awaitable, Callable

from pydantic import Field

from runtime.contracts import AgentResult, AgentResultStatus, TaskLimits, TaskSpec

from .explorer import ExplorationDepth, ExplorationReport, ExplorationRun
from .models import StrictModel


class ExplorationLevel(StrEnum):
    NONE = "none"
    TARGETED = "targeted"
    DEEP = "deep"


class ExplorationDecision(StrictModel):
    level: ExplorationLevel
    reasons: tuple[str, ...]
    security_capabilities: tuple[str, ...] = ()
    allow_multi_agent: bool = False


class ExplorationPreparation(StrictModel):
    decision: ExplorationDecision
    run: ExplorationRun | None = None
    directive: str = ""


class MultiAgentExplorationResult(StrictModel):
    tasks: tuple[TaskSpec, ...]
    results: tuple[AgentResult, ...]
    contradictions: tuple[str, ...] = ()
    overlaps: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()


_DEEP_PATTERNS = (
    "arquitet", "architecture", "refator", "cross-cutting", "transversal", "codebase desconhecido",
    "autentica", "authoriz", "autoriz", "iam", "zero trust", "trust relationship", "secret flow",
    "execution flow", "fluxo completo", "múltiplos subsistemas", "multiplos subsistemas",
)
_SIMPLE_PATTERNS = (
    re.compile(r"\b(troque|altere|mude|change|set)\b.{0,40}\b(timeout|limite|valor|nome)\b", re.I),
    re.compile(r"\b\d+\s*(?:para|to)\s*\d+\b", re.I),
)
_SECURITY_CAPABILITIES = {
    "iam": "iam.flow.discover",
    "permission": "iam.flow.discover",
    "autoriz": "security.boundary.discover",
    "autentica": "security.boundary.discover",
    "secret": "secret.flow.discover",
    "token": "secret.flow.discover",
    "trust": "trust.relationship.discover",
    "terraform": "cloud.resource.discover",
    "cloud": "cloud.resource.discover",
}


class ExplorationAdmissionController:
    def decide(
        self,
        objective: str,
        *,
        for_goal: bool = False,
        full_spec_workflow: bool = False,
        known_paths: tuple[str, ...] = (),
    ) -> ExplorationDecision:
        text = " ".join(objective.casefold().split())
        if not text:
            raise ValueError("objective cannot be empty")
        capabilities = tuple(
            dict.fromkeys(capability for marker, capability in _SECURITY_CAPABILITIES.items() if marker in text)
        )
        deep_reasons = [f"marker:{marker}" for marker in _DEEP_PATTERNS if marker in text]
        if full_spec_workflow:
            deep_reasons.append("full_spec_workflow")
        if for_goal and (len(text) > 400 or len(known_paths) > 4):
            deep_reasons.append("large_goal")
        if deep_reasons:
            return ExplorationDecision(
                level=ExplorationLevel.DEEP,
                reasons=tuple(deep_reasons),
                security_capabilities=capabilities,
                allow_multi_agent=True,
            )
        if len(text) <= 120 and any(pattern.search(text) for pattern in _SIMPLE_PATTERNS):
            return ExplorationDecision(
                level=ExplorationLevel.NONE,
                reasons=("small_local_change",),
                security_capabilities=capabilities,
            )
        return ExplorationDecision(
            level=ExplorationLevel.TARGETED,
            reasons=("related_code_context_required",),
            security_capabilities=capabilities,
        )


WorkerExecutor = Callable[[TaskSpec], Awaitable[AgentResult]]


class MultiAgentExplorationCoordinator:
    """Bounded optional fan-out using canonical TaskSpec/AgentResult contracts."""

    _ROLES = (
        ("entry-points", "Localize entry points e evidências determinísticas.", "code.entrypoint.discover", False),
        ("execution-flow", "Reconstrua callers/callees e fluxo de execução.", "code.flow.discover", True),
        ("dependencies", "Mapeie imports e dependências relevantes.", "code.dependency.discover", False),
        ("state-data-flow", "Rastreie estado e fluxo de dados suportado por evidência.", "code.state.discover", True),
        ("security-boundary", "Mapeie fronteiras de confiança e segurança aplicáveis.", "security.boundary.discover", True),
    )

    def build_tasks(
        self,
        report: ExplorationReport,
        *,
        security_capabilities: tuple[str, ...] = (),
        max_workers: int = 5,
    ) -> tuple[TaskSpec, ...]:
        if max_workers < 1:
            return ()
        tasks: list[TaskSpec] = []
        for index, (role, objective, capability, deep) in enumerate(self._ROLES[:max_workers], 1):
            required = [capability]
            if role == "security-boundary" and security_capabilities:
                required = list(dict.fromkeys((*required, *security_capabilities)))
            tasks.append(
                TaskSpec(
                    task_id=f"EXPLORE-{index:02d}-{role}",
                    type="codebase-exploration",
                    objective=f"{objective} Escopo do manager: {report.objective}",
                    scope=list(report.scope_paths),
                    read_only=True,
                    required_capabilities=required,
                    read_set=list(report.scope_paths),
                    acceptance_criteria=["Retornar AgentResult estruturado com evidence refs path:linha."],
                    limits=TaskLimits(timeout_seconds=180, max_steps=12, max_retries=1),
                    metadata={
                        "exploration_report_id": report.report_id,
                        "context_slice": "minimal",
                        "reasoning_effort": "max",
                        "deep_thinking": deep,
                    },
                )
            )
        return tuple(tasks)

    async def run(
        self,
        report: ExplorationReport,
        executor: WorkerExecutor,
        *,
        security_capabilities: tuple[str, ...] = (),
        max_workers: int = 5,
    ) -> MultiAgentExplorationResult:
        tasks = self.build_tasks(
            report,
            security_capabilities=security_capabilities,
            max_workers=max_workers,
        )
        raw = await asyncio.gather(*(executor(task) for task in tasks), return_exceptions=True)
        results: list[AgentResult] = []
        unknowns: list[str] = []
        for task, item in zip(tasks, raw, strict=True):
            if isinstance(item, BaseException):
                unknowns.append(f"{task.task_id}: worker failed: {type(item).__name__}")
                continue
            if item.task_id != task.task_id:
                unknowns.append(f"{task.task_id}: mismatched AgentResult task_id")
                continue
            if item.status is AgentResultStatus.COMPLETED and any(not finding.evidence for finding in item.findings):
                unknowns.append(f"{task.task_id}: finding sem evidence ref")
            results.append(item)
        descriptions: dict[str, set[str]] = {}
        finding_ids: dict[str, int] = {}
        for result in results:
            for finding in result.findings:
                key = finding.title.casefold()
                descriptions.setdefault(key, set()).add(finding.description.strip())
                finding_ids[finding.finding_id] = finding_ids.get(finding.finding_id, 0) + 1
        contradictions = tuple(
            f"Descrições conflitantes para: {title}" for title, values in descriptions.items() if len(values) > 1
        )
        overlaps = tuple(f"Finding repetido: {identifier}" for identifier, count in finding_ids.items() if count > 1)
        return MultiAgentExplorationResult(
            tasks=tasks,
            results=tuple(results),
            contradictions=contradictions,
            overlaps=overlaps,
            unknowns=tuple(unknowns),
        )


def build_exploration_directive(preparation: ExplorationPreparation) -> str:
    decision = preparation.decision
    if decision.level is ExplorationLevel.NONE or preparation.run is None:
        return (
            "ExplorationAdmission=NONE. A mudança foi classificada como local/simples; "
            "não invente contexto arquitetural."
        )
    run = preparation.run
    return (
        f"ExplorationAdmission={decision.level.value.upper()}. "
        f"ExplorationReport={run.report.report_id}; artifact={run.artifacts.exploration_report}; "
        f"stale={str(run.report.potentially_stale).lower()}.\n"
        "Use o relatório abaixo apenas como COMO O SISTEMA ATUAL FUNCIONA; Specification continua WHAT/WHY "
        "e Plan continua HOW TO CHANGE. Não leia o codebase inteiro novamente.\n"
        + run.report.compact_context(max_chars=32_000)
    )


__all__ = [
    "ExplorationAdmissionController",
    "ExplorationDecision",
    "ExplorationLevel",
    "ExplorationPreparation",
    "MultiAgentExplorationCoordinator",
    "MultiAgentExplorationResult",
    "build_exploration_directive",
]
