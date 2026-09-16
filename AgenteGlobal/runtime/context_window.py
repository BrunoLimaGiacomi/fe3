"""Token-aware selection of messages for one MaaS request.

The canonical conversation remains untouched.  This module selects a bounded
view for the provider, preserving system anchors and the active user/tool turn
before retaining older complete turns from newest to oldest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from llm.contracts import Message

from .context_budget import BudgetCategory, BudgetSnapshot, ContextBudget


class PromptTooLargeError(ValueError):
    """The mandatory request slice cannot fit while reserving model output."""


@dataclass(frozen=True, slots=True)
class PreparedContext:
    messages: list[Message]
    omitted_messages: int
    budget: BudgetSnapshot


def message_size_chars(message: Message) -> int:
    """Legacy observable size retained for compatibility diagnostics."""

    return len(json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str))


def close_oversized_turn(messages: list[Message], turn_start: int, exc: PromptTooLargeError) -> None:
    """Preserve executed tool evidence, or discard an untouched oversized turn."""

    active_messages = messages[turn_start:]
    if any(message.get("role") == "tool" for message in active_messages):
        messages.append(
            {
                "role": "assistant",
                "content": (
                    "A execução foi interrompida localmente por excesso de contexto após ferramentas já terem "
                    f"sido executadas. As evidências foram preservadas no histórico. Detalhe: {exc}"
                ),
            }
        )
        return
    del messages[turn_start:]


def system_state_key(message: Message) -> str | None:
    if message.get("role") != "system":
        return None
    content = str(message.get("content") or "")
    if content.startswith("Modo de permissão alterado"):
        return "permission"
    if content.startswith("Modo de verbosidade alterado"):
        return "verbosity"
    if content.startswith("Modo /plan") or content.startswith("Modo padrão de chat"):
        return "conversation"
    for prefix, key in (
        ("<session_state", "session_state"),
        ("<retrieved_context", "retrieved_context"),
        ("<spec_context", "constitution_spec"),
        ("<skill_context", "skills"),
    ):
        if content.startswith(prefix):
            return key
    return None


def _estimate_messages(budget: ContextBudget, messages: Sequence[Message]) -> int:
    return budget.estimator.estimate_messages(messages)


def _record_categories(
    budget: ContextBudget,
    messages: Sequence[Message],
    tools: Sequence[dict[str, Any]] | None,
) -> BudgetSnapshot:
    budget.reset()
    categorized_system: dict[BudgetCategory, list[Message]] = {
        BudgetCategory.SYSTEM: [],
        BudgetCategory.SESSION_STATE: [],
        BudgetCategory.RETRIEVED_CONTEXT: [],
        BudgetCategory.CONSTITUTION_SPEC: [],
        BudgetCategory.SKILLS: [],
    }
    prefix_categories = (
        ("<session_state", BudgetCategory.SESSION_STATE),
        ("<retrieved_context", BudgetCategory.RETRIEVED_CONTEXT),
        ("<spec_context", BudgetCategory.CONSTITUTION_SPEC),
        ("<skill_context", BudgetCategory.SKILLS),
    )
    for message in messages:
        if message.get("role") != "system":
            continue
        content = str(message.get("content") or "")
        category = next(
            (candidate for prefix, candidate in prefix_categories if content.startswith(prefix)),
            BudgetCategory.SYSTEM,
        )
        categorized_system[category].append(message)
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    conversation = [
        message for message in messages if message.get("role") not in {"system", "tool"}
    ]
    for category, category_messages in categorized_system.items():
        budget.set_estimate(
            category,
            _estimate_messages(budget, category_messages),
            item_count=len(category_messages),
        )
    budget.set_estimate(
        BudgetCategory.HOT_CONVERSATION,
        _estimate_messages(budget, conversation),
        item_count=len(conversation),
    )
    budget.set_estimate(
        BudgetCategory.TOOL_RESULTS,
        _estimate_messages(budget, tool_messages),
        item_count=len(tool_messages),
    )
    if tools:
        budget.set(BudgetCategory.AGENT_INSTRUCTIONS, list(tools))
    return budget.snapshot()


def prepare_messages(
    messages: Sequence[Message],
    budget: ContextBudget,
    *,
    tools: Sequence[dict[str, Any]] | None = None,
) -> PreparedContext:
    """Return a token-bounded provider view without mutating ``messages``."""

    original = list(messages)
    full_snapshot = _record_categories(budget, original, tools)
    if full_snapshot.within_budget:
        return PreparedContext(original, 0, full_snapshot)

    latest_user_index = next(
        (index for index in range(len(original) - 1, -1, -1) if original[index].get("role") == "user"),
        len(original),
    )
    anchor_indices: set[int] = set()
    if original and original[0].get("role") == "system":
        anchor_indices.add(0)
    latest_state_indices: dict[str, int] = {}
    for index, message in enumerate(original[:latest_user_index]):
        if message.get("role") != "system" or index == 0:
            continue
        state_key = system_state_key(message)
        if state_key is None:
            anchor_indices.add(index)
        else:
            latest_state_indices[state_key] = index
    anchor_indices.update(latest_state_indices.values())
    anchors = [original[index] for index in sorted(anchor_indices)]
    active_turn = original[latest_user_index:]
    notice: Message = {
        "role": "system",
        "content": "Proteção de contexto local: mensagens antigas foram omitidas desta chamada; use SessionState e artifacts para recuperar detalhes quando necessário.",
    }
    mandatory = [*anchors, notice, *active_turn]
    mandatory_snapshot = _record_categories(budget, mandatory, tools)
    if not mandatory_snapshot.within_budget:
        raise PromptTooLargeError(
            "O turno ativo não cabe no limite de entrada do modelo com a reserva de output aplicada."
        )

    older_messages = [
        message
        for index, message in enumerate(original[:latest_user_index])
        if index not in anchor_indices and message.get("role") != "system"
    ]
    chunks: list[list[Message]] = []
    for message in older_messages:
        if message.get("role") in {"user", "system"} or not chunks:
            chunks.append([message])
        else:
            chunks[-1].append(message)

    selected_reversed: list[list[Message]] = []
    for chunk in reversed(chunks):
        candidate_selected_reversed = [*selected_reversed, chunk]
        candidate_older = [
            item for group in reversed(candidate_selected_reversed) for item in group
        ]
        candidate = [*anchors, *candidate_older, notice, *active_turn]
        if not _record_categories(budget, candidate, tools).within_budget:
            break
        selected_reversed.append(chunk)

    selected = [item for chunk in reversed(selected_reversed) for item in chunk]
    kept_older_count = len(selected)
    omitted = latest_user_index - len(anchor_indices) - kept_older_count
    notice["content"] = (
        f"Proteção de contexto local: {omitted} mensagens antigas foram omitidas desta chamada. "
        "Use SessionState e artifacts para recuperar detalhes quando necessário."
    )
    compacted = [*anchors, *selected, notice, *active_turn]
    final_snapshot = _record_categories(budget, compacted, tools)
    if not final_snapshot.within_budget:
        raise PromptTooLargeError("A fatia compactada excedeu o budget de contexto.")
    return PreparedContext(compacted, omitted, final_snapshot)


def prepare_messages_by_chars(
    messages: Sequence[Message],
    *,
    max_chars: int,
) -> tuple[list[Message], int]:
    """Compatibility policy used only by callers of the pre-Fase-7 helper."""

    original = list(messages)
    if sum(message_size_chars(message) for message in original) <= max_chars:
        return original, 0
    latest_user_index = next(
        (index for index in range(len(original) - 1, -1, -1) if original[index].get("role") == "user"),
        len(original),
    )
    anchor_indices: set[int] = set()
    if original and original[0].get("role") == "system":
        anchor_indices.add(0)
    latest_state_indices: dict[str, int] = {}
    for index, message in enumerate(original[:latest_user_index]):
        if message.get("role") != "system" or index == 0:
            continue
        state_key = system_state_key(message)
        if state_key is None:
            anchor_indices.add(index)
        else:
            latest_state_indices[state_key] = index
    anchor_indices.update(latest_state_indices.values())
    anchors = [original[index] for index in sorted(anchor_indices)]
    active_turn = original[latest_user_index:]
    reserve_for_notice = 300
    mandatory_chars = sum(message_size_chars(message) for message in [*anchors, *active_turn])
    if mandatory_chars + reserve_for_notice > max_chars:
        raise PromptTooLargeError(
            "O pedido atual é grande demais para o limite de compatibilidade local."
        )
    older_messages = [
        message
        for index, message in enumerate(original[:latest_user_index])
        if index not in anchor_indices and message.get("role") != "system"
    ]
    chunks: list[list[Message]] = []
    for message in older_messages:
        if message.get("role") in {"user", "system"} or not chunks:
            chunks.append([message])
        else:
            chunks[-1].append(message)
    selected_reversed: list[list[Message]] = []
    used_chars = mandatory_chars + reserve_for_notice
    kept_older_count = 0
    for chunk in reversed(chunks):
        chunk_chars = sum(message_size_chars(message) for message in chunk)
        if used_chars + chunk_chars > max_chars:
            break
        selected_reversed.append(chunk)
        used_chars += chunk_chars
        kept_older_count += len(chunk)
    omitted = latest_user_index - len(anchors) - kept_older_count
    compacted: list[Message] = list(anchors)
    for chunk in reversed(selected_reversed):
        compacted.extend(chunk)
    compacted.append(
        {
            "role": "system",
            "content": (
                f"Proteção de contexto local: {omitted} mensagens antigas foram omitidas desta chamada. "
                "O turno atual e as regras permanentes foram preservados."
            ),
        }
    )
    compacted.extend(active_turn)
    return compacted, omitted


__all__ = [
    "PreparedContext",
    "PromptTooLargeError",
    "message_size_chars",
    "prepare_messages",
    "prepare_messages_by_chars",
    "system_state_key",
]
