"""Spec Kit workflow gates without creating a competing source of truth.

Existing GitHub Spec Kit artifacts are read through :mod:`runtime.spec_kit`.
The module maps official task lines to ``TaskSpec`` and performs deterministic,
read-only quality analysis.  Execution remains owned by the internal Plan and
DAG Scheduler after human approval.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .contracts import Plan, TaskSpec
from .spec_kit import SpecKitAdapter, SpecKitCategory, SpecKitDocument, SpecKitScan


MAX_WORKFLOW_CONTEXT_CHARS = 64_000
MAX_ANALYSIS_FINDINGS = 1_000
_CLARIFICATION_RE = re.compile(r"\[NEEDS\s+CLARIFICATION\s*:\s*([^\]]+)\]", re.IGNORECASE)
_AMBIGUITY_RE = re.compile(r"(?i)(?:\bTBD\b|\bTO\s*DO\b|<[^>]*(?:TODO|TBD)[^>]*>)")
_REQUIREMENT_RE = re.compile(
    r"^\s*[-*]\s*(?:\*\*)?((?:FR|REQ|US)[-_]?\d{1,5})(?:\*\*)?\s*[:\-]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_TASK_RE = re.compile(
    r"^\s*-\s*\[(?: |x|X)\]\s+(T\d{3,6})\s*(?:\[P\]\s*)?(?:(\[(?:US|FR|REQ)[-_]?\d{1,5}\])\s*)?(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_DEPENDENCY_RE = re.compile(r"\[(?:depends?|deps?)\s*:\s*([^\]]+)\]", re.IGNORECASE)
_REFERENCE_RE = re.compile(r"\b(?:US|FR|REQ)[-_]?\d{1,5}\b", re.IGNORECASE)
_PATH_RE = re.compile(r"`([^`]+(?:\.[A-Za-z0-9]{1,12}|/)[^`]*)`")
_MUTATION_RE = re.compile(
    r"(?i)\b(?:add|build|change|create|delete|edit|fix|implement|migrate|modify|remove|rename|replace|update|"
    r"adicionar|alterar|atualizar|construir|criar|corrigir|editar|excluir|implementar|migrar|remover)\b"
)
_ANALYSIS_RE = re.compile(r"(?i)\b(?:analy[sz]e|audit|inspect|review|validate|verificar|revisar|analisar)\b")


class SpecWorkflowError(RuntimeError):
    pass


class AnalyzeBlockedError(SpecWorkflowError, PermissionError):
    def __init__(self, blockers: Sequence["AnalyzeFinding"]) -> None:
        self.blockers = tuple(blockers)
        summary = "; ".join(f"{item.code}: {item.message}" for item in self.blockers[:10])
        super().__init__(f"Analyze bloqueou a execução: {summary}")


class SpecWorkflowStage(StrEnum):
    CONSTITUTION = "constitution"
    SPECIFY = "specify"
    CLARIFY = "clarify"
    CHECKLIST = "checklist"
    PLAN = "plan"
    TASKS = "tasks"
    ANALYZE = "analyze"
    HUMAN_APPROVAL = "human_approval"
    IMPLEMENT = "implement"
    REVIEW = "review"
    CONVERGE = "converge"


SPEC_WORKFLOW_STAGES = tuple(SpecWorkflowStage)


class AnalyzeSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class Clarification:
    clarification_id: str
    question: str
    source_path: str
    marker: str


@dataclass(frozen=True, slots=True)
class ClarificationResult:
    items: tuple[Clarification, ...] = ()

    @property
    def required(self) -> bool:
        return bool(self.items)


@dataclass(frozen=True, slots=True)
class RequirementChecklistItem:
    check: str
    passed: bool
    reason: str
    blocker: bool = False


@dataclass(frozen=True, slots=True)
class RequirementChecklistResult:
    items: tuple[RequirementChecklistItem, ...]

    @property
    def passed(self) -> bool:
        return all(item.passed or not item.blocker for item in self.items)

    @property
    def blockers(self) -> tuple[RequirementChecklistItem, ...]:
        return tuple(item for item in self.items if item.blocker and not item.passed)


@dataclass(frozen=True, slots=True)
class AnalyzeFinding:
    code: str
    severity: AnalyzeSeverity
    message: str
    artifact: str = ""
    references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AnalyzeResult:
    findings: tuple[AnalyzeFinding, ...]
    analyzed_paths: tuple[str, ...]
    source_digests: Mapping[str, str]
    read_only: bool = True

    def __post_init__(self) -> None:
        if len(self.findings) > MAX_ANALYSIS_FINDINGS:
            raise ValueError("Analyze excedeu o limite de findings")
        object.__setattr__(self, "source_digests", MappingProxyType(dict(self.source_digests)))

    @property
    def blockers(self) -> tuple[AnalyzeFinding, ...]:
        return tuple(item for item in self.findings if item.severity is AnalyzeSeverity.BLOCKER)

    @property
    def executable(self) -> bool:
        return not self.blockers

    def enforce(self) -> "AnalyzeResult":
        if self.blockers:
            raise AnalyzeBlockedError(self.blockers)
        return self


@dataclass(frozen=True, slots=True)
class SpecKitArtifacts:
    feature: str | None
    constitution: tuple[SpecKitDocument, ...] = ()
    specification: tuple[SpecKitDocument, ...] = ()
    checklist: tuple[SpecKitDocument, ...] = ()
    plan: tuple[SpecKitDocument, ...] = ()
    tasks: tuple[SpecKitDocument, ...] = ()

    @classmethod
    def from_scan(cls, scan: SpecKitScan, *, feature: str | None = None) -> "SpecKitArtifacts":
        available = sorted({item.feature for item in scan.documents if item.feature})
        selected = feature
        if selected is None and available:
            # Spec Kit feature directories are normally number-prefixed.  The
            # last natural name is a deterministic approximation of the active
            # feature when the caller does not provide one explicitly.
            selected = available[-1]
        selected_docs = tuple(
            item
            for item in scan.documents
            if item.feature is None or selected is None or item.feature == selected
        )

        def category(value: SpecKitCategory) -> tuple[SpecKitDocument, ...]:
            return tuple(item for item in selected_docs if item.category is value)

        return cls(
            selected,
            constitution=category(SpecKitCategory.CONSTITUTION),
            specification=category(SpecKitCategory.SPECIFICATION),
            checklist=category(SpecKitCategory.CHECKLIST),
            plan=category(SpecKitCategory.PLAN),
            tasks=category(SpecKitCategory.TASKS),
        )

    @property
    def documents(self) -> tuple[SpecKitDocument, ...]:
        return (*self.constitution, *self.specification, *self.checklist, *self.plan, *self.tasks)

    @property
    def complete_for_analysis(self) -> bool:
        return bool(self.specification and self.plan and self.tasks)


@dataclass(frozen=True, slots=True)
class SpecTaskMapping:
    tasks: tuple[TaskSpec, ...]
    source_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SpecWorkflowDecision:
    use_full_workflow: bool
    reason: str
    official_artifacts: bool = False


@dataclass(frozen=True, slots=True)
class SpecWorkflowContext:
    decision: SpecWorkflowDecision
    artifacts: SpecKitArtifacts
    clarifications: ClarificationResult
    checklist: RequirementChecklistResult
    mapped_tasks: SpecTaskMapping
    directive: str


ClarificationResolver = Any
SpecificationWriter = Any


async def emit_spec_workflow_stage(
    event_bus: Any,
    stage: SpecWorkflowStage | str,
    *,
    status: str,
    official_artifacts: bool,
    blocker_count: int = 0,
) -> None:
    if event_bus is None:
        return
    selected = SpecWorkflowStage(stage)
    await event_bus.emit(
        "spec_workflow.stage_changed",
        source="spec-kit",
        payload={
            "stage": selected.value,
            "status": status,
            "official_artifacts": official_artifacts,
            "blocker_count": max(0, int(blocker_count)),
        },
    )


def _normalize_id(value: str) -> str:
    return value.strip("[] ").upper().replace("_", "-")


def _document_digest(document: SpecKitDocument) -> str:
    return hashlib.sha256(document.content.encode("utf-8")).hexdigest()


def _combined(documents: Iterable[SpecKitDocument]) -> str:
    return "\n\n".join(item.content for item in documents)


def extract_requirements(documents: Iterable[SpecKitDocument]) -> dict[str, str]:
    requirements: dict[str, str] = {}
    for document in documents:
        for match in _REQUIREMENT_RE.finditer(document.content):
            requirements.setdefault(_normalize_id(match.group(1)), match.group(2).strip())
    return requirements


def clarification_gate(documents: Iterable[SpecKitDocument]) -> ClarificationResult:
    items: list[Clarification] = []
    for document in documents:
        for index, match in enumerate(_CLARIFICATION_RE.finditer(document.content), start=1):
            items.append(
                Clarification(
                    clarification_id=f"CLARIFY-{len(items) + 1:03d}",
                    question=match.group(1).strip(),
                    source_path=document.relative_path,
                    marker=match.group(0),
                )
            )
        remaining = _CLARIFICATION_RE.sub("", document.content)
        for match in _AMBIGUITY_RE.finditer(remaining):
            items.append(
                Clarification(
                    clarification_id=f"CLARIFY-{len(items) + 1:03d}",
                    question="Resolva os marcadores TBD/TODO materiais antes do planejamento.",
                    source_path=document.relative_path,
                    marker=match.group(0),
                )
            )
    return ClarificationResult(tuple(items))


def apply_clarification_answers(content: str, answers: Mapping[str, str]) -> str:
    """Resolve explicit markers without rewriting unrelated specification text."""

    index = 0

    def replace_marker(match: re.Match[str]) -> str:
        nonlocal index
        index += 1
        key = f"CLARIFY-{index:03d}"
        answer = str(answers.get(key, "")).strip()
        if not answer:
            return match.group(0)
        if len(answer) > 4_000:
            raise ValueError(f"Resposta {key} excede 4000 caracteres")
        question = match.group(1).strip()
        return f"{answer} <!-- {key}: {question} -->"

    return _CLARIFICATION_RE.sub(replace_marker, content)


async def resolve_clarifications(
    context: SpecWorkflowContext,
    *,
    resolver: ClarificationResolver,
    writer: SpecificationWriter,
) -> Mapping[str, str]:
    """Ask only material questions and update each existing spec once.

    The caller owns the write boundary, so Core integrations can route the
    update through Phase 8 policy, hooks and checkpoint protection.
    """

    if not context.clarifications.required:
        return MappingProxyType({})
    answers: dict[str, str] = {}
    for item in context.clarifications.items:
        supplied = resolver(item)
        answer = await supplied if inspect.isawaitable(supplied) else supplied
        normalized = str(answer or "").strip()
        if not normalized:
            raise SpecWorkflowError(f"Clarification material sem resposta: {item.clarification_id}")
        if len(normalized) > 4_000:
            raise SpecWorkflowError(f"Resposta excede 4000 caracteres: {item.clarification_id}")
        answers[item.clarification_id] = normalized

    by_path = {document.relative_path: document for document in context.artifacts.specification}
    for path, document in by_path.items():
        updated = document.content
        appendices: list[str] = []
        for item in context.clarifications.items:
            if item.source_path != path:
                continue
            answer = answers[item.clarification_id]
            if item.marker in updated:
                updated = updated.replace(
                    item.marker,
                    f"{answer} <!-- {item.clarification_id}: {item.question} -->",
                    1,
                )
            else:
                appendices.append(f"- **{item.clarification_id}** — {item.question}: {answer}")
        if appendices:
            updated = updated.rstrip() + "\n\n## Clarifications\n" + "\n".join(appendices) + "\n"
        supplied = writer(document, updated)
        if inspect.isawaitable(supplied):
            await supplied
    return MappingProxyType(answers)


def requirement_checklist(documents: Iterable[SpecKitDocument]) -> RequirementChecklistResult:
    text = _combined(documents)
    lowered = text.casefold()
    requirements = extract_requirements(documents)
    ambiguities = bool(_CLARIFICATION_RE.search(text) or _AMBIGUITY_RE.search(text))
    has_acceptance = bool(
        re.search(
            r"(?i)(acceptance criteria|success criteria|critérios? de aceite|cenários? de aceite|given\s+.+when\s+.+then)",
            text,
        )
    )
    has_why = any(term in lowered for term in ("why", "purpose", "problem", "business value", "por que", "objetivo"))
    has_constraints = any(term in lowered for term in ("constraint", "restrição", "limitation", "limite"))
    has_out_of_scope = any(term in lowered for term in ("out of scope", "fora de escopo", "não inclui"))
    return RequirementChecklistResult(
        (
            RequirementChecklistItem("what_requirements", bool(requirements), "requisitos identificáveis", True),
            RequirementChecklistItem("why", has_why, "motivação WHAT/WHY explícita"),
            RequirementChecklistItem("acceptance_criteria", has_acceptance, "critérios de aceite testáveis", True),
            RequirementChecklistItem("constraints", has_constraints, "constraints explícitas"),
            RequirementChecklistItem("out_of_scope", has_out_of_scope, "fora de escopo explícito"),
            RequirementChecklistItem("unambiguous", not ambiguities, "sem marcadores materiais", True),
        )
    )


class SpecTaskMapper:
    """Map official ``tasks.md`` checkboxes into internal executable TaskSpecs."""

    def map(self, documents: Iterable[SpecKitDocument], requirements: Mapping[str, str] | None = None) -> SpecTaskMapping:
        requirement_map = dict(requirements or {})
        mapped: list[TaskSpec] = []
        sources: list[str] = []
        for document in documents:
            for match in _TASK_RE.finditer(document.content):
                task_id = match.group(1).upper()
                description = _DEPENDENCY_RE.sub("", match.group(3)).strip()
                refs = {_normalize_id(value) for value in _REFERENCE_RE.findall(match.group(0))}
                dependencies_match = _DEPENDENCY_RE.search(match.group(3))
                dependencies = (
                    [item.strip().upper() for item in re.split(r"[,\s]+", dependencies_match.group(1)) if item.strip()]
                    if dependencies_match
                    else []
                )
                paths = tuple(dict.fromkeys(item.strip() for item in _PATH_RE.findall(description)))
                mutating = bool(_MUTATION_RE.search(description)) and not (
                    _ANALYSIS_RE.search(description) and not re.search(r"(?i)\b(?:fix|corrigir|update|alterar)\b", description)
                )
                criteria = [requirement_map[ref] for ref in sorted(refs) if ref in requirement_map]
                mapped.append(
                    TaskSpec(
                        task_id=task_id,
                        type="spec-kit-task",
                        objective=description[:20_000],
                        scope=[document.feature or document.relative_path],
                        read_only=not mutating,
                        dependencies=dependencies,
                        read_set=list(paths if not mutating else ()),
                        write_set=list(paths if mutating else ()),
                        acceptance_criteria=criteria[:100],
                        metadata={
                            "spec_kit": True,
                            "spec_kit_source": document.relative_path,
                            "spec_kit_task_id": task_id,
                            "requirement_ids": sorted(refs),
                        },
                    )
                )
                sources.append(document.relative_path)
        known = {item.task_id for item in mapped}
        unknown = sorted({dep for item in mapped for dep in item.dependencies if dep not in known})
        if unknown:
            raise SpecWorkflowError(f"tasks.md possui dependências desconhecidas: {unknown}")
        if len(known) != len(mapped):
            raise SpecWorkflowError("tasks.md possui IDs duplicados")
        return SpecTaskMapping(tuple(mapped), tuple(dict.fromkeys(sources)))


class SpecKitAnalyzeGate:
    """Deterministic, strictly read-only cross-artifact quality gate."""

    def analyze(self, artifacts: SpecKitArtifacts, *, internal_plan: Plan | None = None) -> AnalyzeResult:
        findings: list[AnalyzeFinding] = []
        documents = artifacts.documents
        digests = {item.relative_path: _document_digest(item) for item in documents}
        requirements = extract_requirements(artifacts.specification)
        checklist = requirement_checklist(artifacts.specification)
        for item in checklist.items:
            if not item.passed:
                findings.append(
                    AnalyzeFinding(
                        f"SPEC_{item.check.upper()}",
                        AnalyzeSeverity.BLOCKER if item.blocker else AnalyzeSeverity.WARNING,
                        item.reason,
                        artifacts.specification[0].relative_path if artifacts.specification else "spec.md",
                    )
                )

        for category, values, filename in (
            ("SPEC_MISSING", artifacts.specification, "spec.md"),
            ("PLAN_MISSING", artifacts.plan, "plan.md"),
            ("TASKS_MISSING", artifacts.tasks, "tasks.md"),
        ):
            if not values:
                findings.append(AnalyzeFinding(category, AnalyzeSeverity.BLOCKER, f"{filename} ausente", filename))

        mapper = SpecTaskMapper()
        try:
            mapping = mapper.map(artifacts.tasks, requirements)
        except SpecWorkflowError as error:
            findings.append(AnalyzeFinding("TASK_DEPENDENCY_INVALID", AnalyzeSeverity.BLOCKER, str(error), "tasks.md"))
            mapping = SpecTaskMapping((), ())

        referenced = {
            str(ref)
            for task in mapping.tasks
            for ref in task.metadata.get("requirement_ids", ())
        }
        for requirement_id in sorted(set(requirements) - referenced):
            findings.append(
                AnalyzeFinding(
                    "REQUIREMENT_WITHOUT_TASK",
                    AnalyzeSeverity.BLOCKER,
                    f"{requirement_id} não possui task rastreável",
                    "tasks.md",
                    (requirement_id,),
                )
            )
        for task in mapping.tasks:
            if not task.metadata.get("requirement_ids"):
                findings.append(
                    AnalyzeFinding(
                        "TASK_WITHOUT_REQUIREMENT",
                        AnalyzeSeverity.WARNING,
                        f"{task.task_id} não referencia requirement/user story",
                        str(task.metadata.get("spec_kit_source", "tasks.md")),
                        (task.task_id,),
                    )
                )

        if internal_plan is not None:
            official_by_id = {task.task_id: task for task in mapping.tasks}
            internal_by_id = {
                str(task.metadata.get("spec_kit_task_id") or task.task_id): task
                for task in internal_plan.tasks
            }
            official_ids = set(official_by_id)
            internal_ids = set(internal_by_id)
            for task_id in sorted(official_ids - internal_ids):
                findings.append(
                    AnalyzeFinding(
                        "SPEC_TASK_NOT_MAPPED",
                        AnalyzeSeverity.BLOCKER,
                        f"Task oficial {task_id} não foi mapeada para o Plan interno",
                        "tasks.md",
                        (task_id,),
                    )
                )
            for task_id in sorted(internal_ids - official_ids):
                findings.append(
                    AnalyzeFinding(
                        "INTERNAL_TASK_WITHOUT_SPEC_TASK",
                        AnalyzeSeverity.WARNING,
                        f"Task interna {task_id} não existe no tasks.md selecionado",
                        internal_plan.reference,
                        (task_id,),
                    )
                )

            def normalized_text(value: str) -> str:
                return " ".join(value.casefold().split())

            def normalized_resources(values: Sequence[str]) -> set[str]:
                return {normalized_text(value.replace("\\", "/")) for value in values}

            for task_id in sorted(official_ids & internal_ids):
                official = official_by_id[task_id]
                internal = internal_by_id[task_id]
                artifact = str(official.metadata.get("spec_kit_source", "tasks.md"))
                references = (task_id,)
                if normalized_text(official.objective) != normalized_text(internal.objective):
                    findings.append(
                        AnalyzeFinding(
                            "SPEC_TASK_OBJECTIVE_MISMATCH",
                            AnalyzeSeverity.BLOCKER,
                            f"Task interna {task_id} diverge do objetivo oficial",
                            artifact,
                            references,
                        )
                    )
                if set(official.dependencies) != set(internal.dependencies):
                    findings.append(
                        AnalyzeFinding(
                            "SPEC_TASK_DEPENDENCY_MISMATCH",
                            AnalyzeSeverity.BLOCKER,
                            f"Task interna {task_id} diverge das dependências oficiais",
                            artifact,
                            references,
                        )
                    )
                if official.read_only != internal.read_only:
                    findings.append(
                        AnalyzeFinding(
                            "SPEC_TASK_MUTATION_MISMATCH",
                            AnalyzeSeverity.BLOCKER,
                            f"Task interna {task_id} diverge do modo de mutação oficial",
                            artifact,
                            references,
                        )
                    )
                if (
                    normalized_resources(official.read_set) != normalized_resources(internal.read_set)
                    or normalized_resources(official.write_set) != normalized_resources(internal.write_set)
                ):
                    findings.append(
                        AnalyzeFinding(
                            "SPEC_TASK_RESOURCE_SCOPE_MISMATCH",
                            AnalyzeSeverity.BLOCKER,
                            f"Task interna {task_id} diverge dos recursos oficiais",
                            artifact,
                            references,
                        )
                    )
                official_criteria = {normalized_text(value) for value in official.acceptance_criteria}
                internal_criteria = {normalized_text(value) for value in internal.acceptance_criteria}
                if not official_criteria.issubset(internal_criteria):
                    findings.append(
                        AnalyzeFinding(
                            "SPEC_TASK_ACCEPTANCE_MISMATCH",
                            AnalyzeSeverity.BLOCKER,
                            f"Task interna {task_id} perdeu critérios de aceite oficiais",
                            artifact,
                            references,
                        )
                    )

        plan_text = _combined(artifacts.plan)
        spec_text = _combined(artifacts.specification)
        out_of_scope = _section_bullets(spec_text, ("out of scope", "fora de escopo"))
        for excluded in out_of_scope:
            normalized = " ".join(excluded.casefold().split())
            if len(normalized) >= 12 and normalized in " ".join(plan_text.casefold().split()):
                findings.append(
                    AnalyzeFinding(
                        "PLAN_SPEC_CONTRADICTION",
                        AnalyzeSeverity.BLOCKER,
                        f"Plan inclui item explicitamente fora de escopo: {excluded[:240]}",
                        artifacts.plan[0].relative_path if artifacts.plan else "plan.md",
                    )
                )

        implementation_text = "\n".join((plan_text, _combined(artifacts.tasks)))
        for document in artifacts.constitution:
            for token in re.findall(r"(?i)(?:MUST\s+NOT|NEVER|NÃO\s+DEVE|NUNCA)[^\n`]*`([^`]+)`", document.content):
                if token.casefold() in implementation_text.casefold():
                    findings.append(
                        AnalyzeFinding(
                            "CONSTITUTION_VIOLATION",
                            AnalyzeSeverity.BLOCKER,
                            f"Artifact de implementação referencia item proibido pela Constitution: {token}",
                            document.relative_path,
                            (token,),
                        )
                    )

        return AnalyzeResult(
            tuple(findings[:MAX_ANALYSIS_FINDINGS]),
            tuple(item.relative_path for item in documents),
            digests,
        )

    def analyze_internal_plan(self, plan: Plan) -> AnalyzeResult:
        findings: list[AnalyzeFinding] = []
        if not plan.tasks:
            findings.append(AnalyzeFinding("TASKS_MISSING", AnalyzeSeverity.BLOCKER, "Plan não possui TaskSpecs"))
        if not plan.validation_criteria:
            findings.append(
                AnalyzeFinding("ACCEPTANCE_CRITERIA_MISSING", AnalyzeSeverity.BLOCKER, "Plan não possui validation_criteria")
            )
        for task in plan.tasks:
            if not task.acceptance_criteria:
                findings.append(
                    AnalyzeFinding(
                        "TASK_ACCEPTANCE_MISSING",
                        AnalyzeSeverity.WARNING,
                        f"{task.task_id} não possui acceptance_criteria",
                        plan.reference,
                        (task.task_id,),
                    )
                )
        digest = hashlib.sha256(plan.model_dump_json().encode("utf-8")).hexdigest()
        return AnalyzeResult(tuple(findings), (plan.reference,), {plan.reference: digest})


def _section_bullets(text: str, headings: Sequence[str]) -> tuple[str, ...]:
    active = False
    values: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("# ").casefold()
            active = any(value in title for value in headings)
            continue
        if active and re.match(r"^[-*]\s+", stripped):
            values.append(re.sub(r"^[-*]\s+", "", stripped).strip())
    return tuple(values)


def should_use_full_spec_workflow(objective: str, scan: SpecKitScan | None = None) -> SpecWorkflowDecision:
    if scan is not None:
        artifacts = SpecKitArtifacts.from_scan(scan)
        if scan.has_specify and artifacts.complete_for_analysis:
            return SpecWorkflowDecision(True, "workspace possui artifacts oficiais completos do Spec Kit", True)
        if artifacts.complete_for_analysis:
            return SpecWorkflowDecision(True, "workspace possui spec.md, plan.md e tasks.md reutilizáveis", True)
    normalized = objective.casefold()
    signals = sum(
        term in normalized
        for term in (
            "arquitetura",
            "architecture",
            "migração",
            "migration",
            "múltipl",
            "multi-agent",
            "dependên",
            "security",
            "segurança",
            "workflow",
            "integração",
            "integration",
        )
    )
    large = len(objective) >= 800 or (len(objective) >= 320 and signals >= 3)
    return SpecWorkflowDecision(
        large,
        "objetivo grande/complexo requer gates completos" if large else "tarefa simples permanece no fluxo Plan/DAG",
        False,
    )


def build_spec_workflow_context(
    objective: str,
    scan: SpecKitScan | None = None,
    *,
    feature: str | None = None,
) -> SpecWorkflowContext:
    decision = should_use_full_spec_workflow(objective, scan)
    artifacts = SpecKitArtifacts.from_scan(scan, feature=feature) if scan is not None else SpecKitArtifacts(feature)
    clarifications = clarification_gate(artifacts.specification)
    checklist = requirement_checklist(artifacts.specification) if artifacts.specification else RequirementChecklistResult(())
    requirements = extract_requirements(artifacts.specification)
    mapped = SpecTaskMapper().map(artifacts.tasks, requirements) if artifacts.tasks else SpecTaskMapping((), ())
    directive = ""
    if decision.use_full_workflow:
        source = (
            "Reutilize os artifacts oficiais abaixo; não crie spec/plan/tasks paralelos."
            if decision.official_artifacts
            else "O workspace não possui artifacts oficiais completos; preserve a separação Spec Kit no Plan interno."
        )
        excerpts: list[str] = []
        remaining = MAX_WORKFLOW_CONTEXT_CHARS
        for document in artifacts.documents:
            header = f"\n<{document.category.value} path=\"{document.relative_path}\">\n"
            footer = f"\n</{document.category.value}>"
            piece = header + document.content + footer
            if remaining <= 0:
                break
            excerpts.append(piece[:remaining])
            remaining -= len(excerpts[-1])
        directive = (
            "Fluxo obrigatório para este /goal: constitution → specify → clarify → checklist → plan → tasks → "
            "analyze → human approval → implement → review → converge.\n"
            "Specification contém somente WHAT/WHY; Plan contém HOW; TaskSpecs são as únicas unidades canônicas "
            "do DAG após aprovação humana. Analyze é read-only e blockers impedem execução.\n"
            f"{source}\n" + "".join(excerpts)
        )
    return SpecWorkflowContext(decision, artifacts, clarifications, checklist, mapped, directive)


class SpecWorkflowCoordinator:
    """Own the optional large-goal quality gates outside the Core module."""

    def __init__(
        self,
        workspace: Path | str,
        objective: str,
        *,
        inspect_workspace: bool = True,
        event_bus: Any = None,
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        self.objective = objective
        self.inspect_workspace = inspect_workspace
        self.event_bus = event_bus
        scan = SpecKitAdapter(self.workspace).scan() if inspect_workspace else None
        self.context = build_spec_workflow_context(objective, scan)

    async def emit(self, stage: SpecWorkflowStage, status: str, *, blocker_count: int = 0) -> None:
        await emit_spec_workflow_stage(
            self.event_bus,
            stage,
            status=status,
            official_artifacts=self.context.decision.official_artifacts,
            blocker_count=blocker_count,
        )

    async def prepare(
        self,
        *,
        resolver: ClarificationResolver = None,
        writer: SpecificationWriter = None,
    ) -> SpecWorkflowContext:
        if not self.context.decision.use_full_workflow:
            return self.context
        for stage in (SpecWorkflowStage.CONSTITUTION, SpecWorkflowStage.SPECIFY):
            await self.emit(
                stage,
                "reused" if self.context.decision.official_artifacts else "internal",
            )
        await self.emit(
            SpecWorkflowStage.CLARIFY,
            "required" if self.context.clarifications.required else "completed",
        )
        if self.context.clarifications.required:
            if resolver is None or writer is None:
                raise SpecWorkflowError("Clarification requer resolver e writer governado")
            await resolve_clarifications(self.context, resolver=resolver, writer=writer)
            scan = SpecKitAdapter(self.workspace).scan() if self.inspect_workspace else None
            self.context = build_spec_workflow_context(self.objective, scan)
            await self.emit(SpecWorkflowStage.CLARIFY, "completed")
        await self.emit(
            SpecWorkflowStage.CHECKLIST,
            "completed" if self.context.checklist.passed else "blocked",
            blocker_count=len(self.context.checklist.blockers),
        )
        await self.emit(SpecWorkflowStage.PLAN, "running")
        return self.context

    async def analyze(self, plan: Plan) -> AnalyzeResult | None:
        if not self.context.decision.use_full_workflow:
            return None
        await self.emit(
            SpecWorkflowStage.TASKS,
            "mapped" if self.context.mapped_tasks.tasks else "internal",
        )
        result = (
            SpecKitAnalyzeGate().analyze(self.context.artifacts, internal_plan=plan)
            if self.context.decision.official_artifacts
            else SpecKitAnalyzeGate().analyze_internal_plan(plan)
        )
        if self.event_bus is not None:
            await self.event_bus.emit(
                "analyze.completed" if result.executable else "analyze.blocked",
                source="spec-kit",
                payload={
                    "plan_id": plan.reference,
                    "status": "passed" if result.executable else "blocked",
                    "blocker_count": len(result.blockers),
                    "finding_count": len(result.findings),
                    "read_only": result.read_only,
                },
            )
        await self.emit(
            SpecWorkflowStage.ANALYZE,
            "completed" if result.executable else "blocked",
            blocker_count=len(result.blockers),
        )
        return result.enforce()


__all__ = [
    "AnalyzeBlockedError",
    "AnalyzeFinding",
    "AnalyzeResult",
    "AnalyzeSeverity",
    "Clarification",
    "ClarificationResult",
    "RequirementChecklistItem",
    "RequirementChecklistResult",
    "SPEC_WORKFLOW_STAGES",
    "SpecKitAnalyzeGate",
    "SpecKitArtifacts",
    "SpecTaskMapper",
    "SpecTaskMapping",
    "SpecWorkflowContext",
    "SpecWorkflowCoordinator",
    "SpecWorkflowDecision",
    "SpecWorkflowError",
    "SpecWorkflowStage",
    "apply_clarification_answers",
    "build_spec_workflow_context",
    "clarification_gate",
    "extract_requirements",
    "emit_spec_workflow_stage",
    "requirement_checklist",
    "resolve_clarifications",
    "should_use_full_spec_workflow",
]
