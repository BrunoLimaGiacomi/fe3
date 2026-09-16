"""Bounded post-review convergence with explicit repair TaskSpecs."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .contracts import (
    AgentResult,
    AgentResultStatus,
    Plan,
    ReviewDecision,
    ReviewResult,
    TaskLifecycleStatus,
    TaskSpec,
    ValidationStatus,
)
from .events import EventBus
from .scheduler import DAGScheduler, SchedulerResult, TaskExecutor
from .spec_workflow import AnalyzeResult


MAX_CONVERGENCE_PASSES = 10


class ConvergenceOutcome(StrEnum):
    CONVERGED = "CONVERGED"
    GAPS_FOUND = "GAPS_FOUND"
    FAILED = "FAILED"


class EvidenceType(StrEnum):
    """Evidence classes ordered from deterministic to human/model inference."""

    TEST = "test"
    BUILD = "build"
    LINT = "lint"
    TYPECHECK = "typecheck"
    LSP_DIAGNOSTIC = "lsp_diagnostic"
    DETERMINISTIC_VALIDATOR = "deterministic_validator"
    DETERMINISTIC = "deterministic_validator"
    VALIDATOR = "deterministic_validator"
    ARTIFACT = "artifact"
    TOOL_RESULT = "tool_result"
    STRUCTURED_VALIDATION = "structured_validation"
    STRUCTURED = "structured_validation"
    VALIDATION = "structured_validation"
    STRUCTURED_REVIEWER = "structured_reviewer"
    REVIEWER = "structured_reviewer"
    MANUAL = "manual"
    MANUAL_VALIDATION = "manual"
    LEXICAL = "lexical"


class CriterionStatus(StrEnum):
    SATISFIED = "SATISFIED"
    UNSATISFIED = "UNSATISFIED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


# Names used by callers that prefer a result-oriented vocabulary.
CriterionResultStatus = CriterionStatus
CriterionOutcome = CriterionStatus


class ConvergenceError(RuntimeError):
    pass


class ConvergenceLimitExceeded(ConvergenceError):
    def __init__(self, result: "ConvergenceResult", max_passes: int) -> None:
        self.result = result
        self.max_passes = max_passes
        super().__init__(f"Convergence não estabilizou após {max_passes} passe(s): {result.summary}")


@dataclass(frozen=True, slots=True)
class ConvergenceGap:
    gap_id: str
    category: str
    description: str
    target_task_id: str | None = None
    acceptance_criterion: str | None = None
    repairable: bool = True


@dataclass(frozen=True, slots=True)
class ConvergenceEvidence:
    changed_files: tuple[str, ...] = ()
    test_results: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    diff_summary: str = ""
    code_summary: str = ""
    validation_evidence: tuple["ValidationEvidence", ...] = ()
    criterion_evidence: tuple["CriterionEvidence", ...] = ()

    @property
    def validations(self) -> tuple["ValidationEvidence", ...]:
        return self.validation_evidence

    @property
    def criteria(self) -> tuple["CriterionEvidence", ...]:
        return self.criterion_evidence


@dataclass(frozen=True, slots=True, init=False)
class ValidationEvidence:
    """One bounded, typed validation observation.

    ``type`` is accepted as a compatibility alias for ``evidence_type``;
    untyped text remains lexical context and cannot satisfy a criterion.
    """

    evidence_type: EvidenceType
    status: Any
    summary: str
    source: str
    criterion: str | None
    details: tuple[str, ...]

    def __init__(
        self,
        evidence_type: EvidenceType | str | None = None,
        status: Any = ValidationStatus.NOT_RUN,
        summary: str = "",
        source: str = "",
        criterion: str | None = None,
        details: Sequence[str] = (),
        *,
        type: EvidenceType | str | None = None,
    ) -> None:
        selected = evidence_type if evidence_type is not None else type
        if selected is None:
            raise ValueError("ValidationEvidence exige evidence_type")
        try:
            normalized_type = selected if isinstance(selected, EvidenceType) else EvidenceType(str(selected))
        except ValueError as error:
            raise ValueError(f"Tipo de evidência inválido: {selected!r}") from error
        object.__setattr__(self, "evidence_type", normalized_type)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "summary", str(summary)[:20_000])
        object.__setattr__(self, "source", str(source)[:2_000])
        object.__setattr__(self, "criterion", str(criterion)[:20_000] if criterion is not None else None)
        object.__setattr__(self, "details", tuple(str(item)[:4_000] for item in tuple(details)[:100]))

    @property
    def type(self) -> EvidenceType:
        return self.evidence_type


@dataclass(frozen=True, slots=True, init=False)
class CriterionEvidence:
    criterion: str
    evidence: tuple[ValidationEvidence, ...]

    def __init__(
        self,
        criterion: str,
        evidence: Sequence[ValidationEvidence] = (),
        *,
        validations: Sequence[ValidationEvidence] | None = None,
    ) -> None:
        selected = tuple(validations if validations is not None else evidence)
        if not criterion or not str(criterion).strip():
            raise ValueError("criterion não pode ser vazio")
        if any(not isinstance(item, ValidationEvidence) for item in selected):
            raise TypeError("evidence deve conter ValidationEvidence")
        object.__setattr__(self, "criterion", str(criterion)[:20_000])
        object.__setattr__(self, "evidence", selected[:200])

    @property
    def validations(self) -> tuple[ValidationEvidence, ...]:
        return self.evidence


@dataclass(frozen=True, slots=True)
class CriterionResult:
    criterion: str
    status: CriterionStatus
    evidence: tuple[ValidationEvidence, ...] = ()
    reason: str = ""

    @property
    def result(self) -> CriterionStatus:
        return self.status

    @property
    def satisfied(self) -> bool:
        return self.status is CriterionStatus.SATISFIED


@dataclass(frozen=True, slots=True)
class ConvergenceResult:
    outcome: ConvergenceOutcome
    pass_number: int
    gaps: tuple[ConvergenceGap, ...]
    repair_tasks: tuple[TaskSpec, ...]
    summary: str
    criterion_results: tuple[CriterionResult, ...] = ()

    @property
    def criteria(self) -> tuple[CriterionResult, ...]:
        return self.criterion_results

    @property
    def converged(self) -> bool:
        return self.outcome is ConvergenceOutcome.CONVERGED


@dataclass(frozen=True, slots=True)
class ConvergenceRun:
    final: ConvergenceResult
    history: tuple[ConvergenceResult, ...]
    scheduler_results: tuple[SchedulerResult, ...]
    reviews: tuple[ReviewResult, ...]


ReviewCallback = Callable[[Plan, SchedulerResult, int], ReviewResult | Awaitable[ReviewResult]]
EvidenceProvider = Callable[[int], ConvergenceEvidence | Awaitable[ConvergenceEvidence]]


_DETERMINISTIC_EVIDENCE = frozenset(
    {
        EvidenceType.TEST,
        EvidenceType.BUILD,
        EvidenceType.LINT,
        EvidenceType.TYPECHECK,
        EvidenceType.LSP_DIAGNOSTIC,
        EvidenceType.DETERMINISTIC_VALIDATOR,
        EvidenceType.ARTIFACT,
        EvidenceType.TOOL_RESULT,
    }
)
_STRUCTURED_EVIDENCE = frozenset({EvidenceType.STRUCTURED_VALIDATION, EvidenceType.MANUAL})
_REVIEWER_EVIDENCE = frozenset({EvidenceType.STRUCTURED_REVIEWER})


def _validation_status(value: Any) -> ValidationStatus | None:
    if isinstance(value, ValidationStatus):
        return value
    try:
        return ValidationStatus(str(value).casefold())
    except ValueError:
        return None


def _criterion_matches(candidate: str | None, criterion: str, *, allow_unbound: bool = True) -> bool:
    if candidate is None:
        return allow_unbound
    return " ".join(candidate.casefold().split()) == " ".join(criterion.casefold().split())


def _criterion_evidence(
    criterion: str,
    results: Sequence[AgentResult],
    review: ReviewResult | None,
    evidence: ConvergenceEvidence,
) -> tuple[ValidationEvidence, ...]:
    selected: list[ValidationEvidence] = []
    for group in evidence.criterion_evidence:
        if _criterion_matches(group.criterion, criterion, allow_unbound=False):
            selected.extend(group.evidence)
    selected.extend(
        item
        for item in evidence.validation_evidence
        if _criterion_matches(item.criterion, criterion)
    )
    # A plainly reported failed test is a negative signal, never a positive
    # lexical proof.  Passing text remains context-only unless typed below.
    for test_result in evidence.test_results:
        if re.search(r"(?i)\b(?:fail(?:ed|ure)?|error|erro|falh(?:ou|a))\b", test_result):
            selected.append(
                ValidationEvidence(
                    EvidenceType.TEST,
                    ValidationStatus.FAILED,
                    test_result,
                    source="test_results",
                    criterion=criterion,
                )
            )
    for result in results:
        for validation in result.validation:
            if _criterion_matches(validation.name, criterion, allow_unbound=False):
                selected.append(
                    ValidationEvidence(
                        EvidenceType.STRUCTURED_VALIDATION,
                        validation.status,
                        validation.summary,
                        source=validation.name,
                        criterion=validation.name,
                        details=(*validation.checks, *validation.evidence),
                    )
                )
    if review is not None:
        for validation in review.validation:
            if _criterion_matches(validation.name, criterion, allow_unbound=False):
                selected.append(
                    ValidationEvidence(
                        EvidenceType.STRUCTURED_REVIEWER,
                        validation.status,
                        validation.summary,
                        source=validation.name,
                        criterion=validation.name,
                        details=(*validation.checks, *validation.evidence),
                    )
                )
    return tuple(selected)


def _evaluate_criterion(
    criterion: str,
    results: Sequence[AgentResult],
    review: ReviewResult | None,
    evidence: ConvergenceEvidence,
) -> CriterionResult:
    selected = _criterion_evidence(criterion, results, review, evidence)
    if not selected:
        lexical_context = bool(evidence.test_results or evidence.diff_summary or evidence.code_summary)
        reason = (
            "somente correspondência lexical; validação tipada ausente"
            if lexical_context
            else "nenhuma evidência tipada para o critério"
        )
        return CriterionResult(criterion, CriterionStatus.INSUFFICIENT_EVIDENCE, (), reason)

    ranked = [
        item
        for item in selected
        if item.evidence_type in _DETERMINISTIC_EVIDENCE
        or item.evidence_type in _STRUCTURED_EVIDENCE
        or item.evidence_type in _REVIEWER_EVIDENCE
    ]
    if not ranked:
        return CriterionResult(
            criterion,
            CriterionStatus.INSUFFICIENT_EVIDENCE,
            selected,
            "somente correspondência lexical; validação tipada ausente",
        )
    ranks = {
        **{item: 3 for item in _DETERMINISTIC_EVIDENCE},
        **{item: 2 for item in _STRUCTURED_EVIDENCE},
        **{item: 1 for item in _REVIEWER_EVIDENCE},
    }
    highest = max(ranks[item.evidence_type] for item in ranked)
    applicable = [item for item in ranked if ranks[item.evidence_type] == highest]
    statuses = {_validation_status(item.status) for item in applicable}
    if ValidationStatus.FAILED in statuses:
        reason = "evidência determinística falhou" if highest == 3 else "evidência de validação falhou"
        if ValidationStatus.PASSED in statuses:
            reason = "evidências conflitantes no nível de precedência mais alto"
        return CriterionResult(criterion, CriterionStatus.UNSATISFIED, selected, reason)
    if ValidationStatus.PASSED in statuses:
        return CriterionResult(criterion, CriterionStatus.SATISFIED, selected, "evidência tipada passou")
    return CriterionResult(criterion, CriterionStatus.INSUFFICIENT_EVIDENCE, selected, "evidência não conclusiva")


def _criterion_satisfied(criterion: str, results: Sequence[AgentResult], evidence: ConvergenceEvidence) -> bool:
    """Compatibility helper; lexical overlap alone is deliberately ignored."""

    return _evaluate_criterion(criterion, results, None, evidence).satisfied


class ConvergenceEngine:
    """Compare approved intent with execution evidence and bound repairs."""

    def __init__(self, *, max_passes: int = 2) -> None:
        if not 0 <= max_passes <= MAX_CONVERGENCE_PASSES:
            raise ValueError(f"max_passes precisa estar entre 0 e {MAX_CONVERGENCE_PASSES}")
        self.max_passes = max_passes

    def evaluate(
        self,
        *,
        plan: Plan,
        scheduler_result: SchedulerResult,
        review: ReviewResult,
        pass_number: int = 0,
        analyze: AnalyzeResult | None = None,
        evidence: ConvergenceEvidence | None = None,
    ) -> ConvergenceResult:
        if pass_number < 0 or pass_number > MAX_CONVERGENCE_PASSES:
            raise ValueError("pass_number fora do limite")
        selected_evidence = evidence or ConvergenceEvidence()
        gaps: list[ConvergenceGap] = []

        if analyze is not None and analyze.blockers:
            for index, finding in enumerate(analyze.blockers, start=1):
                gaps.append(
                    ConvergenceGap(
                        f"GAP-ANALYZE-{index:03d}",
                        "analyze_blocker",
                        finding.message,
                        repairable=False,
                    )
                )

        states = scheduler_result.states
        results = tuple(state.result for state in states if state.result is not None)
        for state in states:
            if state.status is not TaskLifecycleStatus.COMPLETED:
                gaps.append(
                    ConvergenceGap(
                        f"GAP-TASK-{len(gaps) + 1:03d}",
                        "task_incomplete",
                        f"{state.task_id} terminou em {state.status.value}",
                        target_task_id=state.task_id,
                    )
                )
        if review.decision is ReviewDecision.REJECTED:
            for task_id in review.repair_task_ids:
                gaps.append(
                    ConvergenceGap(
                        f"GAP-REVIEW-{len(gaps) + 1:03d}",
                        "review_rejected",
                        review.summary,
                        target_task_id=task_id,
                    )
                )
        criterion_results = tuple(
            _evaluate_criterion(criterion, results, review, selected_evidence)
            for criterion in plan.validation_criteria
        )
        for criterion_result in criterion_results:
            if not criterion_result.satisfied:
                gaps.append(
                    ConvergenceGap(
                        f"GAP-ACCEPTANCE-{len(gaps) + 1:03d}",
                        "acceptance_unsatisfied"
                        if criterion_result.status is CriterionStatus.UNSATISFIED
                        else "acceptance_unverified",
                        f"{criterion_result.reason}: {criterion_result.criterion}",
                        acceptance_criterion=criterion_result.criterion,
                    )
                )

        if selected_evidence.changed_files and plan.planned_files:
            changed = {item.casefold().replace("\\", "/") for item in selected_evidence.changed_files}
            for planned in plan.planned_files:
                normalized = planned.casefold().replace("\\", "/")
                if not any(item == normalized or item.endswith("/" + normalized) for item in changed):
                    gaps.append(
                        ConvergenceGap(
                            f"GAP-FILE-{len(gaps) + 1:03d}",
                            "planned_file_missing",
                            f"Arquivo planejado sem evidência no diff: {planned}",
                        )
                    )

        nonrepairable = any(not item.repairable for item in gaps)
        if not gaps:
            outcome = ConvergenceOutcome.CONVERGED
            summary = "Specification, Plan, TaskSpecs, execução e Review convergiram."
        elif nonrepairable or pass_number >= self.max_passes:
            outcome = ConvergenceOutcome.FAILED
            summary = f"Convergence terminou com {len(gaps)} gap(s) e sem novo passe seguro."
        else:
            outcome = ConvergenceOutcome.GAPS_FOUND
            summary = f"Convergence encontrou {len(gaps)} gap(s) reparáveis."
        repairs = self.build_repair_tasks(plan, gaps, pass_number + 1) if outcome is ConvergenceOutcome.GAPS_FOUND else ()
        return ConvergenceResult(
            outcome,
            pass_number,
            tuple(gaps),
            repairs,
            summary,
            criterion_results,
        )

    @staticmethod
    def build_repair_tasks(plan: Plan, gaps: Sequence[ConvergenceGap], pass_number: int) -> tuple[TaskSpec, ...]:
        originals = {task.task_id: task for task in plan.tasks}
        default = next((task for task in reversed(plan.tasks) if not task.read_only), plan.tasks[-1] if plan.tasks else None)
        tasks: list[TaskSpec] = []
        for index, gap in enumerate(gaps, start=1):
            if not gap.repairable:
                continue
            source = originals.get(gap.target_task_id or "", default)
            if source is None:
                continue
            task_id = f"CONV-P{pass_number:02d}-{index:03d}"
            criterion = [gap.acceptance_criterion] if gap.acceptance_criterion else list(source.acceptance_criteria)
            tasks.append(
                TaskSpec(
                    task_id=task_id,
                    type="convergence-repair",
                    objective=(
                        f"Resolva somente o gap {gap.gap_id} dentro do escopo já aprovado: {gap.description}"
                    )[:20_000],
                    scope=list(source.scope),
                    read_only=source.read_only,
                    required_capabilities=list(source.required_capabilities),
                    dependencies=[],
                    read_set=list(source.read_set),
                    write_set=list(source.write_set),
                    acceptance_criteria=criterion[:100],
                    limits=source.limits,
                    metadata={
                        **source.metadata,
                        "convergence": True,
                        "convergence_pass": pass_number,
                        "convergence_gap_id": gap.gap_id,
                        "original_task_id": source.task_id,
                    },
                )
            )
        return tuple(tasks)

    async def run(
        self,
        *,
        plan: Plan,
        initial_scheduler_result: SchedulerResult,
        initial_review: ReviewResult,
        task_executor: TaskExecutor,
        reviewer: ReviewCallback,
        analyze: AnalyzeResult | None = None,
        evidence_provider: EvidenceProvider | None = None,
        repair_task_registrar: Callable[[Sequence[TaskSpec]], Any] | None = None,
        recovered_results: Mapping[str, AgentResult] | None = None,
        event_bus: EventBus | None = None,
        max_concurrency: int = 1,
    ) -> ConvergenceRun:
        history: list[ConvergenceResult] = []
        scheduler_results: list[SchedulerResult] = [initial_scheduler_result]
        reviews: list[ReviewResult] = [initial_review]
        combined_states = list(initial_scheduler_result.states)
        current_review = initial_review

        for pass_number in range(self.max_passes + 1):
            supplied_evidence = (
                await _resolve(evidence_provider(pass_number)) if evidence_provider is not None else ConvergenceEvidence()
            )
            combined = SchedulerResult(tuple(combined_states))
            result = self.evaluate(
                plan=plan,
                scheduler_result=combined,
                review=current_review,
                pass_number=pass_number,
                analyze=analyze,
                evidence=supplied_evidence,
            )
            history.append(result)
            if event_bus is not None:
                await event_bus.emit(
                    "convergence.passed" if result.converged else "convergence.gaps_found",
                    source="convergence",
                    payload={
                        "plan_id": plan.reference,
                        "status": result.outcome.value,
                        "convergence_pass": pass_number,
                        "gap_count": len(result.gaps),
                    },
                )
            if result.converged:
                return ConvergenceRun(result, tuple(history), tuple(scheduler_results), tuple(reviews))
            if result.outcome is ConvergenceOutcome.FAILED:
                if event_bus is not None:
                    await event_bus.emit(
                        "convergence.failed",
                        source="convergence",
                        payload={
                            "plan_id": plan.reference,
                            "status": result.outcome.value,
                            "convergence_pass": pass_number,
                            "gap_count": len(result.gaps),
                        },
                    )
                raise ConvergenceLimitExceeded(result, self.max_passes)
            if not result.repair_tasks:
                failed = ConvergenceResult(
                    ConvergenceOutcome.FAILED,
                    pass_number,
                    result.gaps,
                    (),
                    "Gaps encontrados, mas nenhuma repair task segura pôde ser criada.",
                )
                raise ConvergenceLimitExceeded(failed, self.max_passes)

            if event_bus is not None:
                await event_bus.emit(
                    "convergence.repair_started",
                    source="convergence",
                    payload={
                        "plan_id": plan.reference,
                        "status": "running",
                        "convergence_pass": pass_number + 1,
                        "task_count": len(result.repair_tasks),
                    },
                )
            if repair_task_registrar is not None:
                await _resolve(repair_task_registrar(result.repair_tasks))
            repair_ids = {task.task_id for task in result.repair_tasks}
            seeded_repairs = {
                task_id: recovered
                for task_id, recovered in dict(recovered_results or {}).items()
                if task_id in repair_ids
            }
            scheduler = DAGScheduler(
                result.repair_tasks,
                task_executor,
                max_concurrency=max_concurrency,
                event_bus=event_bus,
                source=f"convergence:{plan.reference}:pass:{pass_number + 1}",
                initial_results=seeded_repairs,
            )
            repair_result = await scheduler.run()
            scheduler_results.append(repair_result)
            repaired_originals = {
                str(task.metadata.get("original_task_id"))
                for task, state in zip(result.repair_tasks, repair_result.states)
                if state.status is TaskLifecycleStatus.COMPLETED and task.metadata.get("original_task_id")
            }
            if repaired_originals:
                combined_states = [state for state in combined_states if state.task_id not in repaired_originals]
            combined_states.extend(repair_result.states)
            transient_plan = plan.model_copy(update={"tasks": [*plan.tasks, *result.repair_tasks]})
            current_review = await _resolve(reviewer(transient_plan, repair_result, pass_number + 1))
            reviews.append(current_review)

        raise AssertionError("bounded convergence loop terminou sem resultado")


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


__all__ = [
    "ConvergenceEngine",
    "ConvergenceError",
    "ConvergenceEvidence",
    "ConvergenceGap",
    "ConvergenceLimitExceeded",
    "ConvergenceOutcome",
    "ConvergenceResult",
    "ConvergenceRun",
    "CriterionEvidence",
    "CriterionOutcome",
    "CriterionResult",
    "CriterionResultStatus",
    "CriterionStatus",
    "EvidenceType",
    "MAX_CONVERGENCE_PASSES",
    "ValidationEvidence",
]
