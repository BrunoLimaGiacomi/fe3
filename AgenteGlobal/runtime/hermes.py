"""Optional, non-orchestrating adapter for the Hermes skills CLI.

Hermes is treated as an external skill catalog only.  This adapter never
starts an agent, delegates a task, evaluates a downloaded file, or performs
an install/update without an explicit authorization flag.  All subprocesses
use argument vectors (never a shell), and a runner can be injected for fully
offline tests.
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .policies import PolicyEngine, PolicyRequest, SkillTrust
from .skills import SkillDocument, SkillMetadata


class HermesError(RuntimeError):
    """Base error for Hermes adapter failures."""


class HermesAuthorizationError(PermissionError, HermesError):
    """A mutating Hermes command lacked explicit runtime authorization."""


_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SUPPORTED_READ_OPERATIONS = frozenset({"search", "inspect", "list"})


@dataclass(frozen=True, slots=True)
class HermesStatus:
    """Executable availability without exposing command payloads."""

    available: bool
    binary: str
    version: str | None = None
    reason: str | None = None
    command: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return "available" if self.available else "unavailable"

    @property
    def availability(self) -> str:
        return self.status

    def __bool__(self) -> bool:
        return self.available

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "available": self.available,
            "binary": self.binary,
            "version": self.version,
            "reason": self.reason,
            "command": list(self.command),
        }

    model_dump = to_dict


@dataclass(frozen=True, slots=True)
class HermesResult:
    """Result of one bounded Hermes skills command."""

    operation: str
    status: str
    available: bool
    ok: bool
    command: tuple[str, ...] = ()
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    data: Any = None
    items: tuple[Any, ...] = ()
    error: str | None = None

    @property
    def unavailable(self) -> bool:
        return self.status == "unavailable"

    @property
    def succeeded(self) -> bool:
        return self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "status": self.status,
            "available": self.available,
            "ok": self.ok,
            "command": list(self.command),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "data": self.data,
            "items": list(self.items),
            "error": self.error,
        }

    model_dump = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True, slots=True)
class _RawResult:
    launched: bool
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None


Runner = Callable[..., Any]


def _safe_binary(value: str | Path) -> str:
    binary = str(value).strip()
    if not binary or any(ord(char) < 0x20 for char in binary):
        raise ValueError("Hermes binary must be a non-empty safe command path")
    return binary


def _safe_skill_name(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Hermes skill name must be a string")
    normalized = value.strip().lower()
    if _SKILL_NAME_RE.fullmatch(normalized) is None:
        raise ValueError("Hermes skill name must be a safe identifier")
    return normalized


class HermesAdapter:
    """Small optional wrapper around ``hermes skills`` commands.

    The default runner invokes a local executable only when a caller asks for
    a command.  Tests and embedding runtimes should inject ``runner``; it is
    called with a list of arguments and, when supported, a timeout keyword.
    """

    def __init__(
        self,
        binary: str | Path = "hermes",
        *,
        runner: Runner | None = None,
        timeout_seconds: float = 30.0,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        policy_engine: PolicyEngine | None = None,
        trust: SkillTrust | str = SkillTrust.COMMUNITY,
    ) -> None:
        self.binary = _safe_binary(binary)
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.runner = runner
        self.cwd = None if cwd is None else str(Path(cwd))
        self.env = None if env is None else {str(key): str(value) for key, value in env.items()}
        self.policy_engine = policy_engine or PolicyEngine.with_skill_trust()
        self.trust = SkillTrust(trust)

    def _invoke_runner(self, command: list[str]) -> Any:
        if self.runner is None:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
                timeout=self.timeout_seconds,
                cwd=self.cwd,
                env=self.env,
            )
        runner = self.runner
        try:
            signature = inspect.signature(runner)
            accepts_timeout = "timeout" in signature.parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
        except (TypeError, ValueError):
            accepts_timeout = True
        if accepts_timeout:
            return runner(command, timeout=self.timeout_seconds)
        return runner(command)

    @staticmethod
    def _coerce_result(result: Any) -> _RawResult:
        if isinstance(result, subprocess.CompletedProcess):
            return _RawResult(
                launched=True,
                returncode=result.returncode,
                stdout="" if result.stdout is None else str(result.stdout),
                stderr="" if result.stderr is None else str(result.stderr),
            )
        if isinstance(result, Mapping):
            return _RawResult(
                launched=bool(result.get("launched", True)),
                returncode=result.get("returncode", result.get("code", 0)),
                stdout=str(result.get("stdout", "") or ""),
                stderr=str(result.get("stderr", "") or ""),
                error=str(result.get("error")) if result.get("error") else None,
            )
        if isinstance(result, Sequence) and not isinstance(result, (str, bytes, bytearray)):
            if len(result) < 1:
                raise TypeError("Hermes runner sequence result must include a return code")
            return _RawResult(
                launched=True,
                returncode=result[0],
                stdout="" if len(result) < 2 or result[1] is None else str(result[1]),
                stderr="" if len(result) < 3 or result[2] is None else str(result[2]),
            )
        returncode = getattr(result, "returncode", getattr(result, "code", 0))
        return _RawResult(
            launched=bool(getattr(result, "launched", True)),
            returncode=returncode,
            stdout=str(getattr(result, "stdout", "") or ""),
            stderr=str(getattr(result, "stderr", "") or ""),
            error=str(getattr(result, "error", "")) or None,
        )

    def _invoke(self, arguments: Sequence[str]) -> tuple[tuple[str, ...], _RawResult]:
        command = [self.binary, *(str(argument) for argument in arguments)]
        try:
            raw = self._coerce_result(self._invoke_runner(command))
        except FileNotFoundError:
            raw = _RawResult(False, None, "", "", "Hermes binary was not found")
        except PermissionError as error:
            raw = _RawResult(False, None, "", "", "Hermes binary could not be executed")
        except subprocess.TimeoutExpired:
            raw = _RawResult(True, None, "", "", "Hermes command timed out")
        except OSError as error:
            raw = _RawResult(False, None, "", "", "Hermes command unavailable")
        except (TypeError, ValueError) as error:
            raise HermesError("Hermes runner returned an invalid result") from error
        return tuple(command), raw

    @staticmethod
    def _parse_output(stdout: str) -> tuple[Any, tuple[Any, ...]]:
        text = stdout.strip()
        if not text:
            return None, ()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            lines = tuple(line.strip() for line in text.splitlines() if line.strip())
            return None, lines
        if isinstance(parsed, list):
            return parsed, tuple(parsed)
        if isinstance(parsed, dict):
            values = parsed.get("skills", parsed.get("items", ()))
            return parsed, tuple(values) if isinstance(values, list) else ()
        return parsed, (parsed,)

    def _response(self, operation: str, command: tuple[str, ...], raw: _RawResult) -> HermesResult:
        if not raw.launched:
            status = "unavailable"
            ok = False
        elif raw.returncode == 0:
            status = "ok"
            ok = True
        else:
            status = "error"
            ok = False
        data, items = self._parse_output(raw.stdout)
        return HermesResult(
            operation=operation,
            status=status,
            available=raw.launched,
            ok=ok,
            command=command,
            returncode=raw.returncode,
            stdout=raw.stdout,
            stderr=raw.stderr,
            data=data,
            items=items,
            error=raw.error,
        )

    def status(self) -> HermesStatus:
        command, raw = self._invoke(("--version",))
        if not raw.launched:
            return HermesStatus(False, self.binary, reason=raw.error or raw.stderr or "Hermes unavailable", command=command)
        version_text = (raw.stdout or raw.stderr).strip().splitlines()
        version = version_text[0][:256] if version_text else None
        reason = None if raw.returncode == 0 else (raw.stderr.strip() or raw.error or "Hermes probe failed")
        return HermesStatus(True, self.binary, version=version, reason=reason, command=command)

    detect = status

    def is_available(self) -> bool:
        return self.status().available

    @property
    def available(self) -> bool:
        return self.status().available

    def execute(self, arguments: Sequence[str]) -> HermesResult:
        """Run one explicitly supported read-only ``hermes skills`` command."""

        args = tuple(str(argument) for argument in arguments)
        if len(args) < 2 or args[0] != "skills" or args[1] not in _SUPPORTED_READ_OPERATIONS:
            raise ValueError("HermesAdapter only permits read-only skills commands")
        if args[1] in {"inspect"} and len(args) != 3:
            raise ValueError("hermes skills inspect requires exactly one skill name")
        if args[1] == "search" and len(args) != 3:
            raise ValueError("hermes skills search requires exactly one query")
        return self._response(args[1], *self._invoke(args))

    def search(self, query: str) -> HermesResult:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Hermes search query cannot be empty")
        return self.execute(("skills", "search", query.strip()))

    def inspect(self, name: str) -> HermesResult:
        return self.execute(("skills", "inspect", _safe_skill_name(name)))

    def list_skills(self) -> HermesResult:
        command, raw = self._invoke(("skills",))
        response = self._response("list", command, raw)
        if response.status == "error":
            # Some Hermes releases expose ``skills list`` while others expose
            # the bare ``skills`` command.  A failed first probe is harmless
            # and the fallback remains read-only.
            command, raw = self._invoke(("skills", "list"))
            response = self._response("list", command, raw)
        return response

    list = list_skills

    @staticmethod
    def _metadata_from_item(item: Any) -> SkillMetadata | None:
        """Convert one Hermes L0 result item without interpreting its content."""

        if isinstance(item, str):
            name = item.strip().lower()
            values: Mapping[str, Any] = {}
        elif isinstance(item, Mapping):
            values = item
            name = str(values.get("name", values.get("id", values.get("slug", "")))).strip().lower()
        else:
            return None
        if _SKILL_NAME_RE.fullmatch(name) is None:
            return None
        description = str(values.get("description", "Hermes skill metadata"))[:1_024].strip()
        if not description:
            description = "Hermes skill metadata"

        def as_identifiers(value: Any) -> tuple[str, ...]:
            raw = [value] if isinstance(value, str) else value if isinstance(value, (list, tuple)) else ()
            result: list[str] = []
            for entry in raw:
                candidate = str(entry).strip().lower()
                if re.fullmatch(r"^[a-z0-9][a-z0-9_.:-]{0,63}$", candidate) and candidate not in result:
                    result.append(candidate)
            return tuple(result)

        try:
            return SkillMetadata(
                name=name,
                description=description,
                tags=as_identifiers(values.get("tags")),
                capabilities=as_identifiers(values.get("capabilities")),
                version=str(values.get("version", "unknown"))[:128] or "unknown",
                trust=str(values.get("trust", "unverified"))[:128] or "unverified",
                origin=str(values.get("origin", "hermes"))[:128] or "hermes",
            )
        except (TypeError, ValueError):
            return None

    def discover(self) -> tuple[SkillMetadata, ...]:
        """Expose Hermes listing as an L0 read-only provider catalog."""

        result = self.list_skills()
        if not result.ok:
            return ()
        output = result.items
        if not output and isinstance(result.data, Mapping):
            output = tuple(result.data.get("skills", result.data.get("items", ())))
        metadata: list[SkillMetadata] = []
        for item in output:
            converted = self._metadata_from_item(item)
            if converted is not None and converted.name not in {entry.name for entry in metadata}:
                metadata.append(converted)
        return tuple(sorted(metadata, key=lambda entry: entry.name))

    def load(self, name: str) -> SkillDocument:
        """Select one Hermes skill and return its inert L1 text."""

        normalized = _safe_skill_name(name)
        result = self.inspect(normalized)
        if not result.ok:
            detail = result.error or result.stderr or f"Hermes inspect failed with status {result.status}"
            raise HermesError(detail)
        payload = result.data
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        if isinstance(payload, Mapping):
            raw_content = payload.get("content", payload.get("instructions", payload.get("body", payload.get("skill_md", ""))))
            content = str(raw_content or "")
            metadata = self._metadata_from_item(payload)
        else:
            content = result.stdout
            metadata = None
        if metadata is None:
            metadata = next((item for item in self.discover() if item.name == normalized), None)
        if metadata is None:
            metadata = SkillMetadata(normalized, "Hermes skill metadata", origin="hermes", trust="unverified")
        return SkillDocument(metadata=metadata, content=content, instructions=content)

    @staticmethod
    def _authorized(
        *,
        authorized: bool = False,
        authorize: bool | None = None,
        authorization: bool | None = None,
    ) -> bool:
        values = [authorized]
        if authorize is not None:
            values.append(authorize)
        if authorization is not None:
            values.append(authorization)
        if not any(value is True for value in values):
            raise HermesAuthorizationError("Hermes install/update requires explicit authorization=True")
        return True

    def install(
        self,
        name: str,
        *,
        authorized: bool = False,
        authorize: bool | None = None,
        authorization: bool | None = None,
        validation_passed: bool = False,
    ) -> HermesResult:
        approved = self._authorized(authorized=authorized, authorize=authorize, authorization=authorization)
        normalized = _safe_skill_name(name)
        self.policy_engine.enforce(
            PolicyRequest(
                action="skill.install",
                resource=normalized,
                trust=self.trust,
                approval_granted=approved,
                validation_passed=validation_passed,
            )
        )
        command, raw = self._invoke(("skills", "install", normalized))
        return self._response("install", command, raw)

    def update(
        self,
        name: str | None = None,
        *,
        authorized: bool = False,
        authorize: bool | None = None,
        authorization: bool | None = None,
        validation_passed: bool = False,
    ) -> HermesResult:
        approved = self._authorized(authorized=authorized, authorize=authorize, authorization=authorization)
        normalized = None if name is None else _safe_skill_name(name)
        self.policy_engine.enforce(
            PolicyRequest(
                action="skill.update",
                resource=normalized or "all",
                trust=self.trust,
                approval_granted=approved,
                validation_passed=validation_passed,
            )
        )
        arguments = ("skills", "update") if normalized is None else ("skills", "update", normalized)
        command, raw = self._invoke(arguments)
        return self._response("update", command, raw)


# Names useful to adapters that prefer an explicit provider term.
HermesSkillsAdapter = HermesAdapter
HermesCLI = HermesAdapter


__all__ = [
    "HermesAdapter",
    "HermesAuthorizationError",
    "HermesCLI",
    "HermesError",
    "HermesResult",
    "HermesSkillsAdapter",
    "HermesStatus",
]
