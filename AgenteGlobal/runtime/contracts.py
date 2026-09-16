"""Versioned, transport-neutral contracts for delegated agent work."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "1.0"
_CAPABILITY_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


class ContractModel(BaseModel):
    """Base for public contracts.  Unknown fields are never silently accepted."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, validate_assignment=True)


class VersionedContract(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION


class TaskLifecycleStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    CANCELLED = "cancelled"


class PlanLifecycleStatus(StrEnum):
    """Lifecycle visible for a persisted plan, not a DAG scheduler state."""

    PLANNING = "planning"
    WAITING_USER = "waiting_user"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    REQUIRES_REPLANNING = "requires_replanning"
    RUNNING = "running"
    REVIEWING = "reviewing"
    REPAIRING = "repairing"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GoalLifecycleStatus(StrEnum):
    PLANNING = "planning"
    WAITING_USER = "waiting_user"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    REVIEWING = "reviewing"
    REPAIRING = "repairing"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class UserQuestionStatus(StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    SKIPPED = "skipped"


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ScopeExpansionStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"


class AgentResultStatus(StrEnum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ReviewDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class FindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ValidationStatus(StrEnum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"


class ArtifactKind(StrEnum):
    FILE = "file"
    URL = "url"
    REPORT = "report"
    LOG = "log"
    OTHER = "other"


class AgentErrorCode(StrEnum):
    VALIDATION_ERROR = "validation_error"
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    EXECUTION_ERROR = "execution_error"
    PROTOCOL_ERROR = "protocol_error"
    UNKNOWN = "unknown"


class TaskLimits(ContractModel):
    timeout_seconds: int = Field(default=60, ge=1, le=86_400)
    max_steps: int = Field(default=10, ge=1, le=10_000)
    max_retries: int = Field(default=0, ge=0, le=10)
    token_budget: int | None = Field(default=None, ge=1, le=10_000_000)
    cost_budget_usd: float | None = Field(default=None, ge=0.0, le=1_000_000.0)


class TaskSpec(VersionedContract):
    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    type: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=20_000)
    scope: list[str] = Field(default_factory=list, max_length=200)
    read_only: bool = True
    required_capabilities: list[str] = Field(default_factory=list, max_length=64)
    # ``required_capabilities`` remains the hard contract.  Preferred
    # capabilities are only a routing hint and must never be used as a
    # substitute for a required capability.
    preferred_capabilities: list[str] = Field(default_factory=list, max_length=64)
    operation: str = Field(default="execute", min_length=1, max_length=80)
    mutation_required: bool = False
    risk: str = Field(default="low", min_length=1, max_length=32)
    dependencies: list[str] = Field(default_factory=list, max_length=256)
    read_set: list[str] = Field(default_factory=list, max_length=500)
    write_set: list[str] = Field(default_factory=list, max_length=500)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=100)
    limits: TaskLimits = Field(default_factory=TaskLimits)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "scope",
        "required_capabilities",
        "preferred_capabilities",
        "dependencies",
        "read_set",
        "write_set",
        "acceptance_criteria",
    )
    @classmethod
    def _unique_nonempty_items(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 4_000 for item in values):
            raise ValueError("List items must be non-empty and at most 4000 characters.")
        if len(set(values)) != len(values):
            raise ValueError("List items must be unique.")
        return values

    @field_validator("required_capabilities", "preferred_capabilities")
    @classmethod
    def _valid_capabilities(cls, values: list[str], info) -> list[str]:
        normalized = [value.lower() for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"{info.field_name} must not contain duplicates.")
        if any(not _CAPABILITY_RE.fullmatch(value) for value in normalized):
            raise ValueError(f"{info.field_name} contains an invalid identifier.")
        return normalized

    @field_validator("operation", "risk")
    @classmethod
    def _normalize_routing_fields(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("operation and risk must not be empty.")
        return normalized

    @property
    def requires_mutation(self) -> bool:
        """Effective mutation requirement, including the legacy read_only flag."""

        return self.mutation_required or not self.read_only

    @model_validator(mode="after")
    def _task_cannot_depend_on_itself(self) -> "TaskSpec":
        if self.task_id in self.dependencies:
            raise ValueError("A task cannot depend on itself.")
        if self.read_only and self.write_set:
            raise ValueError("A read-only task cannot declare write_set resources.")
        return self


class Finding(VersionedContract):
    finding_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    severity: FindingSeverity = FindingSeverity.INFO
    category: str = Field(default="general", min_length=1, max_length=128)
    description: str = Field(default="", max_length=20_000)
    evidence: list[str] = Field(default_factory=list, max_length=100)
    recommendation: str | None = Field(default=None, max_length=20_000)


class ArtifactRef(VersionedContract):
    uri: str = Field(min_length=1, max_length=4_096)
    kind: ArtifactKind = ArtifactKind.OTHER
    description: str | None = Field(default=None, max_length=2_000)
    media_type: str | None = Field(default=None, max_length=255)
    checksum_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")


class ValidationResult(VersionedContract):
    name: str = Field(default="runtime", min_length=1, max_length=200)
    status: ValidationStatus = ValidationStatus.NOT_RUN
    summary: str = Field(default="Not run.", min_length=1, max_length=10_000)
    checks: list[str] = Field(default_factory=list, max_length=200)
    evidence: list[str] = Field(default_factory=list, max_length=100)


class AgentError(VersionedContract):
    code: AgentErrorCode = AgentErrorCode.UNKNOWN
    message: str = Field(min_length=1, max_length=10_000)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class AgentResult(VersionedContract):
    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    status: AgentResultStatus
    summary: str = Field(min_length=1, max_length=20_000)
    findings: list[Finding] = Field(default_factory=list, max_length=1_000)
    artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=1_000)
    validation: list[ValidationResult] = Field(default_factory=list, max_length=200)
    risks: list[str] = Field(default_factory=list, max_length=200)
    errors: list[AgentError] = Field(default_factory=list, max_length=200)


class TaskState(VersionedContract):
    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    status: TaskLifecycleStatus = TaskLifecycleStatus.PENDING
    selected_agents: list[str] = Field(default_factory=list, max_length=64)
    attempt: int = Field(default=0, ge=0, le=10_000)
    result: AgentResult | None = None
    reason: str | None = Field(default=None, max_length=10_000)

    @model_validator(mode="after")
    def _result_matches_task(self) -> "TaskState":
        if self.result is not None and self.result.task_id != self.task_id:
            raise ValueError("Result task_id must match state task_id.")
        return self


class ReviewResult(VersionedContract):
    """Independent assessment of one completed scheduler pass."""

    review_id: str = Field(min_length=8, max_length=73, pattern=r"^REVIEW-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    plan_id: str = Field(min_length=9, max_length=64, pattern=r"^PLAN-[0-9]{4,}$")
    plan_revision: int = Field(ge=1, le=100_000)
    decision: ReviewDecision
    summary: str = Field(min_length=1, max_length=20_000)
    validation: list[ValidationResult] = Field(default_factory=list, max_length=200)
    findings: list[Finding] = Field(default_factory=list, max_length=1_000)
    repair_task_ids: list[str] = Field(default_factory=list, max_length=500)
    repair_instructions: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("repair_task_ids")
    @classmethod
    def _unique_repair_tasks(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 128 for value in values):
            raise ValueError("repair_task_ids must contain non-empty task identifiers.")
        if len(set(values)) != len(values):
            raise ValueError("repair_task_ids must be unique.")
        return values

    @field_validator("repair_instructions")
    @classmethod
    def _valid_repair_instructions(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 4_000 for value in values):
            raise ValueError("repair_instructions must be non-empty and at most 4000 characters.")
        if len(set(values)) != len(values):
            raise ValueError("repair_instructions must be unique.")
        return values

    @model_validator(mode="after")
    def _approved_review_needs_no_repairs(self) -> "ReviewResult":
        if self.decision is ReviewDecision.APPROVED and (self.repair_task_ids or self.repair_instructions):
            raise ValueError("An approved review cannot request repairs.")
        if self.decision is ReviewDecision.REJECTED and not self.repair_task_ids:
            raise ValueError("A rejected review must explicitly identify repair_task_ids.")
        return self


class UserQuestion(VersionedContract):
    """A concrete question that blocks planning until the operator responds."""

    question_id: str = Field(min_length=10, max_length=73, pattern=r"^QUESTION-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    prompt: str = Field(min_length=1, max_length=20_000)
    required: bool = True
    options: list[str] = Field(default_factory=list, max_length=25)
    status: UserQuestionStatus = UserQuestionStatus.PENDING
    answer: str | None = Field(default=None, max_length=20_000)

    @field_validator("options")
    @classmethod
    def _valid_options(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 2_000 for value in values):
            raise ValueError("Question options must be non-empty and at most 2000 characters.")
        if len(set(values)) != len(values):
            raise ValueError("Question options must be unique.")
        return values

    @model_validator(mode="after")
    def _answer_matches_status(self) -> "UserQuestion":
        if self.status is UserQuestionStatus.ANSWERED and not self.answer:
            raise ValueError("An answered question requires an answer.")
        if self.status is UserQuestionStatus.PENDING and self.answer is not None:
            raise ValueError("A pending question cannot already have an answer.")
        return self


class Approval(VersionedContract):
    """Human decision bound to an immutable plan revision."""

    approval_id: str = Field(min_length=10, max_length=73, pattern=r"^APPROVAL-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    plan_id: str = Field(min_length=9, max_length=64, pattern=r"^PLAN-[0-9]{4,}$")
    plan_revision: int = Field(ge=1, le=100_000)
    decision: ApprovalDecision = ApprovalDecision.PENDING
    comment: str | None = Field(default=None, max_length=20_000)
    decided_by: str | None = Field(default=None, max_length=500)
    decided_at: datetime | None = None

    @model_validator(mode="after")
    def _decision_metadata_is_consistent(self) -> "Approval":
        decided = self.decision is not ApprovalDecision.PENDING
        if decided != (self.decided_at is not None):
            raise ValueError("A final approval decision requires decided_at; pending approval must not have it.")
        return self


class ScopeExpansion(VersionedContract):
    """A material scope change that must be assessed and re-approved by the caller."""

    expansion_id: str = Field(min_length=7, max_length=70, pattern=r"^SCOPE-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    description: str = Field(min_length=1, max_length=20_000)
    reason: str = Field(min_length=1, max_length=20_000)
    added_scope: list[str] = Field(min_length=1, max_length=200)
    status: ScopeExpansionStatus = ScopeExpansionStatus.PROPOSED
    approval: Approval | None = None

    @field_validator("added_scope")
    @classmethod
    def _unique_scope(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 4_000 for value in values):
            raise ValueError("Scope entries must be non-empty and at most 4000 characters.")
        if len(set(values)) != len(values):
            raise ValueError("Scope entries must be unique.")
        return values

    @model_validator(mode="after")
    def _expansion_approval_matches_status(self) -> "ScopeExpansion":
        if self.status is ScopeExpansionStatus.PROPOSED and self.approval is not None:
            raise ValueError("A proposed scope expansion cannot include a decision.")
        if self.status in (ScopeExpansionStatus.APPROVED, ScopeExpansionStatus.APPLIED):
            if self.approval is None or self.approval.decision is not ApprovalDecision.APPROVED:
                raise ValueError("An approved or applied scope expansion requires an approved approval.")
        if self.status is ScopeExpansionStatus.REJECTED:
            if self.approval is None or self.approval.decision is not ApprovalDecision.REJECTED:
                raise ValueError("A rejected scope expansion requires a rejected approval.")
        return self


class PlanRevision(VersionedContract):
    """Audit entry produced whenever a persisted plan is revised."""

    revision: int = Field(ge=1, le=100_000)
    reason: str = Field(min_length=1, max_length=20_000)
    revised_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Plan(VersionedContract):
    """Typed, revisioned execution plan persisted by :mod:`runtime.planning`."""

    plan_id: str = Field(min_length=9, max_length=64, pattern=r"^PLAN-[0-9]{4,}$")
    objective: str = Field(min_length=1, max_length=20_000)
    context: str | None = Field(default=None, max_length=20_000)
    scope: list[str] = Field(default_factory=list, max_length=200)
    planned_areas: list[str] = Field(default_factory=list, max_length=200)
    planned_files: list[str] = Field(default_factory=list, max_length=500)
    planned_tools: list[str] = Field(default_factory=list, max_length=200)
    planned_commands: list[str] = Field(default_factory=list, max_length=200)
    impact: str | None = Field(default=None, max_length=20_000)
    rollback: str | None = Field(default=None, max_length=20_000)
    tasks: list[TaskSpec] = Field(default_factory=list, max_length=500)
    questions: list[UserQuestion] = Field(default_factory=list, max_length=100)
    approvals: list[Approval] = Field(default_factory=list, max_length=100)
    scope_expansions: list[ScopeExpansion] = Field(default_factory=list, max_length=100)
    status: PlanLifecycleStatus = PlanLifecycleStatus.PLANNING
    revision: int = Field(default=1, ge=1, le=100_000)
    assumptions: list[str] = Field(default_factory=list, max_length=200)
    risks: list[str] = Field(default_factory=list, max_length=200)
    validation_criteria: list[str] = Field(default_factory=list, max_length=200)
    revision_history: list[PlanRevision] = Field(default_factory=list, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator(
        "scope",
        "planned_areas",
        "planned_files",
        "planned_tools",
        "planned_commands",
        "assumptions",
        "risks",
        "validation_criteria",
    )
    @classmethod
    def _unique_text_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 4_000 for value in values):
            raise ValueError("Plan list entries must be non-empty and at most 4000 characters.")
        if len(set(values)) != len(values):
            raise ValueError("Plan list entries must be unique.")
        return values

    @model_validator(mode="after")
    def _plan_references_are_consistent(self) -> "Plan":
        task_ids = [task.task_id for task in self.tasks]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("Plan task_id values must be unique.")
        known_task_ids = set(task_ids)
        unknown_dependencies = {
            dependency
            for task in self.tasks
            for dependency in task.dependencies
            if dependency not in known_task_ids
        }
        if unknown_dependencies:
            raise ValueError(
                f"Plan task dependencies must reference tasks in the same plan: {sorted(unknown_dependencies)}"
            )
        dependency_map = {task.task_id: tuple(task.dependencies) for task in self.tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visited:
                return
            if task_id in visiting:
                raise ValueError("Plan task dependencies must be acyclic.")
            visiting.add(task_id)
            for dependency in dependency_map[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        question_ids = [question.question_id for question in self.questions]
        if len(set(question_ids)) != len(question_ids):
            raise ValueError("Plan question_id values must be unique.")
        expansion_ids = [expansion.expansion_id for expansion in self.scope_expansions]
        if len(set(expansion_ids)) != len(expansion_ids):
            raise ValueError("Plan scope expansion ids must be unique.")
        for approval in self.approvals:
            if approval.plan_id != self.plan_id:
                raise ValueError("Approval plan_id must match the plan.")
            if approval.plan_revision > self.revision:
                raise ValueError("Approval cannot target a future plan revision.")
        if self.status is PlanLifecycleStatus.WAITING_USER:
            if not any(question.status is UserQuestionStatus.PENDING for question in self.questions):
                raise ValueError("waiting_user requires at least one pending question.")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at.")
        return self

    @property
    def reference(self) -> str:
        """Stable UX reference: the first revision is the base PLAN-ID."""

        return self.plan_id if self.revision == 1 else f"{self.plan_id}-r{self.revision}"


class Goal(VersionedContract):
    """Goal workflow state; execution remains gated by a human plan approval."""

    goal_id: str = Field(min_length=7, max_length=69, pattern=r"^GOAL-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    objective: str = Field(min_length=1, max_length=20_000)
    plan_id: str | None = Field(default=None, min_length=9, max_length=64, pattern=r"^PLAN-[0-9]{4,}$")
    plan_revision: int | None = Field(default=None, ge=1, le=100_000)
    status: GoalLifecycleStatus = GoalLifecycleStatus.PLANNING
    approval_required: Literal[True] = True
    completion_criteria: list[str] = Field(default_factory=list, max_length=200)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("completion_criteria")
    @classmethod
    def _valid_completion_criteria(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 4_000 for value in values):
            raise ValueError("Completion criteria must be non-empty and at most 4000 characters.")
        if len(set(values)) != len(values):
            raise ValueError("Completion criteria must be unique.")
        return values

    @model_validator(mode="after")
    def _goal_plan_reference_is_consistent(self) -> "Goal":
        if (self.plan_id is None) != (self.plan_revision is None):
            raise ValueError("plan_id and plan_revision must be provided together.")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at.")
        return self
