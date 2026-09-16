"""Strict validation boundary for model-provided tool arguments."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from Painel import DEFAULT_MAX_SEARCH_SCANNED_FILES, DEFAULT_TIMEOUT_SECONDS
from .contracts import Plan, ScopeExpansion, TaskSpec, UserQuestion

MAX_PROFILE_NAME_CHARS = 80
MAX_SUBAGENT_TASK_CHARS = 4_000
MAX_ARTIFACT_TOOL_READ_BYTES = 8_000


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListDirArguments(ToolArguments):
    path: str = "."
    path_reference: str = ""
    max_entries: int = Field(default=100, ge=1, le=500)


class ReadFileArguments(ToolArguments):
    path: str
    path_reference: str = ""
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=500)


class ReadArtifactArguments(ToolArguments):
    artifact_id: str = Field(pattern=r"^artifact-[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=MAX_ARTIFACT_TOOL_READ_BYTES, ge=1, le=MAX_ARTIFACT_TOOL_READ_BYTES)


class RetrieveContextArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=20_000)
    top_k: int = Field(default=4, ge=1, le=8)
    artifact_id: str | None = Field(default=None, pattern=r"^artifact-[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    strategy: str = Field(default="hybrid", pattern=r"^(lexical|hybrid|semantic)$")


class SearchTextArguments(ToolArguments):
    pattern: str
    path: str = "."
    path_reference: str = ""
    max_matches: int = Field(default=50, ge=1, le=200)
    max_scanned_files: int = Field(default=DEFAULT_MAX_SEARCH_SCANNED_FILES, ge=1, le=20_000)


class WriteFileArguments(ToolArguments):
    path: str
    path_reference: str = ""
    content: str
    overwrite: bool = False


class RunCliArguments(ToolArguments):
    cli: str
    args: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1, le=300)


class RunPowerShellArguments(ToolArguments):
    command: str
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1, le=120)


class SpawnSubagentArguments(ToolArguments):
    task: str = Field(min_length=1, max_length=MAX_SUBAGENT_TASK_CHARS)
    name: str = Field(default="subagente", max_length=MAX_PROFILE_NAME_CHARS)
    scope: str = Field(default="", max_length=MAX_SUBAGENT_TASK_CHARS)
    max_steps: int | None = Field(default=None, ge=1)
    allow_mutation: bool = False
    profile: str = ""
    required_capabilities: list[str] = Field(default_factory=list)
    task_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("required_capabilities")
    @classmethod
    def validate_capabilities(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = value.strip().lower()
            if not re.fullmatch(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*", item):
                raise ValueError(f"Capability inválida: {value!r}")
            if item not in normalized:
                normalized.append(item)
        return normalized

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized):
            raise ValueError("Profile precisa ser um identificador estável.")
        return normalized

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_acceptance_criteria(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 2_000 for value in values):
            raise ValueError("Critérios de aceite precisam ser não vazios e ter até 2000 caracteres.")
        if len(set(values)) != len(values):
            raise ValueError("Critérios de aceite não podem ser duplicados.")
        return values


class DelegateTaskArguments(ToolArguments):
    task_spec: TaskSpec
    allow_mutation: bool = False


class AskUserQuestionArguments(ToolArguments):
    question: UserQuestion


class SubmitPlanArguments(ToolArguments):
    plan: Plan

    @field_validator("plan", mode="before")
    @classmethod
    def decode_provider_nested_json(cls, value: Any) -> Any:
        """Accept providers that serialize the nested Plan object once more.

        The compatibility conversion is local to this boundary; the decoded
        value still has to satisfy the complete strict ``Plan`` contract.
        """

        if isinstance(value, Plan):
            return value
        raw_json: str
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("plan precisa ser um objeto JSON válido") from exc
            if not isinstance(decoded, dict):
                raise ValueError("plan precisa ser um objeto JSON")
            raw_json = value
        elif isinstance(value, dict):
            raw_json = json.dumps(value, ensure_ascii=False, default=str)
        else:
            return value
        # JSON-mode validation preserves strict field contracts while allowing
        # the standard ISO datetime representation used on the wire.
        return Plan.model_validate_json(raw_json)


class RequestScopeExpansionArguments(ToolArguments):
    expansion: ScopeExpansion


TOOL_ARGUMENT_MODELS: dict[str, type[ToolArguments]] = {
    "list_dir": ListDirArguments, "read_file": ReadFileArguments,
    "read_artifact": ReadArtifactArguments, "retrieve_context": RetrieveContextArguments,
    "search_text": SearchTextArguments, "write_file": WriteFileArguments,
    "run_cli": RunCliArguments, "run_powershell": RunPowerShellArguments,
    "spawn_subagent": SpawnSubagentArguments, "delegate_task": DelegateTaskArguments,
    "ask_user_question": AskUserQuestionArguments, "submit_plan": SubmitPlanArguments,
    "request_scope_expansion": RequestScopeExpansionArguments,
}


def validate_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    model = TOOL_ARGUMENT_MODELS.get(name)
    if model is None:
        return arguments
    try:
        validated = model.model_validate(arguments)
    except ValidationError as exc:
        issues = [{"loc": ".".join(str(part) for part in error["loc"]), "type": error["type"], "message": error["msg"]} for error in exc.errors(include_input=False, include_url=False)]
        raise ValueError(f"Argumentos inválidos para {name}: {json.dumps(issues, ensure_ascii=False)}") from exc
    return validated.model_dump(exclude_none=True)


def parse_manual_spawn_options(value: str) -> tuple[str, bool, str]:
    """Parse operator-owned `/spawn` options; writer is the explicit default."""
    body = value.strip()
    allow_mutation = True
    profile = ""
    read_only_flags = ("--read-only", "--readonly", "--read")
    while body.startswith("--"):
        lowered = body.lower()
        read_only = next(
            (flag for flag in read_only_flags if lowered == flag or lowered.startswith(f"{flag} ")),
            None,
        )
        if read_only:
            allow_mutation = False
            body = body[len(read_only):].strip()
            continue
        if lowered == "--write" or lowered.startswith("--write "):
            allow_mutation = True
            body = body[len("--write"):].strip()
            continue
        profile_match = re.match(
            r"(?is)^--profile(?:=|\s+)([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s+|$)(.*)$",
            body,
        )
        if profile_match:
            profile = profile_match.group(1).lower()
            body = profile_match.group(2).strip()
            continue
        break
    return body, allow_mutation, profile


__all__ = [name for name in globals() if name.endswith("Arguments") or name.startswith("MAX_")] + ["TOOL_ARGUMENT_MODELS", "parse_manual_spawn_options", "validate_tool_arguments"]
