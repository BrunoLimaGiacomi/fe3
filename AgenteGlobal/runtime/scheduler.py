"""Deterministic in-process DAG scheduling and resource coordination."""

from __future__ import annotations

import asyncio
import posixpath
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import (
    AgentError,
    AgentErrorCode,
    AgentResult,
    AgentResultStatus,
    TaskLifecycleStatus,
    TaskSpec,
    TaskState,
)
from .events import EventBus
from .hooks import HookError, HookManager, HookPoint, emit_if_configured


TaskExecutor = Callable[[TaskSpec, int], Awaitable[AgentResult]]
_GLOBAL_MUTATION_RESOURCE = "resource:/workspace/*"


class DAGValidationError(ValueError):
    """The task graph cannot be scheduled safely."""


class TaskTransitionError(RuntimeError):
    """A caller attempted an invalid task lifecycle transition."""


@dataclass(frozen=True, slots=True)
class ResourceLease:
    task_id: str
    read_set: frozenset[str]
    write_set: frozenset[str]


class ResourceLockManager:
    """Event-loop-confined shared-read/exclusive-write resource locks.

    Resource names are conservative logical identifiers.  Slash direction and
    case are normalized so Windows paths cannot bypass a declared conflict.
    Acquisition is atomic: a task either owns every declared resource or none.
    """

    def __init__(self) -> None:
        self._leases: dict[str, ResourceLease] = {}

    @staticmethod
    def normalize(resource: str) -> str:
        normalized = posixpath.normpath(resource.strip().replace("\\", "/"))
        return normalized.casefold()

    def _sets(self, read_set: Sequence[str], write_set: Sequence[str]) -> tuple[frozenset[str], frozenset[str]]:
        writes = frozenset(self.normalize(item) for item in write_set)
        reads = frozenset(self.normalize(item) for item in read_set) - writes
        return reads, writes

    def conflicting_tasks(
        self,
        read_set: Sequence[str],
        write_set: Sequence[str],
    ) -> frozenset[str]:
        reads, writes = self._sets(read_set, write_set)
        conflicts: set[str] = set()
        for lease in self._leases.values():
            global_conflict = (
                _GLOBAL_MUTATION_RESOURCE in writes
                or _GLOBAL_MUTATION_RESOURCE in lease.write_set
            )
            if global_conflict or writes & (lease.read_set | lease.write_set) or reads & lease.write_set:
                conflicts.add(lease.task_id)
        return frozenset(conflicts)

    def try_acquire(
        self,
        task_id: str,
        read_set: Sequence[str],
        write_set: Sequence[str],
    ) -> ResourceLease | None:
        if task_id in self._leases:
            raise RuntimeError(f"Task {task_id!r} already owns a resource lease.")
        if self.conflicting_tasks(read_set, write_set):
            return None
        reads, writes = self._sets(read_set, write_set)
        lease = ResourceLease(task_id=task_id, read_set=reads, write_set=writes)
        self._leases[task_id] = lease
        return lease

    def release(self, task_id: str) -> bool:
        return self._leases.pop(task_id, None) is not None

    @property
    def active_leases(self) -> tuple[ResourceLease, ...]:
        return tuple(self._leases.values())


@dataclass(frozen=True, slots=True)
class SchedulerResult:
    states: tuple[TaskState, ...]

    @property
    def successful(self) -> bool:
        return all(state.status is TaskLifecycleStatus.COMPLETED for state in self.states)

    @property
    def results(self) -> tuple[AgentResult, ...]:
        return tuple(state.result for state in self.states if state.result is not None)


_ALLOWED_TRANSITIONS: Mapping[TaskLifecycleStatus, frozenset[TaskLifecycleStatus]] = {
    TaskLifecycleStatus.PENDING: frozenset(
        {TaskLifecycleStatus.READY, TaskLifecycleStatus.BLOCKED, TaskLifecycleStatus.CANCELLED}
    ),
    TaskLifecycleStatus.READY: frozenset(
        {TaskLifecycleStatus.RUNNING, TaskLifecycleStatus.BLOCKED, TaskLifecycleStatus.CANCELLED}
    ),
    TaskLifecycleStatus.RUNNING: frozenset(
        {
            TaskLifecycleStatus.COMPLETED,
            TaskLifecycleStatus.FAILED_RETRYABLE,
            TaskLifecycleStatus.FAILED_FINAL,
            TaskLifecycleStatus.BLOCKED,
            TaskLifecycleStatus.CANCELLED,
        }
    ),
    TaskLifecycleStatus.BLOCKED: frozenset(
        {TaskLifecycleStatus.BLOCKED, TaskLifecycleStatus.READY, TaskLifecycleStatus.CANCELLED}
    ),
    TaskLifecycleStatus.FAILED_RETRYABLE: frozenset(
        {TaskLifecycleStatus.READY, TaskLifecycleStatus.FAILED_FINAL, TaskLifecycleStatus.CANCELLED}
    ),
    TaskLifecycleStatus.COMPLETED: frozenset(),
    TaskLifecycleStatus.FAILED_FINAL: frozenset(),
    TaskLifecycleStatus.CANCELLED: frozenset(),
}

_TERMINAL_STATES = frozenset(
    {
        TaskLifecycleStatus.COMPLETED,
        TaskLifecycleStatus.BLOCKED,
        TaskLifecycleStatus.FAILED_FINAL,
        TaskLifecycleStatus.CANCELLED,
    }
)


class DAGScheduler:
    """Run TaskSpecs according to dependencies, retries and resource locks."""

    def __init__(
        self,
        tasks: Sequence[TaskSpec],
        executor: TaskExecutor,
        *,
        max_concurrency: int = 4,
        event_bus: EventBus | None = None,
        source: str = "scheduler",
        initial_results: Mapping[str, AgentResult] | None = None,
        hooks: HookManager | None = None,
    ) -> None:
        if max_concurrency < 1 or max_concurrency > 128:
            raise ValueError("max_concurrency must be between 1 and 128.")
        self.tasks = tuple(tasks)
        self.executor = executor
        self.max_concurrency = max_concurrency
        self.event_bus = event_bus
        self.source = source
        self.hooks = hooks
        self._task_by_id = self.validate_dag(self.tasks)
        self._order = {task.task_id: index for index, task in enumerate(self.tasks)}
        seeded = dict(initial_results or {})
        unknown_seeded = set(seeded) - set(self._task_by_id)
        if unknown_seeded:
            raise DAGValidationError(f"Initial results reference unknown tasks: {sorted(unknown_seeded)}")
        self._states: dict[str, TaskState] = {}
        for task in self.tasks:
            result = seeded.get(task.task_id)
            if result is not None and result.task_id != task.task_id:
                raise DAGValidationError(f"Initial result task_id does not match {task.task_id!r}.")
            if result is not None and result.status is not AgentResultStatus.COMPLETED:
                raise DAGValidationError("Only completed AgentResult values can seed a scheduler pass.")
            self._states[task.task_id] = TaskState(
                task_id=task.task_id,
                status=(TaskLifecycleStatus.COMPLETED if result is not None else TaskLifecycleStatus.PENDING),
                result=result,
                reason=("seeded_completed_result" if result is not None else None),
            )
        self._locks = ResourceLockManager()
        self._resource_blocked: set[str] = set()

    @staticmethod
    def validate_dag(tasks: Sequence[TaskSpec]) -> dict[str, TaskSpec]:
        task_by_id = {task.task_id: task for task in tasks}
        if len(task_by_id) != len(tasks):
            raise DAGValidationError("Task ids must be unique.")
        unknown = {
            dependency
            for task in tasks
            for dependency in task.dependencies
            if dependency not in task_by_id
        }
        if unknown:
            raise DAGValidationError(f"Unknown task dependencies: {sorted(unknown)}")

        visiting: list[str] = []
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visited:
                return
            if task_id in visiting:
                cycle_start = visiting.index(task_id)
                cycle = [*visiting[cycle_start:], task_id]
                raise DAGValidationError(f"Task dependency cycle detected: {' -> '.join(cycle)}")
            visiting.append(task_id)
            for dependency in task_by_id[task_id].dependencies:
                visit(dependency)
            visiting.pop()
            visited.add(task_id)

        for task in tasks:
            visit(task.task_id)
        return task_by_id

    async def _emit(self, name: str, payload: Mapping[str, Any]) -> None:
        if self.event_bus is not None:
            await self.event_bus.emit(name, source=self.source, payload=payload)

    async def _hook(
        self,
        point: HookPoint,
        task: TaskSpec,
        *,
        validation_passed: bool | None = None,
    ) -> None:
        await emit_if_configured(
            self.hooks,
            point,
            task.task_id,
            metadata={"task_id": task.task_id, "read_only": task.read_only},
            validation_passed=validation_passed,
            correlation_id=task.task_id,
        )

    async def _transition(
        self,
        task_id: str,
        status: TaskLifecycleStatus,
        *,
        reason: str | None = None,
        result: AgentResult | None = None,
        increment_attempt: bool = False,
        clear_result: bool = False,
    ) -> TaskState:
        current = self._states[task_id]
        if status not in _ALLOWED_TRANSITIONS[current.status]:
            raise TaskTransitionError(f"Invalid task transition {current.status.value} -> {status.value} for {task_id}.")
        next_result = None if clear_result else (result if result is not None else current.result)
        updated = TaskState(
            task_id=task_id,
            status=status,
            selected_agents=current.selected_agents,
            attempt=current.attempt + (1 if increment_attempt else 0),
            result=next_result,
            reason=reason,
        )
        self._states[task_id] = updated
        await self._emit(
            f"task.{status.value}",
            {
                "task_id": task_id,
                "status": status.value,
                "previous_status": current.status.value,
                "attempt": updated.attempt,
                "reason": reason,
            },
        )
        return updated

    def _dependencies_completed(self, task: TaskSpec) -> bool:
        return all(
            self._states[dependency].status is TaskLifecycleStatus.COMPLETED
            for dependency in task.dependencies
        )

    @staticmethod
    def _resource_sets(task: TaskSpec) -> tuple[Sequence[str], Sequence[str]]:
        if not task.read_only and not task.write_set:
            return task.read_set, (_GLOBAL_MUTATION_RESOURCE,)
        return task.read_set, task.write_set

    def _failed_dependencies(self, task: TaskSpec) -> list[str]:
        return [
            dependency
            for dependency in task.dependencies
            if self._states[dependency].status
            in {TaskLifecycleStatus.BLOCKED, TaskLifecycleStatus.FAILED_FINAL, TaskLifecycleStatus.CANCELLED}
            and dependency not in self._resource_blocked
        ]

    @staticmethod
    def _exception_result(task_id: str, exc: BaseException) -> AgentResult:
        return AgentResult(
            task_id=task_id,
            status=AgentResultStatus.FAILED,
            summary="A execução da task falhou antes de produzir um AgentResult válido.",
            errors=[
                AgentError(
                    code=AgentErrorCode.EXECUTION_ERROR,
                    message=f"{type(exc).__name__}: task executor raised an exception.",
                    retryable=False,
                )
            ],
        )

    async def _complete_running_task(self, task_id: str, future: asyncio.Task[AgentResult]) -> None:
        task = self._task_by_id[task_id]
        self._locks.release(task_id)
        try:
            raw_result = future.result()
        except asyncio.CancelledError:
            try:
                await self._hook(HookPoint.AFTER_TASK, task, validation_passed=False)
            except HookError:
                pass
            await self._transition(task_id, TaskLifecycleStatus.CANCELLED, reason="scheduler_cancelled")
            return
        except Exception as exc:
            result = self._exception_result(task_id, exc)
        else:
            try:
                result = (
                    raw_result
                    if isinstance(raw_result, AgentResult)
                    else AgentResult.model_validate(raw_result)
                )
            except (TypeError, ValueError):
                result = AgentResult(
                    task_id=task_id,
                    status=AgentResultStatus.FAILED,
                    summary="O executor retornou um payload fora do contrato AgentResult.",
                    errors=[
                        AgentError(
                            code=AgentErrorCode.PROTOCOL_ERROR,
                            message="Task executor result failed local AgentResult validation.",
                        )
                    ],
                )

        if result.task_id != task_id:
            result = AgentResult(
                task_id=task_id,
                status=AgentResultStatus.FAILED,
                summary="O executor retornou AgentResult para outra task.",
                errors=[
                    AgentError(
                        code=AgentErrorCode.PROTOCOL_ERROR,
                        message=f"AgentResult.task_id recebido: {result.task_id!r}.",
                    )
                ],
            )

        try:
            await self._hook(
                HookPoint.AFTER_TASK,
                task,
                validation_passed=result.status is AgentResultStatus.COMPLETED,
            )
        except HookError as error:
            result = AgentResult(
                task_id=task_id,
                status=AgentResultStatus.FAILED,
                summary="Hook after_task recusou ou não validou o resultado.",
                errors=[
                    AgentError(
                        code=AgentErrorCode.VALIDATION_ERROR,
                        message=f"{type(error).__name__}: {str(error)[:1000]}",
                    )
                ],
            )

        if result.status is AgentResultStatus.COMPLETED:
            await self._transition(task_id, TaskLifecycleStatus.COMPLETED, result=result)
            return
        if result.status is AgentResultStatus.CANCELLED:
            await self._transition(task_id, TaskLifecycleStatus.CANCELLED, result=result, reason="executor_cancelled")
            return
        if result.status is AgentResultStatus.BLOCKED:
            await self._transition(task_id, TaskLifecycleStatus.BLOCKED, result=result, reason="executor_blocked")
            return

        retryable = any(error.retryable for error in result.errors)
        current = self._states[task_id]
        if retryable and current.attempt <= task.limits.max_retries:
            await self._transition(
                task_id,
                TaskLifecycleStatus.FAILED_RETRYABLE,
                result=result,
                reason="retryable_failure",
            )
            return
        await self._transition(task_id, TaskLifecycleStatus.FAILED_FINAL, result=result, reason="final_failure")

    async def _execute_with_timeout(self, task: TaskSpec, attempt: int) -> AgentResult:
        """Apply the TaskSpec timeout at the scheduler boundary for every executor."""

        try:
            async with asyncio.timeout(task.limits.timeout_seconds):
                return await self.executor(task, attempt)
        except TimeoutError:
            return AgentResult(
                task_id=task.task_id,
                status=AgentResultStatus.FAILED,
                summary="A task excedeu o timeout controlado pelo DAG Scheduler.",
                errors=[
                    AgentError(
                        code=AgentErrorCode.TIMEOUT,
                        message=f"Execução excedeu {task.limits.timeout_seconds} segundos.",
                        retryable=True,
                    )
                ],
            )

    async def run(self) -> SchedulerResult:
        await self._emit(
            "scheduler.started",
            {"status": "running", "task_count": len(self.tasks), "max_concurrency": self.max_concurrency},
        )
        task_by_id = {task.task_id: task for task in self.tasks}
        for state in self._states.values():
            task = task_by_id[state.task_id]
            await self._emit(
                "task.created",
                {
                    "task_id": state.task_id,
                    "status": state.status.value,
                    "attempt": state.attempt,
                    "dependency_count": len(task.dependencies),
                },
            )

        running: dict[asyncio.Task[AgentResult], str] = {}
        try:
            while True:
                for task in self.tasks:
                    state = self._states[task.task_id]
                    if state.status in _TERMINAL_STATES and task.task_id not in self._resource_blocked:
                        continue
                    failed_dependencies = self._failed_dependencies(task)
                    if failed_dependencies:
                        was_resource_blocked = task.task_id in self._resource_blocked
                        self._resource_blocked.discard(task.task_id)
                        if state.status in {TaskLifecycleStatus.PENDING, TaskLifecycleStatus.READY} or (
                            state.status is TaskLifecycleStatus.BLOCKED
                            and was_resource_blocked
                        ):
                            await self._transition(
                                task.task_id,
                                TaskLifecycleStatus.BLOCKED,
                                reason=f"dependency_failed:{','.join(failed_dependencies)}",
                            )
                        continue
                    if state.status is TaskLifecycleStatus.FAILED_RETRYABLE:
                        await self._transition(
                            task.task_id,
                            TaskLifecycleStatus.READY,
                            reason="retry_scheduled",
                            clear_result=True,
                        )
                    elif state.status is TaskLifecycleStatus.PENDING and self._dependencies_completed(task):
                        await self._transition(task.task_id, TaskLifecycleStatus.READY, reason="dependencies_completed")
                    elif state.status is TaskLifecycleStatus.BLOCKED and task.task_id in self._resource_blocked:
                        self._resource_blocked.discard(task.task_id)
                        await self._transition(task.task_id, TaskLifecycleStatus.READY, reason="resource_recheck")

                slots = self.max_concurrency - len(running)
                ready = sorted(
                    (
                        task
                        for task in self.tasks
                        if self._states[task.task_id].status is TaskLifecycleStatus.READY
                    ),
                    key=lambda item: self._order[item.task_id],
                )
                for task in ready:
                    if slots <= 0:
                        break
                    read_set, write_set = self._resource_sets(task)
                    conflicts = self._locks.conflicting_tasks(read_set, write_set)
                    lease = self._locks.try_acquire(task.task_id, read_set, write_set)
                    if lease is None:
                        self._resource_blocked.add(task.task_id)
                        await self._transition(
                            task.task_id,
                            TaskLifecycleStatus.BLOCKED,
                            reason=f"resource_conflict:{','.join(sorted(conflicts))}",
                        )
                        continue
                    try:
                        await self._hook(HookPoint.BEFORE_TASK, task)
                    except HookError as error:
                        self._locks.release(task.task_id)
                        await self._transition(
                            task.task_id,
                            TaskLifecycleStatus.BLOCKED,
                            reason=f"hook_blocked:{type(error).__name__}",
                        )
                        continue
                    state = await self._transition(
                        task.task_id,
                        TaskLifecycleStatus.RUNNING,
                        reason="resources_acquired",
                        increment_attempt=True,
                        clear_result=True,
                    )
                    future = asyncio.create_task(
                        self._execute_with_timeout(task, state.attempt),
                        name=f"dag:{task.task_id}:attempt:{state.attempt}",
                    )
                    running[future] = task.task_id
                    slots -= 1

                if running:
                    done, _pending = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    for future in sorted(done, key=lambda item: self._order[running[item]]):
                        task_id = running.pop(future)
                        await self._complete_running_task(task_id, future)
                    continue

                if all(state.status in _TERMINAL_STATES for state in self._states.values()):
                    break

                unresolved = [
                    task_id
                    for task_id, state in self._states.items()
                    if state.status not in _TERMINAL_STATES
                ]
                for task_id in unresolved:
                    await self._transition(task_id, TaskLifecycleStatus.BLOCKED, reason="scheduler_deadlock")
                break
        except asyncio.CancelledError:
            for future in running:
                future.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            for task_id in tuple(self._locks._leases):
                self._locks.release(task_id)
            for task_id, state in tuple(self._states.items()):
                if state.status not in _TERMINAL_STATES or task_id in self._resource_blocked:
                    await self._transition(task_id, TaskLifecycleStatus.CANCELLED, reason="scheduler_cancelled")
            self._resource_blocked.clear()
            await self._emit("scheduler.cancelled", {"status": "cancelled"})
            raise

        result = SchedulerResult(states=tuple(self._states[task.task_id] for task in self.tasks))
        await self._emit(
            "scheduler.completed" if result.successful else "scheduler.failed",
            {"status": "completed" if result.successful else "failed", "successful": result.successful},
        )
        return result
