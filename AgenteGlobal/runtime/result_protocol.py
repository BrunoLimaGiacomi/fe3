"""Safe Function Calling intake for versioned ``AgentResult`` contracts."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import AgentError, AgentErrorCode, AgentResult, AgentResultStatus


SUBMIT_AGENT_RESULT_FUNCTION = "submit_agent_result"


class ResultProtocolErrorCode(StrEnum):
    MISSING_FUNCTION_CALL = "missing_function_call"
    UNEXPECTED_FUNCTION = "unexpected_function"
    INVALID_ARGUMENTS = "invalid_arguments"
    REPAIR_EXHAUSTED = "repair_exhausted"


class ResultProtocolFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ResultProtocolErrorCode
    message: str
    attempts: int = Field(ge=0)
    validation_errors: tuple[dict[str, Any], ...] = ()


class AgentResultProtocolError(ValueError):
    def __init__(self, failure: ResultProtocolFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class FunctionCall(BaseModel):
    """A model response representation, never an executable callable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    arguments: str | Mapping[str, Any]


RepairCallback = Callable[[ResultProtocolFailure, int], FunctionCall | Mapping[str, Any] | None]


class AgentResultFunctionProtocol:
    """Validate only ``submit_agent_result`` arguments with bounded repair."""

    def __init__(self, *, max_repairs: int = 1) -> None:
        if max_repairs < 0 or max_repairs > 10:
            raise ValueError("max_repairs must be between 0 and 10.")
        self.max_repairs = max_repairs

    @staticmethod
    def function_schema() -> dict[str, Any]:
        """Function-tool definition for a provider request; JSON mode is not used."""
        return {
            "type": "function",
            "function": {
                "name": SUBMIT_AGENT_RESULT_FUNCTION,
                "description": "Submit the final, versioned result for the assigned task.",
                "parameters": AgentResult.model_json_schema(),
            },
        }

    @staticmethod
    def _failure(
        code: ResultProtocolErrorCode,
        message: str,
        attempts: int,
        validation_errors: tuple[dict[str, Any], ...] = (),
    ) -> ResultProtocolFailure:
        return ResultProtocolFailure(
            code=code,
            message=message,
            attempts=attempts,
            validation_errors=validation_errors,
        )

    def _validate_call(self, call: FunctionCall, attempts: int) -> AgentResult:
        if call.name != SUBMIT_AGENT_RESULT_FUNCTION:
            raise AgentResultProtocolError(
                self._failure(
                    ResultProtocolErrorCode.UNEXPECTED_FUNCTION,
                    f"Expected {SUBMIT_AGENT_RESULT_FUNCTION!r}, got {call.name!r}.",
                    attempts,
                )
            )
        try:
            raw_json = call.arguments if isinstance(call.arguments, str) else json.dumps(dict(call.arguments))
            arguments = json.loads(raw_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise AgentResultProtocolError(
                self._failure(ResultProtocolErrorCode.INVALID_ARGUMENTS, "Function arguments are not valid JSON.", attempts)
            ) from error
        if not isinstance(arguments, dict):
            raise AgentResultProtocolError(
                self._failure(ResultProtocolErrorCode.INVALID_ARGUMENTS, "Function arguments must be a JSON object.", attempts)
            )
        try:
            return AgentResult.model_validate_json(raw_json)
        except ValidationError as error:
            raise AgentResultProtocolError(
                self._failure(
                    ResultProtocolErrorCode.INVALID_ARGUMENTS,
                    "Function arguments do not satisfy AgentResult.",
                    attempts,
                    tuple(error.errors(include_url=False, include_input=False)),
                )
            ) from error

    def validate(self, call: FunctionCall, *, attempt: int = 0) -> AgentResult:
        """Validate one model-produced call without executing or repairing it."""

        return self._validate_call(call, attempt)

    def _coerce_call(
        self,
        call: FunctionCall | Mapping[str, Any] | None,
        *,
        attempt: int,
    ) -> FunctionCall | None:
        if call is None:
            return None
        try:
            return FunctionCall.model_validate(call)
        except ValidationError as error:
            raise AgentResultProtocolError(
                self._failure(
                    ResultProtocolErrorCode.INVALID_ARGUMENTS,
                    "Function call envelope does not satisfy the protocol.",
                    attempt,
                    tuple(error.errors(include_url=False, include_input=False)),
                )
            ) from error

    def exhausted_failure(
        self,
        failure: ResultProtocolFailure,
        *,
        attempts: int,
    ) -> ResultProtocolFailure:
        return self._failure(
            ResultProtocolErrorCode.REPAIR_EXHAUSTED,
            f"Agent result repair exhausted after {self.max_repairs} repair attempt(s): {failure.message}",
            attempts,
            failure.validation_errors,
        )

    @staticmethod
    def failure_result(task_id: str, failure: ResultProtocolFailure) -> AgentResult:
        """Convert an exhausted protocol failure into the normal typed result boundary."""

        return AgentResult(
            task_id=task_id,
            status=AgentResultStatus.FAILED,
            summary="O subagente não produziu um AgentResult válido.",
            errors=[
                AgentError(
                    code=AgentErrorCode.PROTOCOL_ERROR,
                    message=failure.message,
                    retryable=False,
                    details={"attempts": failure.attempts, "protocol_code": failure.code.value},
                )
            ],
        )

    def parse(
        self,
        call: FunctionCall | Mapping[str, Any] | None,
        *,
        repair: RepairCallback | None = None,
    ) -> AgentResult:
        """Parse local data only.  It never invokes model-provided arguments as code."""
        current: FunctionCall | Mapping[str, Any] | None = call
        for attempt in range(self.max_repairs + 1):
            try:
                normalized = self._coerce_call(current, attempt=attempt)
                if normalized is None:
                    failure = self._failure(
                        ResultProtocolErrorCode.MISSING_FUNCTION_CALL,
                        f"Missing {SUBMIT_AGENT_RESULT_FUNCTION!r} function call.",
                        attempt,
                    )
                else:
                    return self._validate_call(normalized, attempt)
            except AgentResultProtocolError as error:
                failure = error.failure
            if repair is None or attempt >= self.max_repairs:
                failure = self._failure(
                    ResultProtocolErrorCode.REPAIR_EXHAUSTED,
                    f"Agent result repair exhausted after {attempt} repair attempt(s): {failure.message}",
                    attempt,
                    failure.validation_errors,
                )
                raise AgentResultProtocolError(failure)
            current = repair(failure, attempt + 1)
        raise AssertionError("Unreachable bounded repair loop.")
