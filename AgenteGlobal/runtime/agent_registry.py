"""Validated, boundary-confined loading and matching of agent manifests."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_CAPABILITY_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
DEFAULT_MAX_MANIFESTS = 64
DEFAULT_MAX_MANIFEST_BYTES = 32_768


class ManifestError(ValueError):
    """The manifest could not be loaded safely or did not meet the contract."""


class ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, validate_assignment=True)


class AgentPermissions(ManifestModel):
    # Campo legado preservado para compatibilidade. A Fase 5 usa network
    # allow-by-default e não deriva bloqueio de CLI/PowerShell deste valor.
    allow_network: bool = False
    allow_shell: bool = False
    allow_sensitive_read: bool = False
    allowed_paths: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("allowed_paths")
    @classmethod
    def _valid_allowed_paths(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            candidate = value.replace("\\", "/").strip().strip("/")
            path = Path(candidate)
            if not candidate or path.is_absolute() or ".." in path.parts or ":" in candidate:
                raise ValueError("permissions.allowed_paths must contain relative paths inside the workspace.")
            if candidate not in normalized:
                normalized.append(candidate)
        return normalized


class MutationPolicy(ManifestModel):
    default_mutation: Literal[False] = False
    requires_runtime_grant: Literal[True] = True

    def permits(self, *, runtime_grant: bool) -> bool:
        """A manifest can never independently authorize a mutation."""
        return bool(runtime_grant)


class ContextSlice(ManifestModel):
    """Deliberately small context-selection result; content injection remains runtime-owned."""

    history: tuple[str, ...] = ()
    global_context: str | None = None
    skills: tuple[str, ...] = ()


class ContextPolicy(ManifestModel):
    include_history: bool = False
    include_global_context: bool = False
    include_all_skills: bool = False
    skills: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("skills")
    @classmethod
    def _valid_skill_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.lower() for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("context.skills must not contain duplicates.")
        if any(not _IDENTIFIER_RE.fullmatch(value) for value in normalized):
            raise ValueError("context.skills must contain stable skill identifiers, not paths.")
        return normalized

    @model_validator(mode="after")
    def _all_skills_is_unambiguous(self) -> "ContextPolicy":
        if self.include_all_skills and self.skills:
            raise ValueError("context.skills must be empty when include_all_skills is true.")
        return self

    def slice_context(
        self,
        *,
        history: Sequence[str] = (),
        global_context: str | None = None,
        available_skills: Sequence[str] = (),
    ) -> ContextSlice:
        """Policy-only slicing stub; it neither reads files nor expands skills."""
        selected_skills = (
            tuple(available_skills)
            if self.include_all_skills
            else tuple(skill for skill in available_skills if skill in self.skills)
        )
        return ContextSlice(
            history=tuple(history) if self.include_history else (),
            global_context=global_context if self.include_global_context else None,
            skills=selected_skills,
        )


class OutputPolicy(ManifestModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )

    schema_name: Literal["agent_result.v1"] = Field(
        default="agent_result.v1",
        validation_alias="schema",
        serialization_alias="schema",
    )
    function_name: Literal["submit_agent_result"] = "submit_agent_result"

    @property
    def schema(self) -> str:  # pyright: ignore[reportIncompatibleMethodOverride]
        return self.schema_name


class AgentLimits(ManifestModel):
    timeout_seconds: int = Field(default=1_800, ge=1, le=86_400)
    max_steps: int = Field(default=64, ge=1, le=10_000)
    max_output_chars: int = Field(default=20_000, ge=1, le=5_000_000)


class AgentManifest(ManifestModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5_000)
    instructions: str = Field(min_length=1, max_length=50_000)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    mutation: MutationPolicy = Field(default_factory=MutationPolicy)
    context: ContextPolicy = Field(default_factory=ContextPolicy)
    preferred_thinking: Literal["low", "medium", "high", "max"] = "max"
    thinking_enabled: bool = True
    output: OutputPolicy = Field(default_factory=OutputPolicy)
    limits: AgentLimits = Field(default_factory=AgentLimits)

    @model_validator(mode="before")
    @classmethod
    def _adapt_legacy_manifest(cls, raw: object) -> object:
        if not isinstance(raw, dict):
            return raw
        data = dict(raw)
        if "default_mutation" in data:
            mutation = dict(data.get("mutation") or {})
            mutation.setdefault("default_mutation", data.pop("default_mutation"))
            data["mutation"] = mutation
        instructions = data.get("instructions")
        if isinstance(instructions, Mapping):
            data["instructions"] = instructions.get("text", instructions.get("developer"))
        if "instructions" not in data and "developer_instructions" in data:
            data["instructions"] = data.pop("developer_instructions")
        return data

    @field_validator("id")
    @classmethod
    def _valid_identifier(cls, value: str) -> str:
        normalized = value.lower()
        if not _IDENTIFIER_RE.fullmatch(normalized):
            raise ValueError("id must be a stable lowercase identifier using letters, digits, and hyphens.")
        return normalized

    @field_validator("capabilities")
    @classmethod
    def _valid_capabilities(cls, values: list[str]) -> list[str]:
        normalized = [value.lower() for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("capabilities must not contain duplicates.")
        if any(not _CAPABILITY_RE.fullmatch(value) for value in normalized):
            raise ValueError("capabilities must be stable dotted or hyphenated identifiers.")
        return normalized

    @property
    def identifier(self) -> str:
        """Compatibility name used by existing consumers."""
        return self.id

    @property
    def developer_instructions(self) -> str:
        """Compatibility view of the legacy TOML field."""
        return self.instructions

    @property
    def default_mutation(self) -> bool:
        """Compatibility/readability view; the enforced manifest default is always false."""

        return self.mutation.default_mutation

    def permits_mutation(self, *, runtime_grant: bool) -> bool:
        return self.mutation.permits(runtime_grant=runtime_grant)

    @property
    def reasoning_mode(self):
        """Keep reasoning effort independent from provider deep-thinking mode."""
        from llm.reasoning import ReasoningMode

        if self.preferred_thinking == "max":
            return ReasoningMode.DEEP if self.thinking_enabled else ReasoningMode.MAX_ONLY
        return ReasoningMode.NORMAL


def _is_within(path: Path, boundary: Path) -> bool:
    try:
        path.relative_to(boundary)
    except ValueError:
        return False
    return True


def _has_symlink_component(path: Path, boundary: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == boundary:
            return False
        if current.parent == current:
            return True
        current = current.parent


class AgentRegistry:
    """Immutable-in-practice registry. Loading is explicit and filesystem-bound."""

    def __init__(
        self,
        manifests: Iterable[AgentManifest] = (),
        *,
        available_agents: Iterable[str] | Mapping[str, bool] | None = None,
    ) -> None:
        items = tuple(manifests)
        ids = [manifest.id for manifest in items]
        if len(set(ids)) != len(ids):
            raise ManifestError("Duplicate agent manifest id.")
        self._by_id = {manifest.id: manifest for manifest in items}
        self._available_agents = CapabilityMatcher.normalize_availability(available_agents)

    @classmethod
    def load(
        cls,
        directory: Path,
        *,
        boundary: Path,
        max_manifests: int = DEFAULT_MAX_MANIFESTS,
        max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    ) -> "AgentRegistry":
        if max_manifests < 1 or max_manifest_bytes < 1:
            raise ValueError("Manifest limits must be positive.")
        try:
            root = boundary.resolve(strict=True)
            target = directory.resolve(strict=True)
        except OSError as error:
            raise ManifestError(f"Cannot resolve agent manifest directory: {directory}") from error
        if not root.is_dir() or not target.is_dir():
            raise ManifestError("Manifest boundary and directory must be directories.")
        if not _is_within(target, root):
            raise ManifestError("Agent manifest directory is outside the configured boundary.")
        if _has_symlink_component(directory.absolute(), boundary.absolute()):
            raise ManifestError("Symlinks are not allowed in the manifest directory path.")

        files = sorted((entry for entry in target.iterdir() if entry.suffix == ".toml"), key=lambda item: item.name)
        if len(files) > max_manifests:
            raise ManifestError(f"Manifest count exceeds configured limit of {max_manifests}.")
        manifests: list[AgentManifest] = []
        for file_path in files:
            if not file_path.is_file() or file_path.is_symlink():
                raise ManifestError(f"Manifest must be a regular non-symlink file: {file_path.name}")
            try:
                size = file_path.stat().st_size
            except OSError as error:
                raise ManifestError(f"Cannot stat manifest: {file_path.name}") from error
            if size > max_manifest_bytes:
                raise ManifestError(f"Manifest exceeds {max_manifest_bytes} bytes: {file_path.name}")
            try:
                with file_path.open("rb") as stream:
                    raw = tomllib.load(stream)
                raw.setdefault("id", file_path.stem.lower())
                manifest = AgentManifest.model_validate(raw)
                if manifest.id != file_path.stem.lower():
                    raise ValueError("manifest id must match its TOML filename")
            except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
                raise ManifestError(f"Invalid agent manifest {file_path.name}: {error}") from error
            manifests.append(manifest)
        return cls(manifests)

    @property
    def manifests(self) -> tuple[AgentManifest, ...]:
        return tuple(self._by_id[identifier] for identifier in sorted(self._by_id))

    def get(self, identifier: str) -> AgentManifest | None:
        return self._by_id.get(identifier.lower())

    def _require(self, identifier: str) -> AgentManifest:
        manifest = self.get(identifier)
        if manifest is None:
            raise KeyError(f"Unknown agent manifest: {identifier}")
        return manifest

    def list_agents(self) -> tuple[str, ...]:
        return tuple(manifest.id for manifest in self.manifests)

    def capabilities(self, identifier: str) -> tuple[str, ...]:
        return tuple(self._require(identifier).capabilities)

    def reasoning_policy(self, identifier: str) -> str:
        return self._require(identifier).preferred_thinking

    def context_policy(self, identifier: str) -> ContextPolicy:
        return self._require(identifier).context

    def mutation_policy(self, identifier: str) -> MutationPolicy:
        return self._require(identifier).mutation

    def permissions_policy(self, identifier: str) -> AgentPermissions:
        return self._require(identifier).permissions

    def find_by_capability(self, capability: str) -> tuple[AgentManifest, ...]:
        normalized = CapabilityMatcher.normalize(capability)
        return tuple(manifest for manifest in self.manifests if normalized in manifest.capabilities)

    def find(
        self,
        required_capabilities: Iterable[str] = (),
        *,
        preferred_capabilities: Iterable[str] = (),
        task: object | None = None,
        operation: str | None = None,
        mutation_required: bool = False,
        risk: str = "low",
        context_skills: Iterable[str] = (),
        runtime_mutation_grant: bool = False,
        available_agents: Iterable[str] | Mapping[str, bool] | None = None,
    ) -> tuple[AgentManifest, ...]:
        """Select a deterministic capability cover for a task.

        The registry remains a source of manifests; policy about whether work
        should be delegated is owned by ``DelegationAdmissionController``.
        ``available_agents`` is an optional runtime snapshot and is never
        inferred from prose.
        """

        return CapabilityMatcher.select_covering_agents(
            self.manifests,
            required_capabilities,
            preferred_capabilities=preferred_capabilities,
            task=task,
            operation=operation,
            mutation_required=mutation_required,
            risk=risk,
            context_skills=context_skills,
            runtime_mutation_grant=runtime_mutation_grant,
            available_agents=(
                self._available_agents if available_agents is None else available_agents
            ),
        )


class CapabilityMatcher:
    """Pure, deterministic routing of a task to compatible manifests."""

    @staticmethod
    def normalize(capability: str) -> str:
        normalized = capability.strip().lower()
        if not _CAPABILITY_RE.fullmatch(normalized):
            raise ValueError(f"Invalid capability identifier: {capability!r}")
        return normalized

    @classmethod
    def matches_all(cls, manifest: AgentManifest, required_capabilities: Iterable[str]) -> bool:
        required = {cls.normalize(capability) for capability in required_capabilities}
        return required.issubset(set(manifest.capabilities))

    @staticmethod
    def normalize_availability(
        available_agents: Iterable[str] | Mapping[str, bool] | None,
    ) -> frozenset[str] | None:
        """Normalize an optional runtime availability snapshot.

        ``None`` means availability is not constrained.  A mapping keeps only
        explicitly true entries; an iterable names available agent ids.
        """

        if available_agents is None:
            return None
        if isinstance(available_agents, Mapping):
            return frozenset(str(identifier).strip().lower() for identifier, value in available_agents.items() if value)
        return frozenset(str(identifier).strip().lower() for identifier in available_agents)

    @classmethod
    def _task_value(cls, task: object | None, name: str, default):
        if task is None:
            return default
        value = getattr(task, name, default)
        return default if value is None else value

    @classmethod
    def _routing_inputs(
        cls,
        task: object | None,
        required_capabilities: Iterable[str],
        preferred_capabilities: Iterable[str],
        operation: str | None,
        mutation_required: bool,
        risk: str,
        context_skills: Iterable[str],
    ) -> tuple[set[str], set[str], str, bool, str, set[str]]:
        required = required_capabilities or cls._task_value(task, "required_capabilities", ())
        preferred = preferred_capabilities or cls._task_value(task, "preferred_capabilities", ())
        operation_value = operation or cls._task_value(task, "operation", "")
        mutation_value = bool(mutation_required or cls._task_value(task, "requires_mutation", False))
        risk_value = str(risk or cls._task_value(task, "risk", "low")).strip().lower()
        context_value = context_skills or cls._task_value(task, "context_skills", ())
        if not context_value and task is not None:
            metadata = getattr(task, "metadata", {}) or {}
            if isinstance(metadata, Mapping):
                context_value = metadata.get("context_skills", metadata.get("available_skills", ()))
        return (
            {cls.normalize(value) for value in required},
            {cls.normalize(value) for value in preferred},
            str(operation_value).strip().lower(),
            mutation_value,
            risk_value,
            {str(value).strip().lower() for value in context_value if str(value).strip()},
        )

    @classmethod
    def _candidate_score(
        cls,
        manifest: AgentManifest,
        *,
        required: set[str],
        preferred: set[str],
        operation: str,
        risk: str,
        context_skills: set[str],
    ) -> tuple[int, int, int, int, int]:
        capabilities = set(manifest.capabilities)
        required_matches = len(required.intersection(capabilities))
        preferred_matches = len(preferred.intersection(capabilities))
        operation_match = int(bool(operation and operation in capabilities))
        context_matches = len(context_skills.intersection(set(manifest.context.skills)))
        # Exact required/operation matches dominate broad capability counts.
        # Risk adds a small deterministic preference for a focused specialist,
        # while context policy remains a tie-breaker rather than authorization.
        risk_weight = 2 if risk in {"high", "critical"} else 1
        return (
            required_matches,
            preferred_matches + operation_match * risk_weight,
            operation_match,
            context_matches,
            -len(manifest.id),
        )

    @classmethod
    def select_covering_agents(
        cls,
        manifests: Iterable[AgentManifest],
        required_capabilities: Iterable[str] = (),
        *,
        preferred_capabilities: Iterable[str] = (),
        task: object | None = None,
        operation: str | None = None,
        mutation_required: bool = False,
        risk: str = "low",
        context_skills: Iterable[str] = (),
        runtime_mutation_grant: bool = False,
        available_agents: Iterable[str] | Mapping[str, bool] | None = None,
    ) -> tuple[AgentManifest, ...]:
        """Return a stable cover; required capabilities are never relaxed."""

        required, preferred, operation_value, mutation, risk_value, context = cls._routing_inputs(
            task,
            required_capabilities,
            preferred_capabilities,
            operation,
            mutation_required,
            risk,
            context_skills,
        )
        available = cls.normalize_availability(available_agents)
        candidates = tuple(
            sorted(
                (
                    manifest
                    for manifest in manifests
                    if (available is None or manifest.id in available)
                    and (not mutation or manifest.permits_mutation(runtime_grant=runtime_mutation_grant))
                ),
                key=lambda manifest: manifest.id,
            )
        )
        if not required:
            if not (preferred or operation_value or context):
                return ()
            if not candidates:
                return ()
            best = max(
                candidates,
                key=lambda manifest: cls._candidate_score(
                    manifest,
                    required=required,
                    preferred=preferred,
                    operation=operation_value,
                    risk=risk_value,
                    context_skills=context,
                ),
            )
            score = cls._candidate_score(
                best,
                required=required,
                preferred=preferred,
                operation=operation_value,
                risk=risk_value,
                context_skills=context,
            )
            if score[1] == 0 and score[2] == 0 and score[3] == 0:
                return ()
            return (best,)

        selected: list[AgentManifest] = []
        uncovered = set(required)
        while uncovered:
            best = max(
                candidates,
                key=lambda manifest: (
                    len(uncovered.intersection(manifest.capabilities)),
                    *cls._candidate_score(
                        manifest,
                        required=uncovered,
                        preferred=preferred,
                        operation=operation_value,
                        risk=risk_value,
                        context_skills=context,
                    )[1:],
                ),
                default=None,
            )
            if best is None or not uncovered.intersection(best.capabilities):
                return ()
            selected.append(best)
            uncovered.difference_update(best.capabilities)
            candidates = tuple(candidate for candidate in candidates if candidate.id != best.id)
        return tuple(selected)

    @classmethod
    def select(cls, task: object, registry: AgentRegistry) -> tuple[AgentManifest, ...]:
        """Task-first adapter used by callers that already have a TaskSpec."""

        return cls.select_covering_agents(registry.manifests, task=task)

    @classmethod
    def match(cls, task: object, registry: AgentRegistry) -> tuple[AgentManifest, ...]:
        """Alias for ``select`` kept intentionally provider/runtime local."""

        return cls.select(task, registry)

    @classmethod
    def selection_trace(
        cls,
        manifests: Iterable[AgentManifest],
        selected: Iterable[AgentManifest],
        *,
        required_capabilities: Iterable[str] = (),
        preferred_capabilities: Iterable[str] = (),
        reason: str = "capability_match",
    ) -> tuple[str, ...]:
        """Produce bounded routing facts, never model reasoning content."""

        required = tuple(sorted({cls.normalize(item) for item in required_capabilities}))
        preferred = tuple(sorted({cls.normalize(item) for item in preferred_capabilities}))
        candidates = tuple(sorted(manifest.id for manifest in manifests))
        chosen = tuple(manifest.id for manifest in selected)
        return (
            f"required={','.join(required) or '-'}",
            f"preferred={','.join(preferred) or '-'}",
            f"candidates={','.join(candidates) or '-'}",
            f"selected={','.join(chosen) or '-'}",
            f"reason={reason}",
        )
