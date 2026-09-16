"""Per-command cloud target resolution and lifecycle safety.

The CLI is intentionally provider-neutral at its public boundary.  This module
only adds a small safety layer for cloud CLIs: scope is resolved for each
command, never stored as one process-wide project.  No provider API is called
here; the process runner remains responsible for execution.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


class CloudScopeError(ValueError):
    """Raised when a cloud command has an unsafe or ambiguous target."""


@dataclass(frozen=True, slots=True)
class CloudScope:
    """Resolved provider scope for one command."""

    provider: str | None = None
    project: str | None = None
    account: str | None = None
    region: str | None = None
    source: str = "none"

    @property
    def key(self) -> str:
        """Stable scope key used for lifecycle tracking, without secrets."""

        values = (self.project, self.account, self.region)
        return ":".join(value.casefold() if value else "" for value in values)


@dataclass(frozen=True, slots=True)
class CloudCommand:
    """Assessment and normalized argv for one cloud CLI invocation."""

    cli: str
    provider: str | None
    args: tuple[str, ...]
    scope: CloudScope
    action: str | None = None
    resource_type: str | None = None
    target: str | None = None
    target_normalized: str | None = None
    mutating: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def is_cloud(self) -> bool:
        return self.provider is not None

    @property
    def project(self) -> str | None:
        return self.scope.project

    @property
    def account(self) -> str | None:
        return self.scope.account

    @property
    def region(self) -> str | None:
        return self.scope.region


@dataclass(slots=True)
class _LifecycleRecord:
    targets: set[str] = field(default_factory=set)


_CLI_PROVIDER = {
    "gcloud": "gcp",
    "aws": "aws",
    "az": "azure",
    "hcloud": "hcloud",
}

_SCOPE_FLAGS: dict[str, dict[str, tuple[str, ...]]] = {
    "gcp": {
        "project": ("--project", "-p"),
        "account": ("--account",),
        "region": ("--region",),
    },
    "aws": {
        "account": ("--profile", "--account-id"),
        "region": ("--region",),
    },
    "azure": {
        "project": ("--subscription",),
        "account": ("--subscription", "--tenant"),
        "region": ("--location", "--region"),
    },
    "hcloud": {
        "account": ("--context",),
        "region": ("--location",),
    },
}

_CONTEXT_KEYS = {
    "project": "project",
    "project_id": "project",
    "gcp_project": "project",
    "projeto": "project",
    "account": "account",
    "account_id": "account",
    "aws_account": "account",
    "subscription": "project",
    "subscription_id": "project",
    "tenant": "account",
    "tenant_id": "account",
    "profile": "account",
    "aws_profile": "account",
    "region": "region",
    "location": "region",
    "regiao": "region",
}

_UNSAFE_TARGET_CHARS = re.compile(r"[\x00-\x1f\x7f\s;|&<>$`!]")
_SAFE_SCOPE_VALUE = re.compile(r"^[^\x00-\x1f\x7f\s;|&<>$`!/\\]+$")
_ACTION = re.compile(
    r"^(?:create|new|provision|describe|show|get|list|ls|delete|del|rm|remove|destroy|"
    r"deploy|update|patch|set|add|attach|detach|start|stop|restart|apply|"
    r"enable|disable|copy|move|cp|sync|publish|send|upload|put|post|ack|"
    r"grant|revoke|bind|unbind|mb|rb|import|restore|cancel|flush|purge|"
    r"download|receive|consume|pull|export|watch|head)(?:[-_].*)?$",
    re.IGNORECASE,
)
_MUTATING_ACTION = re.compile(
    r"^(?:create|new|provision|delete|del|rm|remove|destroy|deploy|update|patch|set|"
    r"add|attach|detach|start|stop|restart|apply|enable|disable|copy|move|"
    r"publish|send|upload|put|post|ack|grant|revoke|bind|unbind|mb|rb|"
    r"import|restore|cancel|flush|purge|sync|cp)"
    r"(?:[-_].*)?$",
    re.IGNORECASE,
)
_TARGET_FLAGS = frozenset(
    {
        "--name",
        "--bucket",
        "--instance",
        "--resource",
        "--resource-name",
        "--resource-group",
        "--server",
        "--cluster",
        "--id",
    }
)
_OPTION_WITH_VALUE = frozenset(
    {
        "--project",
        "-p",
        "--account",
        "--profile",
        "--account-id",
        "--region",
        "--subscription",
        "--tenant",
        "--location",
        "--context",
        "--zone",
        "--format",
        "--filter",
        "--sort-by",
        "--impersonate-service-account",
        "--configuration",
        "--message",
        "--body",
        "--file",
        "--data",
        "--content",
        "--copy-source",
        "--source",
        "--destination",
        "--key",
        "--queue-url",
        "--account-name",
        "--container-name",
    }
)


def _cli_basename(cli: str) -> str:
    return cli.strip().rsplit("\\", 1)[-1].rsplit("/", 1)[-1].casefold()


def _provider_for_cli(cli: str) -> str | None:
    basename = _cli_basename(cli)
    for suffix in (".exe", ".cmd", ".bat"):
        basename = basename.removesuffix(suffix)
    return _CLI_PROVIDER.get(basename)


def is_cloud_command_mutating(command: str) -> bool:
    """Best-effort detection for shell text, used only to prevent a bypass.

    Complex shell text is deliberately not rewritten here.  A direct cloud
    mutation must use ``run_cli`` where argv and scope can be validated.
    """

    if not isinstance(command, str):
        return False
    for segment in _shell_segments(command):
        match = re.match(r"(?is)^\s*(?:&\s*)?([^\s]+)(?:\s+|$)", segment)
        if match is None or _provider_for_cli(match.group(1).strip("\"'")) is None:
            continue
        words = re.findall(r"(?:[^\s\"']+|\"[^\"]*\"|'[^']*')", segment)
        if _command_is_mutating(_provider_for_cli(match.group(1).strip("\"'")) or "", words[1:]):
            return True
    return False


def _shell_segments(command: str) -> tuple[str, ...]:
    """Split simple PowerShell separators without splitting quoted values."""

    segments: list[str] = []
    start = 0
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if char in {"'", '"'}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            index += 1
            continue
        if quote is None and (char in {";", "|"} or command[index : index + 2] == "&&"):
            segments.append(command[start:index])
            if index + 1 < len(command) and command[index : index + 2] in {"||", "&&"}:
                index += 2
            else:
                index += 1
            start = index
            continue
        index += 1
    segments.append(command[start:])
    return tuple(segments)


def _clean_value(value: Any, *, kind: str) -> str:
    if not isinstance(value, str):
        raise CloudScopeError(f"Valor de {kind} precisa ser texto.")
    cleaned = value.strip().strip("\"'")
    if not cleaned:
        raise CloudScopeError(f"Valor de {kind} não pode ser vazio.")
    if not _SAFE_SCOPE_VALUE.fullmatch(cleaned):
        raise CloudScopeError(f"Valor de {kind} contém caracteres inválidos.")
    return cleaned


def _canonical_scope_value(value: str | None) -> str | None:
    return value.casefold() if value else None


def _as_values(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _context_mapping(context: str | Mapping[str, Any] | None) -> Mapping[str, Any]:
    if context is None:
        return {}
    if isinstance(context, Mapping):
        return context
    if not isinstance(context, str):
        raise CloudScopeError("Contexto cloud precisa ser texto ou objeto.")
    body = context.strip()
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        return parsed
    return {"_text": body}


def _text_context_values(text: str) -> dict[str, list[str]]:
    """Extract labelled scope values from natural language/task context."""

    values: dict[str, list[str]] = {"project": [], "account": [], "region": []}
    labels = {
        "project": r"project(?:_id)?|projeto(?:s)?|gcp_project|subscription(?:_id)?",
        "account": r"account(?:_id)?|conta|aws_account|profile|tenant(?:_id)?",
        "region": r"region|região|regiao|location|localização|localizacao",
    }
    value_pattern = r"([^,;\n]+?)"
    for kind, label in labels.items():
        pattern = re.compile(
            rf"(?i)(?:\b(?:{label})\b)\s*(?:=|:|is|é|e|de|da|do)?\s*"
            rf"[`\"']?([A-Za-z0-9][A-Za-z0-9._:-]*)[`\"']?"
        )
        for match in pattern.finditer(text):
            candidate = match.group(1).rstrip(".,")
            if candidate.casefold() not in values[kind]:
                values[kind].append(candidate)

    # Also understand "projetos cybersec-qa e uolcs-audit".  The labelled
    # regex above intentionally captures only one token to avoid swallowing a
    # sentence; this bounded pass handles an explicit list of project ids.
    list_pattern = re.compile(
        r"(?i)\bprojetos?\b\s*(?:=|:|são|sao|de)?\s*"
        r"([A-Za-z0-9][A-Za-z0-9._-]*(?:\s*(?:,|/|\be\b|\band\b|\bvs\b)\s*"
        r"(?!project\b|projeto\b|region\b|regi(?:ão|ao)\b|account\b|conta\b)"
        r"[A-Za-z0-9][A-Za-z0-9._-]*)*)"
    )
    for match in list_pattern.finditer(text):
        for candidate in re.split(r"\s*(?:,|/|\be\b|\band\b|\bvs\b)\s*", match.group(1), flags=re.IGNORECASE):
            candidate = candidate.strip()
            if candidate and candidate.casefold() not in values["project"]:
                values["project"].append(candidate)
    return values


def _context_scope(context: str | Mapping[str, Any] | None) -> tuple[dict[str, list[str]], bool]:
    mapping = _context_mapping(context)
    values: dict[str, list[str]] = {"project": [], "account": [], "region": []}
    for raw_key, raw_value in mapping.items():
        key = str(raw_key).strip().casefold().replace("-", "_")
        if key == "_text":
            text_values = _text_context_values(str(raw_value))
            for kind, entries in text_values.items():
                values[kind].extend(entry for entry in entries if entry.casefold() not in values[kind])
            continue
        # An Azure subscription is both the user-facing project-like target
        # and the account boundary that must be injected into argv.
        if key in {"subscription", "subscription_id"}:
            for item in _as_values(raw_value):
                cleaned = _clean_value(item, kind="subscription")
                for kind in ("project", "account"):
                    if cleaned.casefold() not in {entry.casefold() for entry in values[kind]}:
                        values[kind].append(cleaned)
            continue
        kind = _CONTEXT_KEYS.get(key)
        if kind is None:
            continue
        for item in _as_values(raw_value):
            cleaned = _clean_value(item, kind=kind)
            if cleaned.casefold() not in {entry.casefold() for entry in values[kind]}:
                values[kind].append(cleaned)
    return values, any(values.values())


def _ambient_scope() -> dict[str, str | None]:
    """Read optional ambient metadata only for unscoped read diagnostics."""

    return {
        "project": os.getenv("CLOUDSDK_CORE_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT"),
        "account": os.getenv("AWS_PROFILE") or os.getenv("AZURE_SUBSCRIPTION_ID"),
        "region": os.getenv("CLOUDSDK_COMPUTE_REGION") or os.getenv("AWS_REGION") or os.getenv("AZURE_LOCATION"),
    }


def _flag_value(args: Sequence[str], flags: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return values and indexes consumed by one option family."""

    values: list[str] = []
    consumed: list[str] = []
    flag_set = set(flags)
    index = 0
    while index < len(args):
        arg = args[index]
        matched = None
        for flag in flags:
            if arg == flag:
                matched = flag
                if index + 1 >= len(args):
                    raise CloudScopeError(f"A opção {flag} exige um valor.")
                values.append(_clean_value(args[index + 1], kind=flag))
                consumed.extend([flag, args[index + 1]])
                index += 2
                break
            if arg.startswith(f"{flag}="):
                matched = flag
                values.append(_clean_value(arg[len(flag) + 1 :], kind=flag))
                consumed.append(arg)
                index += 1
                break
        if matched is None:
            index += 1
    return values, consumed


def _unique_scope(values: Sequence[str], kind: str) -> str | None:
    if not values:
        return None
    normalized: dict[str, str] = {}
    for value in values:
        normalized.setdefault(value.casefold(), value)
    if len(normalized) > 1:
        raise CloudScopeError(f"Comando contém valores conflitantes para {kind}: {', '.join(normalized.values())}.")
    return next(iter(normalized.values()))


def _action_tokens(args: Sequence[str]) -> tuple[int | None, str | None, list[str]]:
    positional: list[tuple[int, str]] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            positional.extend((offset, value) for offset, value in enumerate(args[index + 1 :], index + 1))
            break
        if arg.startswith("-"):
            if "=" not in arg and arg in _OPTION_WITH_VALUE:
                index += 2
            else:
                index += 1
            continue
        positional.append((index, arg))
        index += 1
    for position, token in positional:
        if _ACTION.fullmatch(token):
            return position, token.casefold(), [item for _, item in positional]
    return None, None, [item for _, item in positional]


def _resource_details(provider: str, args: Sequence[str]) -> tuple[str | None, str | None, str | None]:
    action_index, action, positional = _action_tokens(args)
    resource_type: str | None = None
    if action_index is not None:
        before = [
            token
            for index, token in enumerate(args)
            if index < action_index and not token.startswith("-")
        ]
        if before:
            resource_type = before[-1].casefold()

    target: str | None = None
    for index, arg in enumerate(args):
        if arg in _TARGET_FLAGS:
            if index + 1 < len(args):
                target = args[index + 1]
                break
        if any(arg.startswith(f"{flag}=") for flag in _TARGET_FLAGS):
            target = arg.split("=", 1)[1]
            break
    if target is None and action_index is not None:
        # The first positional after the action is the resource in gcloud and
        # most AWS/Azure subcommands.  For create commands that use only
        # named options this simply remains None.
        positional_after: list[str] = []
        index = action_index + 1
        while index < len(args):
            token = args[index]
            if token == "--":
                positional_after.extend(args[index + 1 :])
                break
            if token.startswith("-"):
                if "=" not in token and token in _OPTION_WITH_VALUE:
                    index += 2
                else:
                    index += 1
                continue
            positional_after.append(token)
            index += 1
        if positional_after:
            target = positional_after[0]
    if resource_type is None and provider == "gcp" and positional:
        resource_type = positional[-1].casefold()
    normalized = _normalize_target(target, resource_type=resource_type)
    return action, resource_type, normalized


def _positional_after(args: Sequence[str], action_index: int) -> list[str]:
    """Return positional values after an action, ignoring option values."""

    positional: list[str] = []
    index = action_index + 1
    while index < len(args):
        token = args[index]
        if token == "--":
            positional.extend(args[index + 1 :])
            break
        if token.startswith("-"):
            if "=" not in token and token in _OPTION_WITH_VALUE:
                index += 2
            else:
                index += 1
            continue
        positional.append(token)
        index += 1
    return positional


def _is_cloud_target(value: str) -> bool:
    lowered = value.strip("\"'").casefold()
    return lowered.startswith(("gs://", "s3://", "https://"))


def _command_is_mutating(provider: str, args: Sequence[str]) -> bool:
    action_index, action, _ = _action_tokens(args)
    if action is None or not _MUTATING_ACTION.fullmatch(action):
        return False
    if action in {"cp", "copy", "sync"} and action_index is not None:
        positional = _positional_after(args, action_index)
        if len(positional) >= 2 and _is_cloud_target(positional[0]) and not _is_cloud_target(positional[1]):
            return False
    return True


def _normalize_target(target: str | None, *, resource_type: str | None) -> str | None:
    if target is None:
        return None
    cleaned = target.strip().strip("\"'").rstrip(".,")
    if not cleaned:
        raise CloudScopeError("Target cloud não pode ser vazio.")
    if _UNSAFE_TARGET_CHARS.search(cleaned):
        raise CloudScopeError(f"Target cloud inválido ou ambíguo: {target!r}.")
    if cleaned.casefold().startswith("gs://"):
        prefix, _, bucket = cleaned.partition("://")
        if not bucket:
            raise CloudScopeError(f"Target de bucket não pode ser vazio: {target!r}.")
        if "/" in bucket and resource_type in {"bucket", "buckets"}:
            raise CloudScopeError(f"Target de bucket deve identificar somente o bucket: {target!r}.")
        return f"{prefix.casefold()}://{bucket.casefold()}"
    if resource_type in {"bucket", "buckets"}:
        return cleaned.casefold()
    return cleaned


def _replace_gcs_target(args: Sequence[str], target: str | None, normalized: str | None) -> tuple[str, ...]:
    if target is None or normalized is None or target == normalized or not target.casefold().startswith("gs://"):
        return tuple(args)
    result = list(args)
    for index, value in enumerate(result):
        if value == target:
            result[index] = normalized
            break
        if value.startswith("--") and value.split("=", 1)[-1] == target:
            result[index] = f"{value.split('=', 1)[0]}={normalized}"
            break
    return tuple(result)


class CloudScopeResolver:
    """Resolve and validate one cloud command at a time.

    ``ambient_scope`` is intentionally diagnostic-only: it can satisfy a read
    when the caller supplied no requested scope, but it can never authorize a
    mutation or override an explicit/contextual project.
    """

    def __init__(self, ambient_scope: Mapping[str, Any] | None = None) -> None:
        raw_ambient = dict(ambient_scope) if ambient_scope is not None else _ambient_scope()
        self._ambient = {
            key: (str(value).strip() if value is not None and str(value).strip() else None)
            for key, value in raw_ambient.items()
            if key in {"project", "account", "region"}
        }
        for key in ("project", "account", "region"):
            self._ambient.setdefault(key, None)
        self._lifecycle: dict[tuple[str, str, str, str], _LifecycleRecord] = {}

    @property
    def lifecycle(self) -> Mapping[tuple[str, str, str, str], tuple[str, ...]]:
        """Read-only snapshot useful to tests and local diagnostics."""

        return {key: tuple(sorted(record.targets)) for key, record in self._lifecycle.items()}

    @staticmethod
    def _context_fingerprint(context: str | Mapping[str, Any] | None) -> str:
        if context is None or context == "":
            return "none"
        if isinstance(context, Mapping):
            bounded = json.dumps(dict(context), sort_keys=True, default=str, ensure_ascii=False)[:16_000]
        else:
            bounded = str(context)[:16_000]
        return hashlib.sha256(bounded.encode("utf-8")).hexdigest()[:24]

    def resolve(
        self,
        cli: str,
        args: Sequence[str] | None = None,
        *,
        context: str | Mapping[str, Any] | None = None,
    ) -> CloudCommand:
        raw_args = tuple(args or ())
        if not isinstance(cli, str) or not cli.strip():
            raise CloudScopeError("Nome da CLI não pode ser vazio.")
        if not all(isinstance(item, str) for item in raw_args):
            raise CloudScopeError("Argumentos cloud precisam ser strings.")
        provider = _provider_for_cli(cli)
        if provider is None:
            return CloudCommand(cli=cli, provider=None, args=raw_args, scope=CloudScope())

        context_values, context_present = _context_scope(context)
        explicit_values = {"project": [], "account": [], "region": []}
        normalized_args: list[str] = []
        flags = _SCOPE_FLAGS[provider]
        index = 0
        while index < len(raw_args):
            arg = raw_args[index]
            matched: tuple[str, str] | None = None
            for kind, family in flags.items():
                for flag in family:
                    if arg == flag:
                        if index + 1 >= len(raw_args):
                            raise CloudScopeError(f"A opção {flag} exige um valor.")
                        matched = (kind, _clean_value(raw_args[index + 1], kind=flag))
                        index += 2
                        break
                    if arg.startswith(f"{flag}="):
                        matched = (kind, _clean_value(arg[len(flag) + 1 :], kind=flag))
                        index += 1
                        break
                if matched is not None:
                    break
            if matched is None:
                normalized_args.append(arg)
                index += 1
                continue
            kind, value = matched
            explicit_values[kind].append(value)
            normalized_args.append(f"{flags[kind][0]}={value}")

        explicit = {kind: _unique_scope(values, kind) for kind, values in explicit_values.items()}
        selected: dict[str, str | None] = {}
        warnings: list[str] = []
        for kind in ("project", "account", "region"):
            requested = context_values[kind]
            if explicit[kind] is not None and requested and explicit[kind].casefold() not in {item.casefold() for item in requested}:
                raise CloudScopeError(
                    f"Escopo explícito de {kind} ({explicit[kind]}) diverge do contexto solicitado "
                    f"({', '.join(requested)})."
                )
            if len(requested) > 1 and explicit[kind] is None:
                raise CloudScopeError(
                    f"Contexto cloud ambíguo: informe {kind} explicitamente; candidatos: {', '.join(requested)}."
                )
            selected[kind] = explicit[kind] or (requested[0] if requested else self._ambient.get(kind))
            if selected[kind] is not None:
                selected[kind] = _clean_value(selected[kind], kind=kind)

        source = "explicit" if any(explicit.values()) else "context" if context_present else "ambient" if any(selected.values()) else "none"
        scope = CloudScope(
            provider=provider,
            project=selected["project"],
            account=selected["account"],
            region=selected["region"],
            source=source,
        )
        action, resource_type, target = _resource_details(provider, normalized_args)
        target_raw = None
        if target is not None:
            # Resolve raw spelling only for reporting/argv normalization.
            for item in raw_args:
                if _normalize_target(item, resource_type=resource_type) == target:
                    target_raw = item
                    break
            target_raw = target_raw or target
        mutating = _command_is_mutating(provider, normalized_args)
        if mutating and provider in {"gcp", "aws", "azure", "hcloud"}:
            required_kind = "project" if provider in {"gcp", "azure"} else "account"
            if not (explicit[required_kind] or context_values[required_kind]):
                raise CloudScopeError(
                    f"Mutação {provider} exige {required_kind} explícito ou contexto inequívoco; "
                    "não use o ambiente padrão silenciosamente."
                )
        if context_present and not any(context_values.values()):
            raise CloudScopeError("Contexto cloud fornecido não contém projeto, conta ou região inequívocos.")

        if context_values["project"] and explicit["project"] is None and provider == "gcp":
            normalized_args.append(f"--project={context_values['project'][0]}")
        if context_values["account"] and explicit["account"] is None:
            flag = "--profile" if provider == "aws" else "--account" if provider == "gcp" else "--subscription" if provider == "azure" else "--context"
            normalized_args.append(f"{flag}={context_values['account'][0]}")
        if context_values["region"] and not explicit["region"]:
            flag = "--location" if provider == "azure" else "--region"
            normalized_args.append(f"{flag}={context_values['region'][0]}")

        normalized_target = _normalize_target(target_raw, resource_type=resource_type)
        normalized_args_tuple = _replace_gcs_target(normalized_args, target_raw, normalized_target)
        if source == "ambient" and not mutating:
            warnings.append("Escopo de leitura veio do ambiente; informe projeto/conta para uma operação reprodutível.")
        return CloudCommand(
            cli=cli,
            provider=provider,
            args=normalized_args_tuple,
            scope=scope,
            action=action,
            resource_type=resource_type,
            target=target_raw,
            target_normalized=normalized_target,
            mutating=mutating,
            warnings=tuple(warnings),
        )

    def prepare(
        self,
        cli: str,
        args: Sequence[str] | None = None,
        *,
        context: str | Mapping[str, Any] | None = None,
    ) -> CloudCommand:
        """Resolve, validate and record one command's lifecycle intent."""

        command = self.resolve(cli, args, context=context)
        if command.provider is None or command.target_normalized is None:
            return command
        scope_key = command.scope.key
        resource_key = (
            command.provider,
            scope_key,
            command.resource_type or "resource",
            self._context_fingerprint(context),
        )
        record = self._lifecycle.setdefault(resource_key, _LifecycleRecord())
        action = command.action or ""
        if action in {"describe", "show", "get", "update", "patch", "delete", "remove", "destroy"}:
            if record.targets and command.target_normalized not in record.targets:
                expected = ", ".join(sorted(record.targets))
                raise CloudScopeError(
                    f"Possível drift de nome no lifecycle: alvo {command.target_normalized!r} "
                    f"não corresponde ao alvo registrado ({expected}) em {scope_key or 'escopo atual'}."
                )
            if action in {"delete", "remove", "destroy"}:
                record.targets.discard(command.target_normalized)
                if not record.targets:
                    self._lifecycle.pop(resource_key, None)
        elif action in {"create", "new", "provision", "deploy"}:
            record.targets.add(command.target_normalized)
        return command


def normalize_gcloud_args(args: Sequence[str], *, context_project: str | None = None) -> tuple[str, ...]:
    """Canonicalize ``--project value`` and ``--project=value`` forms."""

    resolver = CloudScopeResolver(ambient_scope={})
    context = {"project": context_project} if context_project else None
    return resolver.resolve("gcloud", args, context=context).args


__all__ = [
    "CloudCommand",
    "CloudScope",
    "CloudScopeError",
    "CloudScopeResolver",
    "is_cloud_command_mutating",
    "normalize_gcloud_args",
]
