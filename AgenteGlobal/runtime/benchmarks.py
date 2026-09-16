"""Small deterministic regression benchmark and quality scoring contracts."""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class BenchmarkScenarioKind(StrEnum):
    SIMPLE_EDIT = "simple_edit"
    MEDIUM_CODING_TASK = "medium_coding_task"
    LARGE_GOAL = "large_goal"
    LARGE_PASTED_INPUT = "large_pasted_input"
    MULTI_AGENT_TASK = "multi_agent_task"
    BROWSER_ASSISTED_TASK = "browser_assisted_task"
    FAILURE_RECOVERY = "failure_recovery"


@dataclass(frozen=True, slots=True)
class BenchmarkScenario:
    kind: BenchmarkScenarioKind
    minimum_correctness: float = 0.8
    minimum_completeness: float = 0.8
    maximum_context_utilization: float = 0.9
    maximum_tool_failures: int = 0
    maximum_unnecessary_delegations: int = 0
    require_convergence: bool = False
    require_recovery: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.minimum_correctness <= 1 or not 0 <= self.minimum_completeness <= 1:
            raise ValueError("quality thresholds precisam estar entre 0 e 1")
        if not 0 < self.maximum_context_utilization <= 1:
            raise ValueError("maximum_context_utilization precisa estar entre 0 e 1")


@dataclass(frozen=True, slots=True)
class BenchmarkObservation:
    correctness: float
    completeness: float
    context_utilization: float
    delegation_count: int = 0
    necessary_delegations: int = 0
    tool_failures: int = 0
    converged: bool = True
    recovered: bool = True
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("correctness", self.correctness),
            ("completeness", self.completeness),
            ("context_utilization", self.context_utilization),
        ):
            if not 0 <= value <= 1:
                raise ValueError(f"{name} precisa estar entre 0 e 1")
        if min(self.delegation_count, self.necessary_delegations, self.tool_failures) < 0:
            raise ValueError("benchmark counts precisam ser não negativos")


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    scenario: BenchmarkScenarioKind
    passed: bool
    score: float
    duration_seconds: float
    failures: tuple[str, ...]
    observation: BenchmarkObservation


@dataclass(frozen=True, slots=True)
class BenchmarkSuiteResult:
    results: tuple[BenchmarkResult, ...]

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.results)

    @property
    def average_score(self) -> float:
        return sum(item.score for item in self.results) / len(self.results) if self.results else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "average_score": self.average_score,
            "results": [
                {
                    "scenario": item.scenario.value,
                    "passed": item.passed,
                    "score": item.score,
                    "duration_seconds": item.duration_seconds,
                    "failures": list(item.failures),
                    "quality": {
                        "correctness": item.observation.correctness,
                        "completeness": item.observation.completeness,
                        "context_utilization": item.observation.context_utilization,
                        "delegation_count": item.observation.delegation_count,
                        "necessary_delegations": item.observation.necessary_delegations,
                        "tool_failures": item.observation.tool_failures,
                        "converged": item.observation.converged,
                        "recovered": item.observation.recovered,
                    },
                }
                for item in self.results
            ],
        }


BenchmarkExecutor = Callable[[BenchmarkScenario], BenchmarkObservation | Awaitable[BenchmarkObservation]]


DEFAULT_BENCHMARK_SCENARIOS = (
    BenchmarkScenario(BenchmarkScenarioKind.SIMPLE_EDIT),
    BenchmarkScenario(BenchmarkScenarioKind.MEDIUM_CODING_TASK),
    BenchmarkScenario(BenchmarkScenarioKind.LARGE_GOAL, require_convergence=True),
    BenchmarkScenario(BenchmarkScenarioKind.LARGE_PASTED_INPUT, maximum_context_utilization=0.85),
    BenchmarkScenario(
        BenchmarkScenarioKind.MULTI_AGENT_TASK,
        maximum_unnecessary_delegations=0,
    ),
    BenchmarkScenario(BenchmarkScenarioKind.BROWSER_ASSISTED_TASK, maximum_tool_failures=0),
    BenchmarkScenario(BenchmarkScenarioKind.FAILURE_RECOVERY, maximum_tool_failures=1, require_recovery=True),
)


class RegressionBenchmark:
    def __init__(self, scenarios: Sequence[BenchmarkScenario] = DEFAULT_BENCHMARK_SCENARIOS) -> None:
        self.scenarios = tuple(scenarios)
        if not self.scenarios:
            raise ValueError("benchmark precisa de ao menos um cenário")
        if len({item.kind for item in self.scenarios}) != len(self.scenarios):
            raise ValueError("cenários de benchmark duplicados")

    async def run(self, executor: BenchmarkExecutor) -> BenchmarkSuiteResult:
        results: list[BenchmarkResult] = []
        for scenario in self.scenarios:
            started = time.perf_counter()
            supplied = executor(scenario)
            observation = await supplied if inspect.isawaitable(supplied) else supplied
            if not isinstance(observation, BenchmarkObservation):
                raise TypeError("benchmark executor precisa retornar BenchmarkObservation")
            failures = self._failures(scenario, observation)
            score = self._score(scenario, observation)
            results.append(
                BenchmarkResult(
                    scenario.kind,
                    not failures,
                    score,
                    time.perf_counter() - started,
                    failures,
                    observation,
                )
            )
        return BenchmarkSuiteResult(tuple(results))

    @staticmethod
    def _failures(scenario: BenchmarkScenario, observation: BenchmarkObservation) -> tuple[str, ...]:
        failures: list[str] = []
        if observation.correctness < scenario.minimum_correctness:
            failures.append("correctness_below_threshold")
        if observation.completeness < scenario.minimum_completeness:
            failures.append("completeness_below_threshold")
        if observation.context_utilization > scenario.maximum_context_utilization:
            failures.append("context_utilization_above_threshold")
        if observation.tool_failures > scenario.maximum_tool_failures:
            failures.append("tool_failures_above_threshold")
        unnecessary = max(0, observation.delegation_count - observation.necessary_delegations)
        if unnecessary > scenario.maximum_unnecessary_delegations:
            failures.append("unnecessary_delegation")
        if scenario.require_convergence and not observation.converged:
            failures.append("not_converged")
        if scenario.require_recovery and not observation.recovered:
            failures.append("recovery_failed")
        return tuple(failures)

    @staticmethod
    def _score(scenario: BenchmarkScenario, observation: BenchmarkObservation) -> float:
        quality = (observation.correctness + observation.completeness) / 2
        context_penalty = max(0.0, observation.context_utilization - scenario.maximum_context_utilization)
        delegation_penalty = 0.05 * max(
            0, observation.delegation_count - observation.necessary_delegations
        )
        failure_penalty = 0.1 * observation.tool_failures
        convergence_penalty = 0.2 if scenario.require_convergence and not observation.converged else 0.0
        recovery_penalty = 0.2 if scenario.require_recovery and not observation.recovered else 0.0
        return max(
            0.0,
            min(1.0, quality - context_penalty - delegation_penalty - failure_penalty - convergence_penalty - recovery_penalty),
        )


__all__ = [
    "BenchmarkObservation",
    "BenchmarkResult",
    "BenchmarkScenario",
    "BenchmarkScenarioKind",
    "BenchmarkSuiteResult",
    "DEFAULT_BENCHMARK_SCENARIOS",
    "RegressionBenchmark",
]
