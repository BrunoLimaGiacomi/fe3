from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Message = dict[str, Any]


class ModelCapabilityError(RuntimeError):
    """Uma chamada tentou depender de capability não comprovada."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Chamada de ferramenta normalizada, sem tipos específicos do provider."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Resposta mínima consumida pelo runtime do AgenteGlobal."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None


ModelStreamEventType = Literal[
    "first_token",
    "text_delta",
    "tool_call_started",
    "tool_call_completed",
    "completed",
]


@dataclass(frozen=True, slots=True)
class ModelStreamEvent:
    """Evento normalizado de streaming, independente do SDK do provider.

    ``reasoning_content`` nunca e representado neste contrato.
    """

    type: ModelStreamEventType
    text: str = ""
    tool_call: ToolCall | None = None
    response: ModelResponse | None = None
    first_token_latency_ms: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    """Resultado básico para futuras fronteiras assíncronas do runtime."""

    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
