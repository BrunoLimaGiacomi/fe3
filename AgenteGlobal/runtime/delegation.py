"""Deterministic admission decisions; deliberately not a scheduler or DAG."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .agent_registry import AgentRegistry, CapabilityMatcher
from .contracts import TaskSpec


class DelegationAction(StrEnum):
    EXECUTE_LOCALLY = "execute_locally"
    DELEGATE = "delegate"


class DelegationReason(StrEnum):
    EXPLICIT_CAPABILITIES = "explicit_capabilities"
    CAPABILITY_MATCH = "capability_match"
    PREFERRED_CAPABILITIES = "preferred_capabilities"
    MULTI_DOMAIN_REVIEW = "multi_domain_review"
    TRIVIAL_SHORT_TASK = "trivial_short_task"
    NO_COMPATIBLE_SPECIALIST = "no_compatible_specialist"
    MUTATION_PERMISSION_REQUIRED = "mutation_permission_required"
    NO_SPECIALIZATION_REQUIRED = "no_specialization_required"
    SUBAGENT_LIMIT_REACHED = "subagent_limit_reached"
    DEPENDENCIES_REQUIRE_SCHEDULER = "dependencies_require_scheduler"


class DelegationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    action: DelegationAction
    reasons: tuple[DelegationReason, ...]
    required_capabilities: tuple[str, ...] = ()
    preferred_capabilities: tuple[str, ...] = ()
    operation: str = "execute"
    mutation_required: bool = False
    risk: str = "low"
    candidate_agents: tuple[str, ...] = ()
    selected_agents: tuple[str, ...] = ()
    trace: tuple[str, ...] = ()

    @property
    def candidates(self) -> tuple[str, ...]:
        return self.candidate_agents

    @property
    def selection_trace(self) -> tuple[str, ...]:
        return self.trace


class DelegationAdmissionController:
    """Small deterministic policy for deciding whether work merits delegation."""

    _MULTI_DOMAIN_CAPABILITIES = frozenset(
        {"python.development", "terraform.review", "iam.review", "cicd.review"}
    )

    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    @staticmethod
    def _review_capabilities(task: TaskSpec) -> tuple[str, ...]:
        text = " ".join(
            [task.type, task.objective, *task.scope, *task.acceptance_criteria]
        ).lower()
        if "review" not in text and "revis" not in text:
            return ()
        signals = {
            "python.development": ("python",),
            "terraform.review": ("terraform",),
            "iam.review": ("iam", "identity", "identidade", "access", "acesso"),
            "cicd.review": ("github actions", "ci/cd", "cicd", "pipeline"),
        }
        return tuple(
            capability
            for capability, terms in signals.items()
            if any(term in text for term in terms)
        )

    @staticmethod
    def _fallback_capabilities(task: TaskSpec) -> tuple[str, ...]:
        """Translate only stable keyword signals when the planner gave no requirements."""

        text = " ".join([task.type, task.objective, *task.scope]).lower()
        operation = task.operation
        if operation in {"code.implement", "code.modify", "code.fix", "code.refactor", "code.test", "code.integrate"}:
            return (operation,)
        if operation in {"implement", "modify", "fix", "refactor", "test", "integrate"}:
            return (f"code.{operation}",)
        routes = (
            (("python", "api", "sdk"), ("python.development",)),
            (("iam", "identity", "identidade", "access", "acesso", "role", "permission"), ("iam.review",)),
            (("readme", "documentation", "documentação", "runbook", "handoff", "manual"), ("documentation.authoring",)),
            (("bulk", "classify", "classification", "extract", "transform"), ("bulk.extraction",)),
            (("bash", "shell", "gcloud", "aws cli", "azure cli"), ("bash.automation",)),
            (("pipeline", "github actions", "cicd", "ci/cd"), ("cicd.review",)),
        )
        for terms, capabilities in routes:
            if any(term in text for term in terms):
                return capabilities
        # An implementation verb in prose remains a useful Builder fallback
        # even when operation was left at its backwards-compatible default.
        for verb in ("implement", "modify", "fix", "refactor", "test", "integrat"):
            if verb in text:
                capability = "integrate" if verb == "integrat" else verb
                return (f"code.{capability}",)
        return ()

    @staticmethod
    def _available_from_task(task: TaskSpec):
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        return metadata.get("available_agents") if "available_agents" in metadata else None

    def _select(
        self,
        task: TaskSpec,
        required: tuple[str, ...],
        *,
        reason: DelegationReason,
        runtime_mutation_grant: bool,
    ) -> DelegationDecision:
        available = self._available_from_task(task)
        normalized_available = CapabilityMatcher.normalize_availability(available)
        candidates = self._registry.find(
            required,
            preferred_capabilities=task.preferred_capabilities,
            task=task,
            runtime_mutation_grant=runtime_mutation_grant,
            available_agents=available,
        )
        candidate_pool = tuple(
            manifest.id
            for manifest in self._registry.manifests
            if normalized_available is None or manifest.id in normalized_available
        )
        trace = CapabilityMatcher.selection_trace(
            (
                manifest
                for manifest in self._registry.manifests
                if normalized_available is None or manifest.id in normalized_available
            ),
            candidates,
            required_capabilities=required,
            preferred_capabilities=task.preferred_capabilities,
            reason=reason.value,
        )
        common = {
            "required_capabilities": required,
            "preferred_capabilities": tuple(task.preferred_capabilities),
            "operation": task.operation,
            "mutation_required": task.requires_mutation,
            "risk": task.risk,
            "candidate_agents": candidate_pool,
            "trace": trace,
        }
        if candidates:
            return DelegationDecision(
                action=DelegationAction.DELEGATE,
                reasons=(reason,),
                selected_agents=tuple(agent.id for agent in candidates),
                **common,
            )
        return DelegationDecision(
            action=DelegationAction.EXECUTE_LOCALLY,
            reasons=(reason, DelegationReason.NO_COMPATIBLE_SPECIALIST),
            **common,
        )

    def decide(
        self,
        task: TaskSpec,
        *,
        dependencies_resolved: bool = False,
        runtime_mutation_grant: bool = False,
    ) -> DelegationDecision:
        common = {
            "preferred_capabilities": tuple(task.preferred_capabilities),
            "operation": task.operation,
            "mutation_required": task.requires_mutation,
            "risk": task.risk,
        }
        if task.dependencies and not dependencies_resolved:
            return DelegationDecision(
                action=DelegationAction.EXECUTE_LOCALLY,
                reasons=(DelegationReason.DEPENDENCIES_REQUIRE_SCHEDULER,),
                required_capabilities=tuple(task.required_capabilities),
                **common,
            )
        explicit = tuple(CapabilityMatcher.normalize(item) for item in task.required_capabilities)
        if explicit:
            if task.requires_mutation and not runtime_mutation_grant:
                return DelegationDecision(
                    action=DelegationAction.EXECUTE_LOCALLY,
                    reasons=(DelegationReason.EXPLICIT_CAPABILITIES, DelegationReason.MUTATION_PERMISSION_REQUIRED),
                    required_capabilities=explicit,
                    **common,
                )
            return self._select(
                task,
                explicit,
                reason=DelegationReason.EXPLICIT_CAPABILITIES,
                runtime_mutation_grant=runtime_mutation_grant,
            )

        inferred = self._review_capabilities(task)
        if len(inferred) >= 2:
            return self._select(
                task,
                inferred,
                reason=DelegationReason.MULTI_DOMAIN_REVIEW,
                runtime_mutation_grant=runtime_mutation_grant,
            )

        inferred = self._fallback_capabilities(task)
        if inferred and not (task.limits.timeout_seconds <= 60 and len(task.objective) <= 240):
            return self._select(
                task,
                inferred,
                reason=DelegationReason.CAPABILITY_MATCH,
                runtime_mutation_grant=runtime_mutation_grant,
            )

        if task.preferred_capabilities and not (task.limits.timeout_seconds <= 60 and len(task.objective) <= 240):
            selected = self._select(
                task,
                (),
                reason=DelegationReason.PREFERRED_CAPABILITIES,
                runtime_mutation_grant=runtime_mutation_grant,
            )
            if selected.action is DelegationAction.DELEGATE:
                return selected

        if task.limits.timeout_seconds <= 60 and len(task.objective) <= 240:
            return DelegationDecision(
                action=DelegationAction.EXECUTE_LOCALLY,
                reasons=(DelegationReason.TRIVIAL_SHORT_TASK,),
                **common,
            )
        return DelegationDecision(
            action=DelegationAction.EXECUTE_LOCALLY,
            reasons=(DelegationReason.NO_SPECIALIZATION_REQUIRED,),
            **common,
        )
