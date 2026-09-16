from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from .capabilities import ModelCapabilities
from .contracts import Message, ModelResponse, ModelStreamEvent
from .reasoning import ReasoningMode


@dataclass(frozen=True, slots=True)
class ModelRequest:
    messages: list[Message]
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: float | None = None
    reasoning_mode: ReasoningMode = ReasoningMode.NORMAL
    response_format: dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    capability_probe: bool = False


@runtime_checkable
class ModelAdapter(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def capabilities(self) -> ModelCapabilities: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]: ...

    async def close(self) -> None: ...
