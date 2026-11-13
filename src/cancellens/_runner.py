"""Fresh-case lifecycle and bounded, prefix-checked cancellation exploration."""

from __future__ import annotations

import asyncio
import math
import platform
import sys
import sysconfig
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from importlib import import_module, metadata
from typing import Any, Generic, TypeVar

from ._model import (
    Attempt,
    Cancellation,
    Completion,
    Coverage,
    InvariantViolation,
    Outcome,
    PhaseResult,
    Report,
    Schedule,
    interpreter_key,
)
from ._observe import InstrumentationError, Observer, delegate

S = TypeVar("S")
_SCOPE_CANCELLED = object()


@dataclass(frozen=True)
class Case(Generic[S]):
    factory: Callable[[], Awaitable[S]]
    operation: Callable[[S], Coroutine[Any, Any, Any]]
    oracle: Callable[[S, Completion], Awaitable[None]]
    teardown: Callable[[S], Awaitable[None]]


@dataclass(frozen=True)
class Limits:
    max_points: int = 100
    max_attempts: int = 100
    max_depth: int = 2
    phase_timeout: float = 2.0
    total_timeout: float = 30.0
    cleanup_timeout: float = 0.1

    def __post_init__(self) -> None:
        for name in ("max_points", "max_attempts", "max_depth"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_depth > 2:
            raise ValueError("max_depth cannot exceed two")
        for name in ("phase_timeout", "total_timeout", "cleanup_timeout"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")


def _supported_loop() -> None:
    supported_version = (3, 10) <= sys.version_info[:2] <= (3, 15)
    if platform.python_implementation() != "CPython" or not supported_version:
        raise RuntimeError("CancelLens v0.1 supports CPython 3.10 through 3.15")
    loop = asyncio.get_running_loop()
    if type(loop).__module__ not in ("asyncio.unix_events", "asyncio.windows_events"):
        raise RuntimeError("CancelLens v0.1 requires the standard asyncio event loop")
    if loop.get_task_factory() is not None:
        raise RuntimeError("Custom/eager task factories are outside v0.1 scope")
    gil_enabled = getattr(sys, "_is_gil_enabled", None)
    if sysconfig.get_config_var("Py_GIL_DISABLED") or (
        gil_enabled is not None and not gil_enabled()
    ):
        raise RuntimeError("Free-threaded execution is outside v0.1 scope")


def _environment(cancellation_mode: str, backend: str) -> tuple[Any, str | None]:
    if cancellation_mode not in ("asyncio", "anyio"):
        raise ValueError("Unsupported cancellation mode")
    if backend != "asyncio":
        raise ValueError("Only the asyncio backend is supported")
    _supported_loop()
    if cancellation_mode == "asyncio":
        return None, None
    try:
        anyio = import_module("anyio")
        version = metadata.version("anyio")
    except (ImportError, metadata.PackageNotFoundError) as exc:
        raise RuntimeError("AnyIO mode requires the optional cancellens[anyio] dependency") from exc
    if version.split(".")[0] != "4":
        raise RuntimeError("AnyIO mode requires AnyIO 4")
    if anyio.get_cancelled_exc_class() is not asyncio.CancelledError:
        raise RuntimeError("AnyIO mode requires the asyncio backend")
    return anyio, version


class _Deadline(Exception):
    pass


class _PhaseCancelled(Exception):
    pass


def _error_type(error: Exception) -> str:
    return (
        "CancelledError (lifecycle task)"
        if isinstance(error, _PhaseCancelled)
        else type(error).__name__
    )


class _Session:
    def __init__(self, limits: Limits, anyio: Any = None) -> None:
        self.limits = limits
        self.anyio = anyio
        self.deadline = asyncio.get_running_loop().time() + limits.total_timeout
        self.pending: set[asyncio.Task[Any]] = set()

    def remaining(self) -> float:
        return self.deadline - asyncio.get_running_loop().time()

    def cleanup_scope(self) -> Any:
        # A caller may itself be level-cancelled. Protect the bounded final
        # windows from its scope while still propagating its original exception.
        return self.anyio.CancelScope(shield=True) if self.anyio is not None else nullcontext()

    async def phase(
        self, call: Callable[[], Awaitable[Any]], *, final: bool = False, operation: bool = False
    ) -> Any:
        # Oracle/teardown have a reserved per-phase window even if the exploration
        # deadline expires. They are needed to observe state before forced cleanup.
        timeout = self.limits.phase_timeout
        if not final:
            timeout = min(timeout, self.remaining())
        if timeout <= 0:
            raise _Deadline

        async def invoke() -> Any:
            return await call()

        task = asyncio.create_task(invoke())
        self.pending.add(task)
        with self.cleanup_scope() if final else nullcontext():
            done, _ = await asyncio.wait({task}, timeout=timeout)
        if task not in done:
            raise _Deadline
        self.pending.discard(task)
        if operation:
            # Inspect completion outside the observed Task. Its done callbacks must
            # see the same cancelled()/exception() state as an ordinary operation.
            try:
                value = task.result()
                if value is _SCOPE_CANCELLED:
                    return Completion("scope_cancelled")
                return Completion("returned", value)
            except InstrumentationError:
                raise
            except asyncio.CancelledError as exc:
                return Completion("cancelled", exception=exc)
            except Exception as exc:
                return Completion("raised", exception=exc)
        if task.cancelled():
            # asyncio.wait itself propagates cancellation of the sweep caller.
            # A cancelled factory/oracle/teardown task is a different event.
            raise _PhaseCancelled
        return task.result()

    async def drain(self) -> bool:
        if not self.pending:
            return True
        for task in self.pending:
            if not task.done():
                task.cancel()
        with self.cleanup_scope():
            done, pending = await asyncio.wait(self.pending, timeout=self.limits.cleanup_timeout)
        for task in done:
            if not task.cancelled():
                task.exception()
        self.pending = set(pending)
        # Consume eventual exceptions without claiming that the task was stopped.
        for task in pending:
            task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        return not pending


async def _finalize(case: Case[S], state: S, attempt: Attempt, session: _Session) -> None:
    with session.cleanup_scope():
        try:
            await session.phase(lambda: case.teardown(state), final=True)
            attempt.teardown = PhaseResult(Outcome.PASS)
        except _Deadline:
            attempt.teardown = PhaseResult(Outcome.UNRESOLVED, "teardown deadline expired")
        except Exception as exc:
            attempt.teardown = PhaseResult(Outcome.ERROR, _error_type(exc))
        finally:
            drained = await session.drain()
    if not drained:
        attempt.reasons.append("harness tasks remain pending; external watchdog required")
        attempt.trace_complete = False
    if attempt.outcome not in (Outcome.FAIL, Outcome.ERROR):
        if attempt.teardown.outcome is Outcome.ERROR:
            attempt.outcome = Outcome.ERROR
        elif attempt.teardown.outcome is Outcome.UNRESOLVED or not drained:
            attempt.outcome = Outcome.UNRESOLVED


async def _attempt(
    case: Case[S],
    schedule: Schedule,
    session: _Session,
    *,
    anyio: Any = None,
    anyio_version: str | None = None,
) -> Attempt:
    attempt = Attempt(schedule)
    if schedule.interpreter != interpreter_key():
        attempt.reasons.append("schedule interpreter differs; replay refused")
        return attempt
    if schedule.anyio_version != anyio_version:
        attempt.reasons.append("schedule AnyIO version differs; replay refused")
        return attempt
    observer = Observer(attempt, session.limits.max_points)
    state: S
    state_ready = False

    async def setup() -> S:
        nonlocal state, state_ready
        # Transfer ownership before waking the caller; cancellation may win the
        # race between factory completion and asyncio.wait's notification.
        state = await case.factory()
        state_ready = True
        return state

    try:
        await session.phase(setup)
        attempt.setup = PhaseResult(Outcome.PASS)
    except _Deadline:
        attempt.setup = PhaseResult(Outcome.UNRESOLVED, "factory deadline expired")
        attempt.reasons.append("factory deadline expired")
        if not await session.drain():
            attempt.reasons.append("factory task remains pending after harness cancellation")
        if state_ready:
            await _finalize(case, state, attempt, session)
        return attempt
    except Exception as exc:
        attempt.setup = PhaseResult(Outcome.ERROR, _error_type(exc))
        attempt.outcome = Outcome.ERROR
        attempt.reasons.append(f"factory error: {_error_type(exc)}")
        if state_ready:
            await _finalize(case, state, attempt, session)
        return attempt
    except BaseException:
        await session.drain()
        if state_ready:
            await _finalize(case, state, attempt, session)
        raise

    infrastructure_error = False
    try:

        async def operate() -> Any:
            if anyio is None:
                return await delegate(case.operation(state), observer.point, observer.exception)
            # The scope and every step of coroutine delegation belong to this
            # operation Task. Oracle/teardown Tasks never enter this scope.
            body_error: BaseException | None = None
            try:
                with anyio.CancelScope() as scope:
                    observer.scope = scope
                    try:
                        return await delegate(
                            case.operation(state), observer.point, observer.exception
                        )
                    except BaseException as exc:
                        body_error = exc
                        raise
            except Exception as exc:
                # AnyIO can suppress its cancellation inside a mixed exception
                # group and raise the remaining application failures as a new
                # group. Its public suppression flag distinguishes that path
                # from a failed scope entry/exit (including stack corruption).
                if exc is not body_error and not (
                    body_error is not None and observer.scope.cancelled_caught
                ):
                    raise InstrumentationError("AnyIO scope lifecycle failed") from exc
                raise
            finally:
                if observer.scope is not None:
                    observer.scope_exited()
            return _SCOPE_CANCELLED

        try:
            attempt.completion = await session.phase(operate, operation=True)
        except _Deadline:
            attempt.completion = Completion("pending")
            attempt.reasons.append("operation deadline expired")
            attempt.trace_complete = False
        except Exception as exc:
            infrastructure_error = True
            attempt.reasons.append(f"instrumentation error: {type(exc).__name__}")

        observer.close()  # A timed-out operation must not inject cancellation during the oracle.

        if observer.scope_error is not None:
            infrastructure_error = True
            attempt.reasons.append("instrumentation error: scope cancellation failed")
        missing = [i for i in schedule.cancel_at if i > len(attempt.trace)]
        if missing:
            attempt.reasons.append(f"cancellation point not reached: {missing}")
            if anyio is not None:
                attempt.cancellations.extend(
                    Cancellation(i, delivered=None, target_reached=False, prefix_matched=False)
                    for i in missing
                )
        if anyio is not None:
            observer.assess_scope_delivery()
        if any(not c.accepted or not c.delivered for c in attempt.cancellations):
            attempt.reasons.append("cancellation request was not accepted and delivered")
        if len(attempt.cancellations) != len(schedule.cancel_at):
            attempt.reasons.append("requested cancellation schedule was not executed")
        attempt.valid_schedule = (
            not attempt.reasons
            and not infrastructure_error
            and attempt.completion.state in ("returned", "cancelled", "raised", "scope_cancelled")
        )
        # The oracle runs while timed-out/background work is still untouched by
        # harness cancellation. It may wait for application-owned cleanup itself.
        try:
            await session.phase(lambda: case.oracle(state, attempt.completion), final=True)
            attempt.oracle = PhaseResult(Outcome.PASS)
        except InvariantViolation as exc:
            attempt.oracle = PhaseResult(Outcome.FAIL, str(exc))
        except _Deadline:
            attempt.oracle = PhaseResult(Outcome.UNRESOLVED, "oracle deadline expired")
        except Exception as exc:
            attempt.oracle = PhaseResult(Outcome.ERROR, _error_type(exc))
        if attempt.valid_schedule and attempt.oracle.outcome is Outcome.FAIL:
            attempt.outcome = Outcome.FAIL
        elif infrastructure_error or attempt.oracle.outcome is Outcome.ERROR:
            attempt.outcome = Outcome.ERROR
        elif attempt.valid_schedule and attempt.oracle.outcome is Outcome.PASS:
            attempt.outcome = Outcome.PASS
    finally:
        observer.close()
        await _finalize(case, state, attempt, session)
    return attempt


async def sweep(
    case: Case[S],
    *,
    limits: Limits | None = None,
    cancellation_mode: str = "asyncio",
    backend: str = "asyncio",
) -> Report:
    """Two baselines, then single cancellations and discovered double cancellations."""
    anyio, anyio_version = _environment(cancellation_mode, backend)
    limits = limits or Limits()
    session = _Session(limits, anyio)
    coverage = Coverage()
    report = Report(
        [],
        coverage,
        asdict(limits),
        cancellation_mode=cancellation_mode,
        backend=backend,
        anyio_version=anyio_version,
    )

    def schedule_for(cancel_at: tuple[int, ...] = (), prefix: tuple[Any, ...] = ()) -> Schedule:
        return Schedule(
            cancel_at,
            prefix,
            cancellation_mode=cancellation_mode,
            backend=backend,
            anyio_version=anyio_version,
        )

    async def run(schedule: Schedule) -> Attempt | None:
        if len(report.attempts) >= limits.max_attempts or session.remaining() <= 0:
            if "attempt/time budget exhausted" not in coverage.reasons:
                coverage.reasons.append("attempt/time budget exhausted")
            return None
        attempt = await _attempt(case, schedule, session, anyio=anyio, anyio_version=anyio_version)
        if anyio is not None:
            await import_module("anyio.lowlevel").checkpoint_if_cancelled()
        report.attempts.append(attempt)
        coverage.attempts_run += 1
        if not attempt.trace_complete:
            coverage.reasons.append("attempt trace incomplete")
        return attempt

    baseline = await run(schedule_for())
    if baseline is None:
        return report
    coverage.baseline_points = len(baseline.trace)
    coverage.single_planned = len(baseline.trace)
    if baseline.outcome is not Outcome.PASS or not baseline.trace_complete:
        coverage.reasons.append("baseline could not be established")
        return report
    repeat = await run(schedule_for())
    if repeat is None:
        return report
    if repeat.trace != baseline.trace or not repeat.trace_complete:
        if repeat.outcome not in (Outcome.FAIL, Outcome.ERROR):
            repeat.outcome = Outcome.UNRESOLVED
        repeat.reasons.append("baseline trace is not repeatable")
        coverage.reasons.append("baseline trace is not repeatable")
        return report
    if repeat.outcome is not Outcome.PASS:
        coverage.reasons.append("repeat baseline could not be established")
        return report
    if not baseline.trace:
        coverage.reasons.append("no suspension points; no cancellation coverage")
        return report

    singles: list[Attempt] = []
    for point in baseline.trace:
        schedule = schedule_for((point.index,), tuple(baseline.trace[: point.index]))
        attempt = await run(schedule)
        if attempt is None:
            break
        singles.append(attempt)
        if attempt.valid_schedule:
            coverage.single_valid += 1

    doubles: list[Schedule] = []
    if limits.max_depth == 2 and cancellation_mode == "asyncio":
        for single in singles:
            first = single.schedule.cancel_at[0]
            if not single.valid_schedule:
                coverage.reasons.append("double path unavailable from an invalid single schedule")
                continue
            for point in single.trace[first:]:
                doubles.append(Schedule((first, point.index), tuple(single.trace[: point.index])))
        coverage.double_planned = len(doubles)
        for schedule in doubles:
            attempt = await run(schedule)
            if attempt is None:
                break
            if attempt.valid_schedule:
                coverage.double_valid += 1

    coverage.complete = (
        not coverage.reasons
        and coverage.single_valid == coverage.single_planned
        and coverage.double_valid == coverage.double_planned
        and all(a.oracle.outcome in (Outcome.PASS, Outcome.FAIL) for a in report.attempts)
        and all(a.teardown.outcome is Outcome.PASS for a in report.attempts)
    )
    if not coverage.complete and not coverage.reasons:
        coverage.reasons.append("one or more schedules or lifecycle phases could not be evaluated")
    return report


async def replay(
    case: Case[S],
    schedule: Schedule,
    *,
    limits: Limits | None = None,
    cancellation_mode: str | None = None,
    backend: str = "asyncio",
) -> Report:
    """Run exactly the supplied schedule on a fresh case; no claim of sweep coverage."""
    if cancellation_mode is None:
        cancellation_mode = schedule.cancellation_mode
    if cancellation_mode != schedule.cancellation_mode or backend != schedule.backend:
        raise ValueError("Replay cancellation mode/backend differs from schedule")
    anyio, anyio_version = _environment(cancellation_mode, backend)
    limits = limits or Limits()
    if len(schedule.cancel_at) > limits.max_depth:
        raise ValueError("schedule exceeds max_depth")
    session = _Session(limits, anyio)
    attempt = await _attempt(case, schedule, session, anyio=anyio, anyio_version=anyio_version)
    if anyio is not None:
        await import_module("anyio.lowlevel").checkpoint_if_cancelled()
    depth = len(schedule.cancel_at)
    coverage = Coverage(
        single_planned=int(depth == 1),
        single_valid=int(depth == 1 and attempt.valid_schedule),
        double_planned=int(depth == 2),
        double_valid=int(depth == 2 and attempt.valid_schedule),
        attempts_run=1,
        complete=(
            attempt.valid_schedule
            and attempt.trace_complete
            and attempt.outcome in (Outcome.PASS, Outcome.FAIL)
            and attempt.teardown.outcome is Outcome.PASS
        ),
    )
    if not coverage.complete:
        coverage.reasons.append("selected schedule could not be fully evaluated")
    return Report(
        [attempt],
        coverage,
        asdict(limits),
        mode="replay",
        cancellation_mode=cancellation_mode,
        backend=backend,
        anyio_version=anyio_version,
    )
