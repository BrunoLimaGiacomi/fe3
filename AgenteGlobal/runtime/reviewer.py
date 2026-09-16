"""Independent review protocol and bounded repair selection helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from .contracts import ReviewResult, TaskSpec


SUBMIT_REVIEW_RESULT_FUNCTION = "submit_review_result"


class ReviewProtocolError(ValueError):
    """The reviewer did not return the required typed function call."""


class RepairLimitExceeded(ValueError):
    def __init__(self, review: ReviewResult, max_repairs: int) -> None:
        super().__init__(
            f"Reviewer rejeitou o resultado após o limite de {max_repairs} repair(s): {review.summary}"
        )
        self.review = review
        self.max_repairs = max_repairs


class ReviewResultFunctionProtocol:
    """Strict local intake for one reviewer result."""

    @staticmethod
    def function_schema() -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": SUBMIT_REVIEW_RESULT_FUNCTION,
                "description": "Submit the independent review verdict for the executed plan.",
                "parameters": ReviewResult.model_json_schema(),
            },
        }

    @staticmethod
    def validate(
        name: str,
        arguments: str | Mapping[str, Any],
        *,
        plan_id: str,
        plan_revision: int,
        known_task_ids: frozenset[str],
    ) -> ReviewResult:
        if name != SUBMIT_REVIEW_RESULT_FUNCTION:
            raise ReviewProtocolError(
                f"Expected {SUBMIT_REVIEW_RESULT_FUNCTION!r}, got {name!r}."
            )
        try:
            raw = arguments if isinstance(arguments, str) else json.dumps(dict(arguments))
            review = ReviewResult.model_validate_json(raw)
        except (TypeError, ValidationError, json.JSONDecodeError) as exc:
            raise ReviewProtocolError("Reviewer arguments do not satisfy ReviewResult.") from exc
        if review.plan_id != plan_id or review.plan_revision != plan_revision:
            raise ReviewProtocolError("ReviewResult must reference the exact executed plan revision.")
        unknown = set(review.repair_task_ids) - known_task_ids
        if unknown:
            raise ReviewProtocolError(f"Reviewer requested unknown repair tasks: {sorted(unknown)}")
        return review


def repair_closure(tasks: Sequence[TaskSpec], requested_task_ids: Sequence[str]) -> frozenset[str]:
    """Return requested tasks plus every dependent that must be revalidated."""

    task_ids = {task.task_id for task in tasks}
    requested = set(requested_task_ids)
    unknown = requested - task_ids
    if unknown:
        raise ValueError(f"Repair references unknown tasks: {sorted(unknown)}")
    if not requested:
        raise ValueError("Repair must explicitly identify at least one task.")

    selected = set(requested)
    changed = True
    while changed:
        changed = False
        for task in tasks:
            if task.task_id not in selected and selected.intersection(task.dependencies):
                selected.add(task.task_id)
                changed = True
    return frozenset(selected)
