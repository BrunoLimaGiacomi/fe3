"""Token-aware context budgeting for the AgenteGlobal runtime.

This module deliberately has no provider calls.  It keeps the accounting
needed by a future context engine separate from message construction and
from the LLM adapters.  ``tiktoken`` is an optional accuracy improvement;
the UTF-8 fallback is deterministic and intentionally overestimates.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from enum import StrEnum
from typing import Any, Callable, Iterator

try:  # Optional dependency: the runtime must work without tiktoken.
    import tiktoken  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised when the optional package is absent
    tiktoken = None  # type: ignore[assignment]

try:  # The repository exposes ``llm`` as a top-level package in its CLI.
    from llm.capabilities import ModelCapabilities
except ImportError:  # pragma: no cover - also supports ``AgenteGlobal.runtime`` imports
    from ..llm.capabilities import ModelCapabilities


DEFAULT_CONTEXT_WINDOW_TOKENS = 32_768
DEFAULT_MAX_OUTPUT_TOKENS = 4_096
DEFAULT_OUTPUT_RESERVE_TOKENS = 4_096
DEFAULT_FALLBACK_BYTES_PER_TOKEN = 3
DEFAULT_SAFETY_MARGIN = 0.10
DEFAULT_MINIMUM_SAFETY_TOKENS = 4


class BudgetCategory(StrEnum):
    """Named portions of the request context.

    The values are part of the telemetry contract.  Keep them stable when
    adding a new context source so old snapshots remain readable.
    """

    SYSTEM = "system"
    AGENT_INSTRUCTIONS = "agent_instructions"
    CONSTITUTION_SPEC = "constitution_spec"
    SKILLS = "skills"
    HOT_CONVERSATION = "hot_conversation"
    SESSION_STATE = "session_state"
    RETRIEVED_CONTEXT = "retrieved_context"
    TOOL_RESULTS = "tool_results"
    OUTPUT_RESERVE = "output_reserve"


# Names used by callers in early prototypes and by downstream integrations.
ContextBudgetCategory = BudgetCategory
BudgetCategories = BudgetCategory
CONTEXT_BUDGET_CATEGORIES = tuple(category.value for category in BudgetCategory)


def _json_default(value: Any) -> Any:
    """Convert common runtime objects to deterministic JSON-safe values."""

    if hasattr(value, "model_dump") and callable(value.model_dump):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    """Detailed estimate useful for diagnostics without retaining content."""

    raw_tokens: int
    estimated_tokens: int
    method: str
    safety_margin: float

    @property
    def tokens(self) -> int:
        return self.estimated_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_tokens": self.raw_tokens,
            "estimated_tokens": self.estimated_tokens,
            "method": self.method,
            "safety_margin": self.safety_margin,
        }


class TokenEstimator:
    """Conservative, provider-neutral token estimator.

    ``tiktoken`` is used only when already installed locally.  If it is not
    available, UTF-8 bytes are divided by a configurable small ratio and a
    safety margin is applied.  That is deliberately less precise than a
    provider tokenizer but protects the request boundary from undercounting.
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        safety_margin: float = DEFAULT_SAFETY_MARGIN,
        minimum_safety_tokens: int = DEFAULT_MINIMUM_SAFETY_TOKENS,
        fallback_bytes_per_token: int = DEFAULT_FALLBACK_BYTES_PER_TOKEN,
    ) -> None:
        if not 0 <= safety_margin <= 10:
            raise ValueError("safety_margin deve estar entre 0 e 10.")
        if minimum_safety_tokens < 0:
            raise ValueError("minimum_safety_tokens não pode ser negativo.")
        if fallback_bytes_per_token < 1:
            raise ValueError("fallback_bytes_per_token deve ser positivo.")
        self.model = model
        self.safety_margin = float(safety_margin)
        self.minimum_safety_tokens = int(minimum_safety_tokens)
        self.fallback_bytes_per_token = int(fallback_bytes_per_token)
        self._encoding: Any | None = None
        self._encoding_checked = False

    def _get_encoding(self) -> Any | None:
        if self._encoding_checked:
            return self._encoding
        self._encoding_checked = True
        if tiktoken is None:
            return None
        try:
            if self.model:
                self._encoding = tiktoken.encoding_for_model(self.model)
            else:
                self._encoding = tiktoken.get_encoding("cl100k_base")
        except (KeyError, LookupError, TypeError, ValueError):
            # Unknown provider model names are expected; the local fallback is
            # safer than trying to download or guess an encoding.
            self._encoding = None
        return self._encoding

    def _apply_safety(self, raw_tokens: int) -> int:
        if raw_tokens <= 0:
            return 0
        return max(
            raw_tokens,
            int(math.ceil(raw_tokens * (1.0 + self.safety_margin))),
            raw_tokens + self.minimum_safety_tokens,
        )

    def estimate_text_with_details(self, text: str) -> TokenEstimate:
        if not isinstance(text, str):
            raise TypeError("O estimador de texto exige str.")
        encoding = self._get_encoding()
        if encoding is not None:
            try:
                raw_tokens = len(encoding.encode(text, disallowed_special=()))
            except TypeError:  # Small compatibility shim for older tiktoken APIs.
                raw_tokens = len(encoding.encode(text))
            method = "tiktoken"
        else:
            # Three bytes/token is intentionally conservative for a tokenizer
            # that is unknown to this process.  UTF-8 replacement is stable.
            byte_length = len(text.encode("utf-8"))
            raw_tokens = math.ceil(byte_length / self.fallback_bytes_per_token)
            method = "utf8_fallback"
        return TokenEstimate(
            raw_tokens=raw_tokens,
            estimated_tokens=self._apply_safety(raw_tokens),
            method=method,
            safety_margin=self.safety_margin,
        )

    def estimate_text(self, text: str) -> int:
        return self.estimate_text_with_details(text).estimated_tokens

    def estimate_with_details(self, value: Any) -> TokenEstimate:
        if isinstance(value, str):
            return self.estimate_text_with_details(value)
        if isinstance(value, bytes):
            return self.estimate_text_with_details(value.decode("utf-8", errors="replace"))
        if isinstance(value, Mapping | list | tuple | set | frozenset):
            # Sorting keys and using compact separators makes test and runtime
            # accounting repeatable.  Structural overhead covers message or
            # JSON framing not represented in the serialized value.
            if isinstance(value, (set, frozenset)):
                value = sorted(value, key=str)
            serialized = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=_json_default,
            )
            estimate = self.estimate_text_with_details(serialized)
            return TokenEstimate(
                raw_tokens=estimate.raw_tokens + 4,
                estimated_tokens=estimate.estimated_tokens + 4,
                method=f"{estimate.method}_structured",
                safety_margin=estimate.safety_margin,
            )
        if value is None:
            return self.estimate_text_with_details("")
        return self.estimate_text_with_details(str(value))

    def estimate(self, value: Any) -> int:
        return self.estimate_with_details(value).estimated_tokens

    def estimate_messages(self, messages: Iterable[Any]) -> int:
        total = 0
        for message in messages:
            total += self.estimate(message)
            # Chat APIs add role/message framing around serialized content.
            total += 4
        return total


@dataclass(frozen=True, slots=True)
class CategorySnapshot:
    category: str
    estimated_tokens: int
    actual_tokens: int | None
    effective_tokens: int
    item_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "estimated_tokens": self.estimated_tokens,
            "actual_tokens": self.actual_tokens,
            "effective_tokens": self.effective_tokens,
            "item_count": self.item_count,
        }


@dataclass(frozen=True, slots=True)
class BudgetSnapshot(Mapping[str, Any]):
    """Immutable observable budget state.

    It behaves both as a typed object (attributes) and a mapping (for JSON
    telemetry and compatibility with callers expecting ``snapshot["..."]``).
    """

    schema_version: str
    model: str
    context_window_tokens: int
    max_input_tokens: int
    max_output_tokens: int
    output_reserve_tokens: int
    categories: Mapping[str, CategorySnapshot]
    estimated_input_tokens: int
    estimated_total_tokens: int
    actual_input_tokens: int | None
    actual_output_tokens: int | None
    actual_total_tokens: int | None
    reconciled_input_tokens: int
    reconciled_total_tokens: int
    remaining_input_tokens: int
    overflow_tokens: int
    within_budget: bool
    estimator_method: str
    limits_source: str | None
    limits_observation: str | None
    sequence: int

    @property
    def estimated_tokens(self) -> int:
        return self.estimated_input_tokens

    @property
    def total_tokens(self) -> int:
        return self.reconciled_total_tokens

    @property
    def remaining_tokens(self) -> int:
        return self.remaining_input_tokens

    @property
    def fits(self) -> bool:
        return self.within_budget

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "model": self.model,
            "context_window_tokens": self.context_window_tokens,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
            "categories": {name: item.to_dict() for name, item in self.categories.items()},
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_total_tokens": self.estimated_total_tokens,
            "actual_input_tokens": self.actual_input_tokens,
            "actual_output_tokens": self.actual_output_tokens,
            "actual_total_tokens": self.actual_total_tokens,
            "reconciled_input_tokens": self.reconciled_input_tokens,
            "reconciled_total_tokens": self.reconciled_total_tokens,
            "remaining_input_tokens": self.remaining_input_tokens,
            "overflow_tokens": self.overflow_tokens,
            "within_budget": self.within_budget,
            "estimator_method": self.estimator_method,
            "limits_source": self.limits_source,
            "limits_observation": self.limits_observation,
            "sequence": self.sequence,
        }
        # Stable aliases make the telemetry useful to older integrations that
        # used generic ``estimated_tokens``/``total_tokens`` names.
        result.update(
            {
                "estimated_tokens": self.estimated_input_tokens,
                "total_tokens": self.reconciled_total_tokens,
                "remaining_tokens": self.remaining_input_tokens,
            }
        )
        return result

    as_dict = to_dict
    model_dump = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


@dataclass(slots=True)
class _CategoryState:
    estimated_tokens: int = 0
    actual_tokens: int | None = None
    item_count: int = 0


def _coerce_nonnegative_int(value: Any, *, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{name} deve ser inteiro, não bool.")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    else:
        raise TypeError(f"{name} deve ser um inteiro não negativo.")
    if result < 0:
        raise ValueError(f"{name} não pode ser negativo.")
    return result


def _read_limit(source: Any, names: tuple[str, ...]) -> int | None:
    if source is None:
        return None
    if isinstance(source, Mapping):
        for name in names:
            value = source.get(name)
            if value is not None:
                return _coerce_nonnegative_int(value, name=name)
        return None
    for name in names:
        value = getattr(source, name, None)
        if value is not None:
            return _coerce_nonnegative_int(value, name=name)
    return None


def _read_text(source: Any, names: tuple[str, ...]) -> str | None:
    if source is None:
        return None
    if isinstance(source, Mapping):
        values = (source.get(name) for name in names)
    else:
        values = (getattr(source, name, None) for name in names)
    for value in values:
        if value is not None and str(value).strip():
            return str(value)
    return None


class ContextBudget:
    """Tracks conservative input tokens and an explicit output reserve.

    The model's limits are read from ``ModelCapabilities``.  If a legacy
    capability snapshot has no limit fields, bounded defaults are used and
    exposed in telemetry as an unknown/legacy source.  This keeps old
    snapshots loadable while avoiding an unbounded request.
    """

    schema_version = "1.0"

    def __init__(
        self,
        capabilities: ModelCapabilities | Any | None = None,
        *,
        model_capabilities: ModelCapabilities | Any | None = None,
        output_reserve_tokens: int | None = None,
        output_reserve: int | None = None,
        estimator: TokenEstimator | None = None,
        telemetry_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        observer: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        if capabilities is not None and model_capabilities is not None and capabilities is not model_capabilities:
            raise TypeError("Informe capabilities ou model_capabilities, não ambos.")
        self.capabilities = capabilities if capabilities is not None else model_capabilities
        if output_reserve_tokens is not None and output_reserve is not None:
            raise TypeError("Informe output_reserve_tokens ou output_reserve, não ambos.")
        requested_reserve = output_reserve_tokens if output_reserve_tokens is not None else output_reserve

        nested_limits = None
        if self.capabilities is not None:
            nested_limits = getattr(self.capabilities, "token_limits", None)
            if nested_limits is None:
                nested_limits = getattr(self.capabilities, "limits", None)

        context_window = _read_limit(
            self.capabilities,
            ("context_window_tokens", "max_context_tokens", "context_window"),
        )
        context_window = context_window or _read_limit(
            nested_limits,
            ("context_window_tokens", "max_context_tokens", "context_window"),
        )
        self.context_window_tokens = context_window or DEFAULT_CONTEXT_WINDOW_TOKENS

        max_output = _read_limit(
            self.capabilities,
            ("max_output_tokens", "output_token_limit", "max_completion_tokens"),
        )
        max_output = max_output or _read_limit(
            nested_limits,
            ("max_output_tokens", "output_token_limit", "max_completion_tokens"),
        )
        self.max_output_tokens = min(max_output or DEFAULT_MAX_OUTPUT_TOKENS, self.context_window_tokens)

        capability_reserve = _read_limit(
            self.capabilities,
            ("output_reserve_tokens", "default_output_reserve_tokens"),
        )
        capability_reserve = capability_reserve or _read_limit(
            nested_limits,
            ("output_reserve_tokens", "default_output_reserve_tokens"),
        )
        if requested_reserve is None:
            requested_reserve = capability_reserve
        if requested_reserve is None:
            requested_reserve = min(DEFAULT_OUTPUT_RESERVE_TOKENS, self.max_output_tokens)
        self.output_reserve_tokens = _coerce_nonnegative_int(
            requested_reserve, name="output_reserve_tokens"
        )
        if self.output_reserve_tokens is None:  # Defensive guard for static/runtime callers.
            raise TypeError("output_reserve_tokens é obrigatório.")
        if self.output_reserve_tokens > self.max_output_tokens:
            raise ValueError("output_reserve_tokens não pode exceder max_output_tokens.")
        if self.output_reserve_tokens >= self.context_window_tokens:
            raise ValueError("output_reserve_tokens deve deixar espaço para contexto de entrada.")

        max_input = _read_limit(self.capabilities, ("max_input_tokens", "input_token_limit"))
        max_input = max_input or _read_limit(nested_limits, ("max_input_tokens", "input_token_limit"))
        context_input_limit = self.context_window_tokens - self.output_reserve_tokens
        self.max_input_tokens = min(max_input, context_input_limit) if max_input is not None else context_input_limit

        self.model = str(getattr(self.capabilities, "model", "unknown"))
        self.limits_source = _read_text(
            self.capabilities,
            ("limits_source", "limit_source", "context_window_source"),
        ) or _read_text(nested_limits, ("source", "limits_source", "limit_source"))
        self.limits_observation = _read_text(
            self.capabilities,
            ("limits_observation", "limit_observation", "context_window_observation"),
        ) or _read_text(nested_limits, ("observation", "limits_observation", "limit_observation"))
        if self.limits_source is None:
            self.limits_source = "legacy/default policy"
        if self.limits_observation is None:
            self.limits_observation = "Limites ausentes no snapshot; aplicado default conservador local."

        self.estimator = estimator or TokenEstimator(model=self.model)
        sink = telemetry_sink if telemetry_sink is not None else observer
        if sink is not None and not callable(sink):
            raise TypeError("telemetry_sink precisa ser chamável.")
        self._telemetry_sink = sink
        self._sequence = 0
        self._categories: dict[BudgetCategory, _CategoryState] = {
            category: _CategoryState() for category in BudgetCategory
        }
        self._categories[BudgetCategory.OUTPUT_RESERVE] = _CategoryState(
            estimated_tokens=self.output_reserve_tokens,
            item_count=1,
        )
        self._actual_input_tokens: int | None = None
        self._actual_output_tokens: int | None = None
        self._actual_total_tokens: int | None = None

    @property
    def available_tokens(self) -> int:
        return self.remaining_input_tokens

    @property
    def remaining_input_tokens(self) -> int:
        return max(0, self.max_input_tokens - self._estimated_input_tokens())

    @property
    def is_within_budget(self) -> bool:
        return self._estimated_input_tokens() <= self.max_input_tokens

    @property
    def within_budget(self) -> bool:
        return self.is_within_budget

    def _category(self, category: BudgetCategory | str) -> BudgetCategory:
        try:
            return category if isinstance(category, BudgetCategory) else BudgetCategory(str(category))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Categoria de budget desconhecida: {category!r}.") from exc

    def _estimated_input_tokens(self) -> int:
        return sum(
            state.estimated_tokens
            for category, state in self._categories.items()
            if category is not BudgetCategory.OUTPUT_RESERVE
        )

    def _effective_input_tokens(self) -> int:
        return max(self._estimated_input_tokens(), self._actual_input_tokens or 0)

    def _effective_total_tokens(self) -> int:
        estimated_total = self._estimated_input_tokens() + self.output_reserve_tokens
        observed_total = self._actual_total_tokens or 0
        observed_parts = (self._actual_input_tokens or 0) + (self._actual_output_tokens or 0)
        return max(estimated_total, observed_total, observed_parts)

    def set_estimate(
        self,
        category: BudgetCategory | str,
        estimated_tokens: int,
        *,
        actual_tokens: int | None = None,
        item_count: int = 1,
    ) -> int:
        selected = self._category(category)
        estimate = _coerce_nonnegative_int(estimated_tokens, name="estimated_tokens")
        count = _coerce_nonnegative_int(item_count, name="item_count")
        actual = _coerce_nonnegative_int(actual_tokens, name="actual_tokens")
        if estimate is None or count is None:  # Defensive guard for static/runtime callers.
            raise TypeError("estimated_tokens e item_count são obrigatórios.")
        if selected is BudgetCategory.OUTPUT_RESERVE:
            if estimate > self.max_output_tokens:
                raise ValueError("output_reserve não pode exceder max_output_tokens.")
            if estimate >= self.context_window_tokens:
                raise ValueError("output_reserve deve deixar espaço para contexto de entrada.")
            self.output_reserve_tokens = estimate
            self._categories[selected] = _CategoryState(estimate, actual, count)
            self.max_input_tokens = min(self.max_input_tokens, self.context_window_tokens - estimate)
        else:
            self._categories[selected] = _CategoryState(estimate, actual, count)
        self._sequence += 1
        return estimate

    def set(
        self,
        category: BudgetCategory | str,
        value: Any,
        *,
        actual_tokens: int | None = None,
    ) -> int:
        selected = self._category(category)
        if selected is BudgetCategory.OUTPUT_RESERVE:
            estimate = _coerce_nonnegative_int(value, name="output_reserve_tokens")
            if estimate is None:
                raise TypeError("output_reserve_tokens é obrigatório.")
        elif isinstance(value, int) and not isinstance(value, bool):
            # Explicit integers are useful when a provider reports a category
            # count; textual/structured sources continue through the estimator.
            estimate = value
        elif (
            selected is BudgetCategory.HOT_CONVERSATION
            and isinstance(value, Iterable)
            and not isinstance(value, (str, bytes, Mapping))
        ):
            estimate = self.estimator.estimate_messages(value)
        else:
            estimate = self.estimator.estimate(value)
        return self.set_estimate(selected, estimate, actual_tokens=actual_tokens)

    def add(
        self,
        category: BudgetCategory | str,
        value: Any,
        *,
        actual_tokens: int | None = None,
    ) -> int:
        selected = self._category(category)
        if selected is BudgetCategory.OUTPUT_RESERVE:
            raise ValueError("output_reserve é explícito e deve ser definido uma única vez.")
        estimate = value if isinstance(value, int) and not isinstance(value, bool) else self.estimator.estimate(value)
        estimate = _coerce_nonnegative_int(estimate, name="estimated_tokens")
        if estimate is None:
            raise TypeError("estimated_tokens é obrigatório.")
        current = self._categories[selected]
        actual = _coerce_nonnegative_int(actual_tokens, name="actual_tokens")
        current.estimated_tokens += estimate
        current.item_count += 1
        if actual is not None:
            current.actual_tokens = max(current.actual_tokens or 0, actual)
        self._sequence += 1
        return estimate

    record = add

    def reset(self, category: BudgetCategory | str | None = None) -> None:
        if category is None:
            for selected in BudgetCategory:
                self._categories[selected] = _CategoryState()
            self._categories[BudgetCategory.OUTPUT_RESERVE] = _CategoryState(
                estimated_tokens=self.output_reserve_tokens,
                item_count=1,
            )
        else:
            selected = self._category(category)
            if selected is BudgetCategory.OUTPUT_RESERVE:
                self._categories[selected] = _CategoryState(
                    estimated_tokens=self.output_reserve_tokens,
                    item_count=1,
                )
            else:
                self._categories[selected] = _CategoryState()
        self._sequence += 1

    def estimate(self, value: Any, maybe_value: Any = None) -> int | BudgetSnapshot:
        """Estimate a source, or populate categories from a mapping.

        ``estimate(mapping)`` is a concise context-building operation.  For a
        single source, use ``estimate(value)``; ``estimate(category, value)``
        is accepted for compatibility with early callers.
        """

        if isinstance(value, (BudgetCategory, str)) and maybe_value is not None:
            self.set(value, maybe_value)
            return self.snapshot()
        if isinstance(value, Mapping):
            for category, source in value.items():
                self.set(category, source)
            return self.snapshot()
        return self.estimator.estimate(value)

    estimate_categories = estimate

    def can_add(self, category: BudgetCategory | str, value: Any) -> bool:
        selected = self._category(category)
        if selected is BudgetCategory.OUTPUT_RESERVE:
            raise ValueError("output_reserve não é uma categoria incremental.")
        estimate = self.estimator.estimate(value)
        return self._estimated_input_tokens() + estimate <= self.max_input_tokens

    def fits(self, additional_tokens: int = 0) -> bool:
        additional = _coerce_nonnegative_int(additional_tokens, name="additional_tokens")
        if additional is None:
            raise TypeError("additional_tokens é obrigatório.")
        return self._estimated_input_tokens() + additional <= self.max_input_tokens

    @staticmethod
    def _usage_value(usage: Any, names: tuple[str, ...]) -> int | None:
        if usage is None:
            return None
        if isinstance(usage, Mapping):
            for name in names:
                value = usage.get(name)
                if value is not None:
                    return _coerce_nonnegative_int(value, name=name)
            return None
        for name in names:
            value = getattr(usage, name, None)
            if value is not None:
                return _coerce_nonnegative_int(value, name=name)
        return None

    def reconcile_usage(self, usage: Any) -> BudgetSnapshot:
        """Merge provider usage without lowering conservative estimates."""

        prompt = self._usage_value(usage, ("prompt_tokens", "input_tokens"))
        completion = self._usage_value(usage, ("completion_tokens", "output_tokens"))
        total = self._usage_value(usage, ("total_tokens", "tokens"))
        if total is None and (prompt is not None or completion is not None):
            total = (prompt or 0) + (completion or 0)
        if prompt is not None:
            self._actual_input_tokens = max(self._actual_input_tokens or 0, prompt)
        if completion is not None:
            self._actual_output_tokens = max(self._actual_output_tokens or 0, completion)
        if total is not None:
            self._actual_total_tokens = max(self._actual_total_tokens or 0, total)
        self._sequence += 1
        return self.snapshot()

    reconcile = reconcile_usage
    record_usage = reconcile_usage

    def snapshot(self) -> BudgetSnapshot:
        estimated_input = self._estimated_input_tokens()
        actual_input = self._actual_input_tokens
        actual_total = self._actual_total_tokens
        effective_input = self._effective_input_tokens()
        effective_total = self._effective_total_tokens()
        category_snapshots = {
            category.value: CategorySnapshot(
                category=category.value,
                estimated_tokens=state.estimated_tokens,
                actual_tokens=state.actual_tokens,
                effective_tokens=max(state.estimated_tokens, state.actual_tokens or 0),
                item_count=state.item_count,
            )
            for category, state in self._categories.items()
        }
        snapshot = BudgetSnapshot(
            schema_version=self.schema_version,
            model=self.model,
            context_window_tokens=self.context_window_tokens,
            max_input_tokens=self.max_input_tokens,
            max_output_tokens=self.max_output_tokens,
            output_reserve_tokens=self.output_reserve_tokens,
            categories=category_snapshots,
            estimated_input_tokens=estimated_input,
            estimated_total_tokens=estimated_input + self.output_reserve_tokens,
            actual_input_tokens=actual_input,
            actual_output_tokens=self._actual_output_tokens,
            actual_total_tokens=actual_total,
            reconciled_input_tokens=effective_input,
            reconciled_total_tokens=effective_total,
            remaining_input_tokens=max(0, self.max_input_tokens - effective_input),
            overflow_tokens=max(0, effective_input - self.max_input_tokens),
            within_budget=effective_input <= self.max_input_tokens,
            estimator_method=self.estimator.estimate_text_with_details("").method,
            limits_source=self.limits_source,
            limits_observation=self.limits_observation,
            sequence=self._sequence,
        )
        return snapshot

    get_snapshot = snapshot

    def telemetry(self) -> dict[str, Any]:
        """Return metadata-only telemetry suitable for an event logger."""

        payload = {"event": "context_budget.snapshot", "snapshot": self.snapshot().to_dict()}
        if self._telemetry_sink is not None:
            self._telemetry_sink(payload)
        return payload

    telemetry_snapshot = telemetry

    def category_totals(self) -> dict[str, int]:
        return {category.value: state.estimated_tokens for category, state in self._categories.items()}


__all__ = [
    "BudgetCategory",
    "BudgetSnapshot",
    "CategorySnapshot",
    "CONTEXT_BUDGET_CATEGORIES",
    "ContextBudget",
    "ContextBudgetCategory",
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "DEFAULT_OUTPUT_RESERVE_TOKENS",
    "TokenEstimate",
    "TokenEstimator",
]
