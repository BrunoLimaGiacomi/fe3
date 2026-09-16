"""Shared policy, hook and checkpoint wiring for runtime tool execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .checkpoints import CheckpointManager
from .hooks import HookManager, HookPoint, emit_if_configured
from .policies import PolicyEngine, PolicyRequest
from .security_text import redact_cli_args, redact_command_text


@dataclass(slots=True)
class RuntimeGovernance:
    hooks: HookManager | None = None
    policy_engine: PolicyEngine | None = None
    checkpoints: CheckpointManager | None = None

    def __post_init__(self) -> None:
        if self.policy_engine is None:
            self.policy_engine = PolicyEngine()

    @classmethod
    def for_workspace(
        cls,
        workspace: Path,
        *,
        constitution_inputs: Sequence[str] = (),
    ) -> "RuntimeGovernance":
        return cls(
            hooks=HookManager(),
            policy_engine=PolicyEngine(constitution_inputs=constitution_inputs),
            checkpoints=CheckpointManager(workspace),
        )

    @staticmethod
    def _attributes(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            "tool": name,
            "mutating": name in {"write_file", "run_cli", "run_powershell"},
        }
        if name == "write_file":
            attributes.update(path=str(arguments.get("path") or ""), overwrite=bool(arguments.get("overwrite")))
        elif name == "run_cli":
            attributes.update(
                cli=str(arguments.get("cli") or ""),
                args=tuple(redact_cli_args(list(arguments.get("args") or []))),
            )
        elif name == "run_powershell":
            attributes["command"] = redact_command_text(str(arguments.get("command") or ""))
        return attributes

    async def before_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        actor: str,
        correlation_id: str,
        approval_granted: bool = False,
        validation_passed: bool = False,
    ) -> None:
        attributes = self._attributes(name, arguments)
        assert self.policy_engine is not None
        self.policy_engine.enforce(
            PolicyRequest(
                action=f"tool.{name}",
                resource=str(attributes.get("path") or name),
                actor=actor,
                attributes=attributes,
                approval_granted=approval_granted,
                validation_passed=validation_passed,
            )
        )
        await emit_if_configured(
            self.hooks,
            HookPoint.BEFORE_TOOL,
            name,
            metadata={"tool": name, "mutating": attributes["mutating"]},
            correlation_id=correlation_id,
        )
        if name == "write_file":
            await emit_if_configured(
                self.hooks,
                HookPoint.BEFORE_WRITE,
                name,
                metadata={"path": attributes["path"], "overwrite": attributes["overwrite"]},
                correlation_id=correlation_id,
            )

    async def after_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        correlation_id: str,
        validation_passed: bool,
    ) -> None:
        attributes = self._attributes(name, arguments)
        if name == "write_file":
            await emit_if_configured(
                self.hooks,
                HookPoint.AFTER_WRITE,
                name,
                metadata={"path": attributes["path"], "status": "completed"},
                validation_passed=validation_passed,
                correlation_id=correlation_id,
            )
        await emit_if_configured(
            self.hooks,
            HookPoint.AFTER_TOOL,
            name,
            metadata={"tool": name, "mutating": attributes["mutating"], "status": "completed"},
            validation_passed=validation_passed,
            correlation_id=correlation_id,
        )

    async def before_agent_spawn(
        self,
        task_id: str,
        *,
        agent: str,
        allow_mutation: bool,
    ) -> None:
        await emit_if_configured(
            self.hooks,
            HookPoint.BEFORE_AGENT_SPAWN,
            task_id,
            metadata={"agent": agent, "task_id": task_id, "allow_mutation": allow_mutation},
            correlation_id=task_id,
        )

    async def after_agent_spawn(self, task_id: str, *, agent: str, status: str, validated: bool) -> None:
        await emit_if_configured(
            self.hooks,
            HookPoint.AFTER_AGENT_SPAWN,
            task_id,
            metadata={"agent": agent, "task_id": task_id, "status": status},
            validation_passed=validated,
            correlation_id=task_id,
        )

    async def before_final(self, agent: str, *, structured_result: bool) -> None:
        await emit_if_configured(
            self.hooks,
            HookPoint.BEFORE_FINAL,
            agent,
            metadata={"agent": agent, "structured_result": structured_result},
            validation_passed=True,
        )


__all__ = ["RuntimeGovernance"]
