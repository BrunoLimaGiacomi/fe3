"""Classification helpers for sensitive paths and mutating commands."""

from __future__ import annotations

import re
from pathlib import Path


SENSITIVE_NAME_PATTERNS = (
    "agenta", "cred", "credential", "credentials", "secret", "token",
    "apikey", "api_key", "password", "passwd", "private_key", ".env",
)
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
DESTRUCTIVE_COMMAND_PATTERNS = (
    r"\bRemove-Item\b", r"\brm\b", r"\bdel\b", r"\brmdir\b", r"\bFormat-",
    r"\bshutdown\b", r"\brestart-computer\b", r"\bStop-Computer\b",
    r"\bSet-ExecutionPolicy\b", r"\breg\s+delete\b", r"\baz\b.*\bdelete\b",
    r"\bgcloud\b.*\bdelete\b", r"\baws\b.*\bdelete\b", r"\bhcloud\b.*\bdelete\b",
)
MUTATING_COMMAND_PATTERNS = (
    r"\bapply\b", r"\bcreate\b", r"\bdeploy\b", r"\bdestroy\b", r"\bdelete\b",
    r"\bremove\b", r"\bset\b", r"\bupdate\b", r"\bupgrade\b", r"\bpatch\b",
    r"\bput\b", r"\bpost\b", r"\battach\b", r"\bdetach\b", r"\bstart\b",
    r"\bstop\b", r"\brestart\b", r"\bterminate\b", r"\badd-iam-policy-binding\b",
    r"\bremove-iam-policy-binding\b", r"\brole assignment\b.*\bcreate\b", r"\biam\b.*\bput-\b",
)
COMMON_CLI_NAMES = frozenset(
    {
        "aws", "az", "gcloud", "hcloud", "kubectl", "terraform", "tofu", "helm",
        "docker", "git", "gh", "python", "py", "pip", "node", "npm", "npx",
        "powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe",
    }
)


def is_sensitive_path(path: Path) -> bool:
    lowered_parts = [part.lower() for part in path.parts]
    return path.suffix.lower() in SENSITIVE_SUFFIXES or any(
        pattern in part for part in lowered_parts for pattern in SENSITIVE_NAME_PATTERNS
    )


def is_destructive_command(command: str) -> bool:
    return any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in DESTRUCTIVE_COMMAND_PATTERNS)


def is_mutating_command(command: str) -> bool:
    return any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in MUTATING_COMMAND_PATTERNS)


def is_unsafe_command(command: str, cli_name: str | None = None) -> bool:
    return is_destructive_command(command) or is_mutating_command(command) or bool(
        cli_name and cli_name.lower() not in COMMON_CLI_NAMES
    )


__all__ = ["is_destructive_command", "is_mutating_command", "is_sensitive_path", "is_unsafe_command"]
