"""Runtime-owned authorization for mutation-capable subagents.

The model's ``allow_mutation`` field is only a request.  This module resolves
that request against operator state and the independent runtime boundaries:
workspace, manifest, policy and lifecycle hooks.  It intentionally does not
perform a mutation and does not prompt for approval; the caller owns the
operator interaction and passes the resulting state back on the next check.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .agent_registry import AgentManifest
from .hooks import HookError, HookManager, HookPoint
from .policies import PolicyError, PolicyEngine, PolicyRequest


class MutationGrantMode(StrEnum):
    """Permission modes relevant to a runtime mutation grant."""

    STRICT = "strict"
    BALANCED = "balanced"
    AUTO = "auto"


class MutationGrantReason(StrEnum):
    """Stable, secret-free reason codes for grant decisions."""

    GRANTED_AUTO = "granted_auto_operator_mode"
    GRANTED_BALANCED = "granted_balanced_operator_mode"
    GRANTED_STRICT = "granted_strict_explicit_approval"
    NOT_REQUESTED = "mutation_not_requested"
    INVALID_OPERATOR_STATE = "operator_state_inactive"
    PLANNING_READ_ONLY = "planning_is_read_only"
    PLAN_NOT_APPROVED = "plan_not_approved"
    WORKSPACE_DENIED = "workspace_denied"
    MANIFEST_DENIED = "manifest_denied"
    POLICY_DENIED = "policy_denied"
    POLICY_APPROVAL_REQUIRED = "policy_approval_required"
    POLICY_VALIDATION_REQUIRED = "policy_validation_required"
    HOOK_REQUIRED = "hook_authorization_required"
    HOOK_DENIED = "hook_denied"
    RUNTIME_GRANT_REQUIRED = "strict_runtime_grant_required"
    OPERATOR_APPROVAL_REQUIRED = "strict_operator_approval_required"


@dataclass(frozen=True, slots=True)
class MutationGrantRequest:
    """Runtime evidence used to evaluate one subagent spawn request.

    ``allow_mutation`` is deliberately named after the model tool argument,
    while ``runtime_mutation_grant`` and the remaining fields are supplied by
    trusted runtime boundaries.  A model payload alone therefore cannot
    construct an effective grant.

    ``plan_approved=None`` means that the request is outside ``/run``.  When a
    plan execution boundary is active it must pass ``True``; an explicit
    ``False`` always blocks the grant.  ``planning=True`` represents ``/plan``
    and is unconditionally read-only.
    """

    permission_mode: MutationGrantMode | str
    allow_mutation: bool
    runtime_mutation_grant: bool = False
    operator_approval: bool = False
    operator_state_active: bool = True
    planning: bool = False
    plan_approved: bool | None = None
    workspace_allowed: bool = True
    manifest_allowed: bool = True
    policy_allowed: bool = True
    hook_allowed: bool = True
    validation_passed: bool = False
    policy_approval_granted: bool | None = None
    workspace: Path | str | None = None
    requested_paths: Sequence[Path | str] = ()
    allowed_paths: Sequence[Path | str] = ()
    manifest: AgentManifest | Any | None = None
    policy_engine: PolicyEngine | Any | None = None
    actor: str = "runtime"
    agent: str = "subagente"
    task_id: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        mode = MutationGrantMode(self.permission_mode)
        object.__setattr__(self, "permission_mode", mode)
        object.__setattr__(self, "requested_paths", tuple(self.requested_paths))
        object.__setattr__(self, "allowed_paths", tuple(self.allowed_paths))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))
        if len(self.actor) > 256 or len(self.agent) > 256 or len(self.task_id) > 128:
            raise ValueError("Mutation grant actor, agent or task_id exceeds its limit.")
        if len(self.attributes) > 64:
            raise ValueError("Mutation grant attributes exceed 64 entries.")


@dataclass(frozen=True, slots=True)
class MutationGrantDecision:
    """Auditable result of one runtime grant evaluation."""

    granted: bool
    mode: MutationGrantMode
    reason: MutationGrantReason
    requires_approval: bool = False
    requires_validation: bool = False
    hook_checked: bool = False
    checks: tuple[str, ...] = ()

    @property
    def runtime_mutation_grant(self) -> bool:
        """Compatibility view for callers forwarding the grant to a child."""

        return self.granted

    @property
    def allow_mutation(self) -> bool:
        """Compatibility view used when constructing the child tool boundary."""

        return self.granted


class RuntimeMutationGrantPolicy:
    """Resolve mutation grants without trusting model-controlled arguments."""

    POLICY_ACTION = "agent.spawn_mutation"
    HOOK_ACTION = "spawn_subagent_mutation"

    def __init__(
        self,
        *,
        policy_engine: PolicyEngine | Any | None = None,
        hooks: HookManager | None = None,
    ) -> None:
        self.policy_engine = policy_engine
        self.hooks = hooks

    @staticmethod
    def _denied(
        request: MutationGrantRequest,
        reason: MutationGrantReason,
        *,
        checks: Sequence[str] = (),
        requires_approval: bool = False,
        requires_validation: bool = False,
        hook_checked: bool = False,
    ) -> MutationGrantDecision:
        return MutationGrantDecision(
            granted=False,
            mode=request.permission_mode,
            reason=reason,
            requires_approval=requires_approval,
            requires_validation=requires_validation,
            hook_checked=hook_checked,
            checks=tuple(checks),
        )

    @staticmethod
    def _path_inside(path: Path, boundary: Path) -> bool:
        try:
            path.relative_to(boundary)
        except ValueError:
            return False
        return True

    @classmethod
    def _resolve_workspace_paths(cls, request: MutationGrantRequest) -> bool:
        if not request.workspace_allowed:
            return False
        if not request.requested_paths:
            return True
        if request.workspace is None:
            # A caller that supplies mutation paths must also supply their
            # trusted workspace boundary.  Do not infer one from model text.
            return False
        try:
            workspace = Path(request.workspace).resolve(strict=False)
            raw_boundaries = request.allowed_paths or (workspace,)
            boundaries: list[Path] = []
            for raw_boundary in raw_boundaries:
                candidate = Path(raw_boundary)
                if not candidate.is_absolute():
                    candidate = workspace / candidate
                candidate = candidate.resolve(strict=False)
                if not cls._path_inside(candidate, workspace):
                    return False
                boundaries.append(candidate)
            for raw_path in request.requested_paths:
                candidate = Path(raw_path)
                if not candidate.is_absolute():
                    candidate = workspace / candidate
                resolved = candidate.resolve(strict=False)
                if not any(cls._path_inside(resolved, boundary) for boundary in boundaries):
                    return False
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return True

    @classmethod
    def _manifest_allows(cls, request: MutationGrantRequest) -> bool:
        if not request.manifest_allowed:
            return False
        manifest = request.manifest
        if manifest is None:
            return True
        permits = getattr(manifest, "permits_mutation", None)
        if not callable(permits):
            return False
        try:
            # This is a necessary manifest check, never the source of the
            # grant: mode/operator state is checked independently below.
            if not bool(permits(runtime_grant=True)):
                return False
        except (TypeError, ValueError, AttributeError):
            return False
        permissions = getattr(manifest, "permissions", None)
        manifest_paths = tuple(getattr(permissions, "allowed_paths", ()) or ())
        if manifest_paths and request.requested_paths:
            narrowed = replace(request, allowed_paths=manifest_paths)
            if not cls._resolve_workspace_paths(narrowed):
                return False
        return True

    def _policy_decision(
        self,
        request: MutationGrantRequest,
    ) -> tuple[MutationGrantReason | None, bool, bool]:
        if not request.policy_allowed:
            return MutationGrantReason.POLICY_DENIED, False, False
        engine = request.policy_engine if request.policy_engine is not None else self.policy_engine
        if engine is None:
            return None, False, False
        evaluate = getattr(engine, "evaluate", None)
        if not callable(evaluate):
            return MutationGrantReason.POLICY_DENIED, False, False
        policy_approval = (
            request.operator_approval
            if request.policy_approval_granted is None
            else request.policy_approval_granted
        )
        attributes = {
            "allow_mutation": True,
            "permission_mode": request.permission_mode.value,
            "operator_state_active": request.operator_state_active,
            "plan_approved": request.plan_approved,
            **dict(request.attributes),
        }
        try:
            evaluation = evaluate(
                PolicyRequest(
                    action=self.POLICY_ACTION,
                    resource=request.task_id or request.agent,
                    actor=request.actor,
                    attributes=attributes,
                    approval_granted=bool(policy_approval),
                    validation_passed=request.validation_passed,
                )
            )
        except (PolicyError, TypeError, ValueError):
            return MutationGrantReason.POLICY_DENIED, False, False
        if bool(getattr(evaluation, "denied", False)):
            return MutationGrantReason.POLICY_DENIED, False, False
        if bool(getattr(evaluation, "requires_approval", False)) and not policy_approval:
            return MutationGrantReason.POLICY_APPROVAL_REQUIRED, True, False
        if bool(getattr(evaluation, "requires_validation", False)) and not request.validation_passed:
            return MutationGrantReason.POLICY_VALIDATION_REQUIRED, False, True
        return None, False, False

    def _decide_without_hooks(
        self,
        request: MutationGrantRequest,
        *,
        hook_checked: bool,
    ) -> MutationGrantDecision:
        if not request.allow_mutation:
            return self._denied(request, MutationGrantReason.NOT_REQUESTED, checks=("model_request",))
        if request.planning:
            return self._denied(request, MutationGrantReason.PLANNING_READ_ONLY, checks=("planning",))
        if request.plan_approved is False:
            return self._denied(request, MutationGrantReason.PLAN_NOT_APPROVED, checks=("plan",))
        if not request.operator_state_active:
            return self._denied(request, MutationGrantReason.INVALID_OPERATOR_STATE, checks=("operator_state",))
        if not self._resolve_workspace_paths(request):
            return self._denied(request, MutationGrantReason.WORKSPACE_DENIED, checks=("workspace",))
        if not self._manifest_allows(request):
            return self._denied(request, MutationGrantReason.MANIFEST_DENIED, checks=("manifest",))

        policy_reason, policy_approval, policy_validation = self._policy_decision(request)
        if policy_reason is not None:
            return self._denied(
                request,
                policy_reason,
                checks=("workspace", "manifest", "policy"),
                requires_approval=policy_approval,
                requires_validation=policy_validation,
            )

        if request.permission_mode is MutationGrantMode.STRICT:
            if not request.runtime_mutation_grant:
                return self._denied(
                    request,
                    MutationGrantReason.RUNTIME_GRANT_REQUIRED,
                    checks=("workspace", "manifest", "policy", "runtime_grant"),
                )
            if not request.operator_approval:
                return self._denied(
                    request,
                    MutationGrantReason.OPERATOR_APPROVAL_REQUIRED,
                    checks=("workspace", "manifest", "policy", "runtime_grant"),
                    requires_approval=True,
                )
            reason = MutationGrantReason.GRANTED_STRICT
        elif request.permission_mode is MutationGrantMode.BALANCED:
            # Selecting /mode balanced is an operator decision.  The grant is
            # still gated by every independent boundary above.
            reason = MutationGrantReason.GRANTED_BALANCED
        else:
            # Selecting /mode auto is likewise runtime/operator state, never a
            # privilege supplied by the model's boolean argument.
            reason = MutationGrantReason.GRANTED_AUTO

        if not request.hook_allowed:
            return self._denied(
                request,
                MutationGrantReason.HOOK_DENIED,
                checks=("workspace", "manifest", "policy", "hooks"),
                hook_checked=hook_checked,
            )
        if self.hooks is not None and not hook_checked:
            return self._denied(
                request,
                MutationGrantReason.HOOK_REQUIRED,
                checks=("workspace", "manifest", "policy", "hooks"),
            )
        return MutationGrantDecision(
            granted=True,
            mode=request.permission_mode,
            reason=reason,
            hook_checked=hook_checked,
            checks=("workspace", "manifest", "policy", "hooks" if self.hooks is not None else "hooks_not_configured"),
        )

    def decide(self, request: MutationGrantRequest) -> MutationGrantDecision:
        """Evaluate a grant without executing hooks.

        If hooks are configured, callers must use :meth:`authorize`; this
        method intentionally returns ``granted=False`` until the lifecycle
        hook boundary has been crossed.
        """

        return self._decide_without_hooks(request, hook_checked=False)

    async def authorize(self, request: MutationGrantRequest) -> MutationGrantDecision:
        """Evaluate policy and invoke the before-spawn hook before granting."""

        preliminary = self._decide_without_hooks(request, hook_checked=True)
        if not preliminary.granted:
            return preliminary
        if not request.hook_allowed:
            return self._denied(
                request,
                MutationGrantReason.HOOK_DENIED,
                checks=(*preliminary.checks, "hooks"),
                hook_checked=True,
            )
        if self.hooks is not None:
            try:
                await self.hooks.emit(
                    HookPoint.BEFORE_AGENT_SPAWN,
                    self.HOOK_ACTION,
                    metadata={
                        "agent": request.agent,
                        "task_id": request.task_id,
                        "allow_mutation": True,
                        "permission_mode": request.permission_mode.value,
                    },
                    validation_passed=request.validation_passed,
                    correlation_id=request.task_id or None,
                )
            except HookError:
                return self._denied(
                    request,
                    MutationGrantReason.HOOK_DENIED,
                    checks=(*preliminary.checks, "hooks"),
                    hook_checked=True,
                )
        return replace(preliminary, hook_checked=True, checks=(*preliminary.checks, "hooks"))


# Short alias for callers that prefer the shorter noun.
MutationGrantPolicy = RuntimeMutationGrantPolicy


def resolve_mutation_grant(
    *,
    permission_mode: MutationGrantMode | str,
    allow_mutation: bool,
    runtime_mutation_grant: bool = False,
    operator_approval: bool = False,
    operator_state_active: bool = True,
    planning: bool = False,
    plan_approved: bool | None = None,
    workspace_allowed: bool = True,
    manifest_allowed: bool = True,
    policy_allowed: bool = True,
    hook_allowed: bool = True,
    validation_passed: bool = False,
    policy_approval_granted: bool | None = None,
    workspace: Path | str | None = None,
    requested_paths: Sequence[Path | str] = (),
    allowed_paths: Sequence[Path | str] = (),
    manifest: AgentManifest | Any | None = None,
    policy_engine: PolicyEngine | Any | None = None,
    actor: str = "runtime",
    agent: str = "subagente",
    task_id: str = "",
    attributes: Mapping[str, Any] | None = None,
) -> MutationGrantDecision:
    """Convenience wrapper for synchronous callers and test matrices."""

    request = MutationGrantRequest(
        permission_mode=permission_mode,
        allow_mutation=allow_mutation,
        runtime_mutation_grant=runtime_mutation_grant,
        operator_approval=operator_approval,
        operator_state_active=operator_state_active,
        planning=planning,
        plan_approved=plan_approved,
        workspace_allowed=workspace_allowed,
        manifest_allowed=manifest_allowed,
        policy_allowed=policy_allowed,
        hook_allowed=hook_allowed,
        validation_passed=validation_passed,
        policy_approval_granted=policy_approval_granted,
        workspace=workspace,
        requested_paths=requested_paths,
        allowed_paths=allowed_paths,
        manifest=manifest,
        policy_engine=policy_engine,
        actor=actor,
        agent=agent,
        task_id=task_id,
        attributes=attributes or {},
    )
    return RuntimeMutationGrantPolicy(policy_engine=policy_engine).decide(request)


__all__ = [
    "MutationGrantDecision",
    "MutationGrantMode",
    "MutationGrantPolicy",
    "MutationGrantReason",
    "MutationGrantRequest",
    "RuntimeMutationGrantPolicy",
    "resolve_mutation_grant",
]
