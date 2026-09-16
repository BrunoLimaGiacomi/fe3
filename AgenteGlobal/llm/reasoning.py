from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReasoningMode(StrEnum):
    NORMAL = "normal"
    MAX_ONLY = "max_only"
    DEEP = "deep"


@dataclass(frozen=True, slots=True)
class ReasoningSettings:
    effort: str
    thinking_enabled: bool


class ReasoningPolicy:
    """Mapeia intenção do runtime para o contrato comprovado do GLM-5.2."""

    _SETTINGS = {
        ReasoningMode.NORMAL: ReasoningSettings(effort="none", thinking_enabled=False),
        ReasoningMode.MAX_ONLY: ReasoningSettings(effort="max", thinking_enabled=False),
        ReasoningMode.DEEP: ReasoningSettings(effort="max", thinking_enabled=True),
    }

    def resolve(self, mode: ReasoningMode) -> ReasoningSettings:
        try:
            return self._SETTINGS[ReasoningMode(mode)]
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Modo de reasoning não suportado: {mode}") from exc


DEFAULT_REASONING_POLICY = ReasoningPolicy()
