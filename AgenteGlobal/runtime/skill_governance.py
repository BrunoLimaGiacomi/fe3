"""Governed lifecycle for external skill installation and updates."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .hooks import HookManager, HookPoint, emit_if_configured
from .policies import PolicyEngine, PolicyRequest, SkillTrust


_HASH_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExternalSkillRecord:
    name: str
    source: str
    version: str = "unknown"
    revision: str = "unknown"
    content_hash: str | None = None
    trust: SkillTrust = SkillTrust.UNTRUSTED
    scripts: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip() or len(self.name) > 128:
            raise ValueError("skill name inválido")
        if not self.source.strip() or len(self.source) > 2_048:
            raise ValueError("skill source inválido")
        if self.content_hash is not None and _HASH_RE.fullmatch(self.content_hash) is None:
            raise ValueError("skill hash precisa ser SHA-256")
        object.__setattr__(self, "trust", SkillTrust(self.trust))
        object.__setattr__(self, "scripts", tuple(self.scripts))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))


class SkillMutationProvider(Protocol):
    def install(self, name: str, **kwargs: Any) -> Any: ...

    def update(self, name: str | None = None, **kwargs: Any) -> Any: ...


SkillValidator = Callable[[ExternalSkillRecord, str], bool | Awaitable[bool]]


class SkillLifecycleManager:
    def __init__(
        self,
        *,
        policy_engine: PolicyEngine | None = None,
        hooks: HookManager | None = None,
    ) -> None:
        self.policy_engine = policy_engine or PolicyEngine.with_skill_trust()
        self.hooks = hooks

    @staticmethod
    async def _resolve(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    async def _mutate(
        self,
        operation: str,
        provider: SkillMutationProvider,
        record: ExternalSkillRecord,
        *,
        authorized: bool,
        validator: SkillValidator | None,
    ) -> Any:
        validated = False
        if validator is not None:
            validated = bool(await self._resolve(validator(record, operation)))
        request = PolicyRequest(
            action=f"skill.{operation}",
            resource=record.name,
            attributes={
                "source": record.source,
                "version": record.version,
                "revision": record.revision,
                "hash": record.content_hash,
                "scripts": record.scripts,
                "dependencies": record.dependencies,
                "capabilities": record.capabilities,
            },
            trust=record.trust,
            approval_granted=authorized,
            validation_passed=validated,
        )
        self.policy_engine.enforce(request)
        metadata = {
            "skill": record.name,
            "source": record.source,
            "version": record.version,
            "trust": record.trust.value,
            "operation": operation,
        }
        await emit_if_configured(
            self.hooks,
            HookPoint.BEFORE_SKILL_INSTALL,
            f"skill.{operation}",
            metadata=metadata,
            validation_passed=validated,
        )
        method = provider.install if operation == "install" else provider.update
        result = await self._resolve(
            method(record.name, authorized=authorized, validation_passed=validated)
        )
        await emit_if_configured(
            self.hooks,
            HookPoint.AFTER_SKILL_INSTALL,
            f"skill.{operation}",
            metadata={**metadata, "status": "completed"},
            validation_passed=True,
        )
        return result

    async def install(
        self,
        provider: SkillMutationProvider,
        record: ExternalSkillRecord,
        *,
        authorized: bool = False,
        validator: SkillValidator | None = None,
    ) -> Any:
        return await self._mutate("install", provider, record, authorized=authorized, validator=validator)

    async def update(
        self,
        provider: SkillMutationProvider,
        record: ExternalSkillRecord,
        *,
        authorized: bool = False,
        validator: SkillValidator | None = None,
    ) -> Any:
        return await self._mutate("update", provider, record, authorized=authorized, validator=validator)


__all__ = ["ExternalSkillRecord", "SkillLifecycleManager", "SkillMutationProvider", "SkillValidator"]
