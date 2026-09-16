"""Bounded retry classification for provider failures."""

from __future__ import annotations

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError


RETRYABLE_HTTP_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def is_retryable_api_error(exc: OpenAIError) -> bool:
    """Return true only for transient timeout, disconnect and status failures."""

    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return True
    return isinstance(exc, APIStatusError) and exc.status_code in RETRYABLE_HTTP_STATUS


def retry_delay_seconds(attempt: int, *, cap_seconds: float = 8.0) -> float:
    """Small deterministic exponential backoff, bounded for cancellation responsiveness."""

    if attempt < 1:
        raise ValueError("attempt precisa ser positivo")
    if cap_seconds <= 0:
        raise ValueError("cap_seconds precisa ser positivo")
    return min(cap_seconds, float(2 ** (attempt - 1)))


__all__ = ["RETRYABLE_HTTP_STATUS", "is_retryable_api_error", "retry_delay_seconds"]
