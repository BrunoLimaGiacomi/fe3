from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MODEL = "glm-5.2"
DEFAULT_MODEL_ALIASES = {
    "primary": "maas-current",
    "default": "maas-current",
    "subagent": "maas-current",
    "maas-current": DEFAULT_MODEL,
    "glm-current": DEFAULT_MODEL,
}
MAX_MODEL_ALIAS_FILE_BYTES = 20_000
MAX_MODEL_ALIAS_DEPTH = 10


@dataclass(frozen=True)
class AgentConfig:
    workspace: Path
    api_key_file: Path
    model_alias_file: Path
    agents_file: Path
    skills_dir: Path
    profiles_dir: Path
    read_scope: str
    write_scope: str
    load_project_context: bool
    history_limit: int
    allow_shell: bool
    allow_sensitive_read: bool
    permission_mode: str
    verbosity_mode: str
    api_timeout_seconds: float
    api_retries: int
    max_steps: int
    max_subagents: int
    subagent_max_steps: int
    subagent_timeout_seconds: int = 1_800
    # Read-only files shipped with AgenteGlobal live in a different path
    # domain from the operator's tool workspace.
    resource_root: Path | None = None
    user_profile_id: str = "global"
    user_profile_what: str = ""
    user_profile_criteria: tuple[str, ...] = ()
    user_profile_skills: tuple[str, ...] = ()


def load_model_aliases(alias_file: Path) -> dict[str, str]:
    aliases = dict(DEFAULT_MODEL_ALIASES)
    if not alias_file.exists():
        return aliases
    if not alias_file.is_file():
        raise ValueError(f"O caminho de aliases não é um arquivo: {alias_file}")
    if alias_file.stat().st_size > MAX_MODEL_ALIAS_FILE_BYTES:
        raise ValueError(f"Arquivo de aliases maior que {MAX_MODEL_ALIAS_FILE_BYTES} bytes: {alias_file}")

    raw = json.loads(alias_file.read_text(encoding="utf-8-sig"))
    if isinstance(raw, dict) and isinstance(raw.get("aliases"), dict):
        raw = raw["aliases"]
    if not isinstance(raw, dict):
        raise ValueError("Arquivo de aliases precisa ser um objeto JSON ou conter a chave 'aliases'.")

    for alias, target in raw.items():
        if not isinstance(alias, str) or not isinstance(target, str):
            raise ValueError("Aliases de modelo precisam mapear string para string.")
        alias = alias.strip()
        target = target.strip()
        if not alias or not target:
            raise ValueError("Alias de modelo e destino não podem ser vazios.")
        aliases[alias] = target
    return aliases


def resolve_model_name(
    direct_model: str | None,
    alias_name: str,
    alias_file: Path,
    *,
    model_env_name: str = "HUAWEI_MAAS_MODEL",
    alias_env_name: str = "HUAWEI_MAAS_MODEL_ALIAS",
) -> tuple[str, str]:
    aliases = load_model_aliases(alias_file)
    configured_model = (direct_model or "").strip()
    if configured_model:
        current = configured_model
        source = f"--model/{model_env_name}"
    else:
        current = alias_name.strip()
        source = f"--model-alias/{alias_env_name}"

    if not current:
        raise ValueError("Modelo ou alias de modelo não pode ser vazio.")

    chain = [current]
    for _ in range(MAX_MODEL_ALIAS_DEPTH):
        target = aliases.get(current)
        if not target:
            return current, f"{source}: {' -> '.join(chain)}"
        current = target.strip()
        if current in chain:
            chain.append(current)
            raise ValueError(f"Ciclo detectado nos aliases de modelo: {' -> '.join(chain)}")
        chain.append(current)
    raise ValueError(f"Alias de modelo excede profundidade máxima de {MAX_MODEL_ALIAS_DEPTH}: {' -> '.join(chain)}")
