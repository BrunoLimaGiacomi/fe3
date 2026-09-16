"""Bounded formatting and best-effort secret redaction utilities."""

from __future__ import annotations

import re
import os


def truncate_text(value: str, limit: int = 12_000) -> str:
    return value if len(value) <= limit else value[:limit] + f"\n... saída truncada em {limit} caracteres ..."


def truncate_single_line(value: str, limit: int = 220) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def is_sensitive_key_name(value: str) -> bool:
    lowered = value.lower().strip("-_/ ")
    return any(
        term in lowered
        for term in ("key", "token", "secret", "password", "passwd", "credential", "authorization", "cookie")
    )


def redact_cli_args(args: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        if "=" in arg:
            name, _ = arg.split("=", 1)
            redacted.append(f"{name}=[REDACTED]" if is_sensitive_key_name(name) else arg)
            continue
        redacted.append("[REDACTED]" if is_sensitive_key_name(arg) and not arg.startswith("-") else arg)
        if arg.startswith("-") and is_sensitive_key_name(arg):
            redact_next = True
    return redacted


def redact_command_text(command: str) -> str:
    redacted = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[^\s'\";]+", r"\1[REDACTED]", command)
    return re.sub(
        r"(?i)(\b(?:api[_-]?key|access[_-]?key|secret|token|password|passwd|credential|cookie)\b\s*[=:]\s*)('[^']*'|\"[^\"]*\"|[^\s;,\)]+)",
        r"\1[REDACTED]",
        redacted,
    )


def redact_sensitive_text(value: str) -> str:
    text = str(value)
    text = re.sub(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY BLOCK]",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[^\s`'\"<>]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)\bbearer\s+[a-z0-9._~+/\-=]{12,}", "Bearer [REDACTED]", text)
    text = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "AKIA[REDACTED]", text)
    text = re.sub(r"\bAIza[0-9A-Za-z\-_]{35}\b", "AIza[REDACTED]", text)
    text = re.sub(
        r"(?im)^(\s*[\w.$:-]*(?:api[_-]?key|apikey|access[_-]?key|secret|token|password|passwd|credential|authorization|private[_-]?key|cookie)[\w.$:-]*\s*[=:]\s*)(.+)$",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)(\b(?:api[_-]?key|apikey|access[_-]?key|secret|token|password|passwd|credential|authorization|private[_-]?key|cookie)\b\s*[=:]\s*)('[^']*'|\"[^\"]*\"|[^\s,;]+)",
        r"\1[REDACTED]",
        text,
    )
    return re.sub(
        r'(?i)("(?:api[_-]?key|apikey|access[_-]?key|secret|token|password|passwd|credential|authorization|private[_-]?key|cookie|set-cookie)"\s*:\s*)("(?:\\.|[^"\\])*"|[^,}\s]+)',
        r'\1"[REDACTED]"',
        text,
    )


def safe_subprocess_env(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Copy an environment without common credential-bearing variables."""

    sensitive_terms = (
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "CREDENTIAL",
        "AUTHORIZATION",
        "COOKIE",
    )
    source = os.environ if environment is None else environment
    return {
        key: value
        for key, value in source.items()
        if not any(term in key.upper() for term in sensitive_terms)
    }


def exception_chain_summary(exc: BaseException) -> str:
    chain: list[str] = []
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited and len(chain) < 5:
        visited.add(id(current))
        message = redact_sensitive_text(truncate_single_line(str(current), limit=500))
        chain.append(f"{current.__class__.__name__}: {message or '(sem detalhe)'}")
        current = current.__cause__ or current.__context__
    return " -> ".join(chain)


__all__ = [
    "exception_chain_summary",
    "is_sensitive_key_name",
    "redact_cli_args",
    "redact_command_text",
    "redact_sensitive_text",
    "safe_subprocess_env",
    "truncate_single_line",
    "truncate_text",
]
