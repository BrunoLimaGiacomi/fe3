"""Extensible policy decisions kept outside the AgenteGlobal Core."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    REQUIRE_VALIDATION = "require_validation"


class SkillTrust(StrEnum):
    BUILTIN = "builtin"
    LOCAL = "local"
    TRUSTED = "trusted"
    COMMUNITY = "community"
    UNTRUSTED = "untrusted"


class PolicyError(RuntimeError):
    pass


class PolicyDeniedError(PermissionError, PolicyError):
    pass


class PolicyApprovalRequired(PermissionError, PolicyError):
    pass


class PolicyValidationRequired(PolicyError):
    pass


@dataclass(frozen=True, slots=True)
class PolicyRequest:
    action: str
    resource: str = ""
    actor: str = "runtime"
    attributes: Mapping[str, Any] = field(default_factory=dict)
    trust: SkillTrust | str | None = None
    approval_granted: bool = False
    validation_passed: bool = False

    def __post_init__(self) -> None:
        if not self.action or len(self.action) > 256:
            raise ValueError("policy action must be a bounded non-empty string")
        if len(self.resource) > 2_048 or len(self.actor) > 256:
            raise ValueError("policy resource or actor exceeds its limit")
        if len(self.attributes) > 64:
            raise ValueError("policy attributes exceed 64 entries")
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))
        if self.trust is not None:
            object.__setattr__(self, "trust", SkillTrust(self.trust))


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    effect: PolicyEffect
    policy: str
    reason: str

    def __post_init__(self) -> None:
        if not self.policy or len(self.policy) > 128:
            raise ValueError("policy name must be bounded")
        if not self.reason or len(self.reason) > 2_000:
            raise ValueError("policy reason must be bounded")


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    request: PolicyRequest
    decisions: tuple[PolicyDecision, ...]

    @property
    def denied(self) -> bool:
        return any(item.effect is PolicyEffect.DENY for item in self.decisions)

    @property
    def requires_approval(self) -> bool:
        return any(item.effect is PolicyEffect.REQUIRE_APPROVAL for item in self.decisions)

    @property
    def requires_validation(self) -> bool:
        return any(item.effect is PolicyEffect.REQUIRE_VALIDATION for item in self.decisions)

    @property
    def effect(self) -> PolicyEffect:
        if self.denied:
            return PolicyEffect.DENY
        if self.requires_approval and not self.request.approval_granted:
            return PolicyEffect.REQUIRE_APPROVAL
        if self.requires_validation and not self.request.validation_passed:
            return PolicyEffect.REQUIRE_VALIDATION
        return PolicyEffect.ALLOW

    @property
    def allowed(self) -> bool:
        return self.effect is PolicyEffect.ALLOW


@runtime_checkable
class Policy(Protocol):
    name: str

    def evaluate(self, request: PolicyRequest) -> PolicyDecision | None: ...


@dataclass(frozen=True, slots=True)
class RulePolicy:
    name: str
    effect: PolicyEffect
    reason: str
    matcher: Callable[[PolicyRequest], bool]

    def evaluate(self, request: PolicyRequest) -> PolicyDecision | None:
        return PolicyDecision(self.effect, self.name, self.reason) if self.matcher(request) else None


class SkillTrustPolicy:
    """Default external-skill gate; it does not infer trust from prose."""

    name = "skill-trust"
    _ACTIONS = frozenset({"skill.install", "skill.update", "skill.execute_script"})

    def evaluate(self, request: PolicyRequest) -> PolicyDecision | None:
        if request.action not in self._ACTIONS:
            return None
        trust = SkillTrust(request.trust or SkillTrust.UNTRUSTED)
        if trust is SkillTrust.UNTRUSTED:
            return PolicyDecision(PolicyEffect.DENY, self.name, "skill não confiável não pode executar mutações")
        if trust is SkillTrust.COMMUNITY:
            return PolicyDecision(
                PolicyEffect.REQUIRE_APPROVAL,
                self.name,
                "skill comunitária exige aprovação humana explícita",
            )
        if trust in {SkillTrust.TRUSTED, SkillTrust.LOCAL}:
            return PolicyDecision(
                PolicyEffect.REQUIRE_VALIDATION,
                self.name,
                "skill precisa de validação de origem e integridade",
            )
        return PolicyDecision(PolicyEffect.ALLOW, self.name, "skill builtin permitida")


class SkillIntegrityPolicy:
    name = "skill-integrity"
    _ACTIONS = frozenset({"skill.install", "skill.update", "skill.execute_script"})

    def evaluate(self, request: PolicyRequest) -> PolicyDecision | None:
        if request.action not in self._ACTIONS or request.trust is SkillTrust.BUILTIN:
            return None
        return PolicyDecision(
            PolicyEffect.REQUIRE_VALIDATION,
            self.name,
            "origem, revisão, hash e conteúdo da skill externa precisam ser validados",
        )


class PolicyEngine:
    """Evaluate isolated policies and enforce their combined obligations."""

    def __init__(
        self,
        policies: Iterable[Policy] = (),
        *,
        constitution_inputs: Sequence[str] = (),
    ) -> None:
        self._policies: list[Policy] = []
        self.constitution_inputs = tuple(str(value) for value in constitution_inputs if str(value).strip())
        for policy in policies:
            self.register(policy)

    @classmethod
    def with_skill_trust(cls, *, constitution_inputs: Sequence[str] = ()) -> "PolicyEngine":
        return cls((SkillTrustPolicy(), SkillIntegrityPolicy()), constitution_inputs=constitution_inputs)

    def register(self, policy: Policy) -> None:
        if not isinstance(policy, Policy):
            raise TypeError("policy must expose name and evaluate(request)")
        if any(item.name == policy.name for item in self._policies):
            raise ValueError(f"duplicate policy name: {policy.name}")
        self._policies.append(policy)

    def evaluate(self, request: PolicyRequest) -> PolicyEvaluation:
        decisions: list[PolicyDecision] = []
        for policy in tuple(self._policies):
            decision = policy.evaluate(request)
            if decision is not None:
                decisions.append(decision)
        if not decisions:
            decisions.append(PolicyDecision(PolicyEffect.ALLOW, "default", "nenhuma policy aplicável"))
        return PolicyEvaluation(request, tuple(decisions))

    def enforce(self, request: PolicyRequest) -> PolicyEvaluation:
        evaluation = self.evaluate(request)
        reasons = "; ".join(item.reason for item in evaluation.decisions)
        if evaluation.denied:
            raise PolicyDeniedError(reasons)
        if evaluation.requires_approval and not request.approval_granted:
            raise PolicyApprovalRequired(reasons)
        if evaluation.requires_validation and not request.validation_passed:
            raise PolicyValidationRequired(reasons)
        return evaluation


__all__ = [
    "Policy",
    "PolicyApprovalRequired",
    "PolicyDecision",
    "PolicyDeniedError",
    "PolicyEffect",
    "PolicyEngine",
    "PolicyError",
    "PolicyEvaluation",
    "PolicyRequest",
    "PolicyValidationRequired",
    "RulePolicy",
    "SkillTrust",
    "SkillIntegrityPolicy",
    "SkillTrustPolicy",
]
