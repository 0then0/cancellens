"""Public scope cancellation contracts; always run under validation/watchdog.py."""

from __future__ import annotations

import asyncio
import builtins
import json
import time
from dataclasses import replace
from importlib import import_module

import pytest

from cancellens import Case, Limits, Outcome, Schedule, replay, sweep

anyio = pytest.importorskip("anyio")

from examples.anyio_cleanup import callback_case, resource_case  # noqa: E402


def plain_case(operation, *, factory=None, oracle=None, teardown=None):
    async def make():
        return {}

    async def check(state, completion):
        pass

    async def close(state):
        pass

    return Case(factory or make, operation, oracle or check, teardown or close)


def run(case, **kwargs):
    return asyncio.run(sweep(case, cancellation_mode="anyio", **kwargs))


def selected(report, point=1):
    return next(a for a in report.attempts if a.schedule.cancel_at == (point,))


@pytest.mark.parametrize("case_factory", [resource_case, callback_case])
@pytest.mark.parametrize("shielded", [False, True])
def test_single_scope_request_contrasts_with_asyncio_and_replays(case_factory, shielded):
    observations = []
    case = case_factory(shielded=shielded, observations=observations)
    report = run(case)
    assert report.outcome is (Outcome.PASS if shielded else Outcome.FAIL)
    assert len(report.attempts) == 3  # Two baselines and exactly one request.
    assert report.coverage.single_valid == 1
    assert report.coverage.double_planned == 0
    assert report.coverage.complete
    attempt = selected(report)
    assert attempt.valid_schedule
    assert attempt.completion.state == "scope_cancelled"
    assert len(attempt.cancellations) == 1
    cancellation = attempt.cancellations[0]
    assert cancellation.target_reached and cancellation.prefix_matched
    assert cancellation.scope_cancel_called and cancellation.scope_cancelled_caught
    assert cancellation.exception_observed and cancellation.delivered
    assert attempt.cancellation_exceptions >= (1 if shielded else 2)
    assert all(s.teardown_done for s in observations)
    if case_factory is resource_case:
        assert observations[-1].released is shielded
        assert observations[-1].reused is shielded
    else:
        assert observations[-1].events == (["start", "terminal"] if shielded else ["start"])
    saved = json.loads(json.dumps(attempt.schedule.to_dict()))
    assert saved["cancellation_mode"] == "anyio"
    assert saved["backend"] == "asyncio"
    assert saved["anyio_version"]
    schedule = Schedule.from_dict(saved)
    assert schedule == attempt.schedule
    replayed = asyncio.run(replay(case, schedule))
    assert replayed.outcome is attempt.outcome
    assert replayed.attempts[0].valid_schedule
    edge = asyncio.run(sweep(case, limits=Limits(max_depth=1)))
    edge.assert_passed()
    assert selected(edge).completion.state == "cancelled"


def test_unstable_baselines_prevent_scope_requests():
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return {"branch": calls == 1}

    async def operation(state):
        if state["branch"]:
            await asyncio.sleep(0.001)
        else:
            await asyncio.sleep(0.001)

    report = run(plain_case(operation, factory=factory))
    assert report.outcome is Outcome.UNRESOLVED
    assert len(report.attempts) == 2
    assert "baseline trace is not repeatable" in report.coverage.reasons
    assert all(not a.cancellations for a in report.attempts)


def test_replay_prefix_divergence_withholds_scope_cancellation():
    async def operation(state):
        if state.get("other"):
            await asyncio.sleep(0.001)
        else:
            await asyncio.sleep(0.001)

    case = plain_case(operation)
    schedule = selected(run(case)).schedule

    async def other():
        return {"other": True}

    report = asyncio.run(replay(replace(case, factory=other), schedule))
    attempt = report.attempts[0]
    assert report.outcome is Outcome.UNRESOLVED
    assert len(attempt.cancellations) == 1
    record = attempt.cancellations[0]
    assert record.target_reached and not record.prefix_matched
    assert not record.scope_cancel_called and not record.accepted
    assert any("diverged" in reason for reason in attempt.reasons)


def test_scope_requests_before_inside_and_after_shield_boundary():
    states = []

    async def factory():
        state = {"steps": []}
        states.append(state)
        return state

    async def operation(state):
        await asyncio.sleep(0.001)
        state["steps"].append("before")
        with anyio.fail_after(0.1, shield=True):
            await asyncio.sleep(0.001)
            state["steps"].append("shielded")
        await asyncio.sleep(0.001)
        state["steps"].append("after")

    report = run(plain_case(operation, factory=factory))
    report.assert_passed()
    assert report.coverage.single_valid == 3
    assert [s["steps"] for s in states[2:]] == [[], ["before", "shielded"], ["before", "shielded"]]
    assert all(selected(report, p).cancellations[0].scope_cancelled_caught for p in (1, 2, 3))


def test_target_reached_but_shield_defers_delivery_until_scope_has_exited():
    async def operation(state):
        with anyio.fail_after(0.1, shield=True):
            await asyncio.sleep(0.001)
        return 42

    report = run(plain_case(operation))
    attempt = selected(report)
    record = attempt.cancellations[0]
    assert report.outcome is Outcome.UNRESOLVED
    assert record.target_reached and record.prefix_matched and record.scope_cancel_called
    assert not record.exception_observed
    assert record.delivered is None
    assert not record.scope_cancelled_caught
    assert record.delivery_unknown_reason
    assert attempt.completion.state == "returned" and attempt.completion.value == 42
    assert not attempt.valid_schedule
    assert attempt.oracle.outcome is Outcome.PASS


def test_user_suppression_does_not_claim_exact_scope_attribution():
    async def operation(state):
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            state["suppressed"] = True
        return 7

    report = run(plain_case(operation))
    attempt = selected(report)
    record = attempt.cancellations[0]
    assert report.outcome is Outcome.UNRESOLVED
    assert attempt.completion.state == "returned" and attempt.completion.value == 7
    assert record.scope_cancel_called and record.exception_observed
    assert record.delivered is None
    assert not record.scope_cancelled_caught
    assert record.delivery_unknown_reason
    assert not attempt.valid_schedule


def test_uncancelled_replay_preserves_return_and_task_identity():
    states = []

    async def factory():
        state = {}
        states.append(state)
        return state

    async def operation(state):
        state["task"] = asyncio.current_task()
        with anyio.CancelScope():
            await asyncio.sleep(0.001)
            assert asyncio.current_task() is state["task"]
        return 13

    case = plain_case(operation, factory=factory)
    report = run(case)
    baseline = report.attempts[0]
    assert baseline.completion.state == "returned" and baseline.completion.value == 13
    assert not baseline.cancellations
    assert not states[0]["task"].cancelled()
    control = asyncio.run(replay(case, baseline.schedule))
    control.assert_passed()
    assert control.attempts[0].trace == baseline.trace


def test_operation_exception_remains_an_operation_result():
    async def operation(state):
        await asyncio.sleep(0.001)
        raise LookupError("private operation exception")

    report = run(plain_case(operation))
    report.assert_passed()
    assert report.attempts[0].completion.state == "raised"
    assert isinstance(report.attempts[0].completion.exception, LookupError)
    assert selected(report).completion.state == "scope_cancelled"
    assert "private operation exception" not in report.to_json()


def test_scope_preserves_application_error_remaining_in_mixed_cancellation_group():
    group_type = getattr(builtins, "BaseExceptionGroup", None)
    if group_type is None:
        group_type = import_module("exceptiongroup").BaseExceptionGroup

    async def operation(state):
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError as exc:
            raise group_type(
                "mixed cancellation and cleanup error", [exc, ValueError("cleanup")]
            ) from None

    async def oracle(state, completion):
        if completion.state == "raised":
            assert isinstance(completion.exception, group_type)
            assert len(completion.exception.exceptions) == 1
            assert isinstance(completion.exception.exceptions[0], ValueError)
        else:
            assert completion.state == "returned"

    case = plain_case(operation, oracle=oracle)
    report = run(case)
    report.assert_passed()
    attempt = selected(report)
    assert attempt.completion.state == "raised"
    assert attempt.cancellations[0].delivered
    assert attempt.cancellations[0].scope_cancelled_caught
    replayed = asyncio.run(replay(case, attempt.schedule))
    replayed.assert_passed()
    assert replayed.attempts[0].completion.state == "raised"


def test_broken_scope_stack_is_an_infrastructure_error():
    phases = []

    async def operation(state):
        # Deliberately invalid user code: the harness must report failed scope
        # exit as ERROR, rather than accept it as an ordinary operation result.
        state["leaked_scope"] = anyio.CancelScope()
        state["leaked_scope"].__enter__()
        await asyncio.sleep(0.001)

    async def oracle(state, completion):
        await asyncio.sleep(0)
        phases.append("oracle")

    async def teardown(state):
        await asyncio.sleep(0)
        phases.append("teardown")

    report = run(plain_case(operation, oracle=oracle, teardown=teardown))
    assert report.outcome is Outcome.ERROR
    assert not report.attempts[0].valid_schedule
    assert any("instrumentation error" in reason for reason in report.attempts[0].reasons)
    assert phases == ["oracle", "teardown"]


def test_oracle_and_teardown_are_outside_the_operation_scope():
    records = []

    async def factory():
        return {"phases": []}

    async def operation(state):
        state["task"] = asyncio.current_task()
        await asyncio.sleep(0.001)

    async def oracle(state, completion):
        assert asyncio.current_task() is not state["task"]
        assert anyio.current_effective_deadline() != float("-inf")
        with anyio.fail_after(0.1):
            await asyncio.sleep(0.001)
        state["phases"].append("oracle")

    async def teardown(state):
        assert asyncio.current_task() is not state["task"]
        assert anyio.current_effective_deadline() != float("-inf")
        await asyncio.sleep(0.001)
        state["phases"].append("teardown")
        records.append(state["phases"])

    run(Case(factory, operation, oracle, teardown)).assert_passed()
    assert records == [["oracle", "teardown"]] * 3


def test_caller_cancellation_runs_bounded_cleanup_and_preserves_scope_stack():
    async def scenario():
        started = asyncio.Event()
        phases = []
        operation_tasks = []

        async def operation(state):
            operation_tasks.append(asyncio.current_task())
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                with anyio.fail_after(0.1, shield=True):
                    await asyncio.sleep(0.001)
                    phases.append("operation cleanup")

        async def teardown(state):
            await asyncio.sleep(0.001)
            phases.append("teardown")

        runner = asyncio.create_task(
            sweep(
                plain_case(operation, teardown=teardown),
                cancellation_mode="anyio",
                limits=Limits(phase_timeout=0.1, cleanup_timeout=0.1),
            )
        )
        await started.wait()
        before = time.monotonic()
        runner.cancel("caller")
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert time.monotonic() - before < 0.5
        assert "teardown" in phases and "operation cleanup" in phases
        assert all(t.done() for t in operation_tasks)
        with anyio.CancelScope():
            await anyio.sleep(0)

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["factory", "operation", "oracle", "teardown"])
def test_anyio_caller_scope_cancellation_preserves_finalization_budgets(phase):
    async def scenario():
        phases = []
        state = {}
        operation_tasks = []

        def cancel_caller(selected_phase):
            if phase == selected_phase:
                asyncio.get_running_loop().call_soon(caller_scope.cancel)

        async def factory():
            cancel_caller("factory")
            return state

        async def operation(state):
            operation_tasks.append(asyncio.current_task())
            cancel_caller("operation")
            try:
                await asyncio.sleep(0.005)
            finally:
                phases.append("operation exit")

        async def oracle(state, completion):
            phases.append("oracle start")
            cancel_caller("oracle")
            await asyncio.sleep(0.005)
            phases.append("oracle done")

        async def teardown(state):
            phases.append("teardown start")
            cancel_caller("teardown")
            await asyncio.sleep(0.005)
            phases.append("teardown done")

        before = time.monotonic()
        with anyio.CancelScope() as caller_scope:
            await sweep(
                Case(factory, operation, oracle, teardown),
                cancellation_mode="anyio",
                limits=Limits(phase_timeout=0.1, cleanup_timeout=0.1),
            )
            phases.append("sweep returned")
        assert caller_scope.cancelled_caught
        assert "sweep returned" not in phases
        assert time.monotonic() - before < 0.5
        assert "teardown done" in phases
        assert phases.index("teardown start") < phases.index("teardown done")
        if phase == "oracle":
            assert phases.index("oracle done") < phases.index("teardown start")
        assert all(t.done() for t in operation_tasks)
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        with anyio.CancelScope():
            await anyio.sleep(0)

    asyncio.run(scenario())


def test_repeated_deliveries_respect_point_phase_and_total_budgets():
    observations = []

    async def factory():
        return {"stop": False, "exceptions": 0}

    async def operation(state):
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            while not state["stop"]:
                try:
                    await asyncio.sleep(0.001)
                except asyncio.CancelledError:
                    state["exceptions"] += 1

    async def oracle(state, completion):
        observations.append((completion.state, state["exceptions"]))
        await asyncio.sleep(0)

    async def teardown(state):
        state["stop"] = True
        await asyncio.sleep(0.001)

    before = time.monotonic()
    report = run(
        Case(factory, operation, oracle, teardown),
        limits=Limits(max_points=4, max_attempts=3, phase_timeout=0.02, total_timeout=0.1),
    )
    attempt = selected(report)
    assert time.monotonic() - before < 0.5
    assert report.outcome is Outcome.UNRESOLVED
    assert attempt.completion.state == "pending"
    assert attempt.oracle.outcome is Outcome.PASS and attempt.teardown.outcome is Outcome.PASS
    assert len(attempt.trace) == 4 and not attempt.trace_complete
    assert len(attempt.cancellations) == 1
    assert attempt.cancellation_exceptions > 4
    assert observations[-1][0] == "pending" and observations[-1][1] > 4
    assert "suspension point budget exhausted" in attempt.reasons


def test_anyio_attempt_limit_includes_both_baselines():
    async def operation(state):
        await asyncio.sleep(0.001)

    report = run(plain_case(operation), limits=Limits(max_attempts=2))
    assert report.outcome is Outcome.UNRESOLVED
    assert len(report.attempts) == 2
    assert report.coverage.single_planned == 1 and report.coverage.single_valid == 0
    assert "attempt/time budget exhausted" in report.coverage.reasons


@pytest.mark.parametrize(
    "field,value", [("anyio_version", "4.0.0"), ("interpreter", "CPython-0.0")]
)
def test_replay_refuses_environment_mismatch_before_factory(field, value):
    async def operation(state):
        await asyncio.sleep(0.001)

    case = plain_case(operation)
    schedule = replace(selected(run(case)).schedule, **{field: value})

    async def factory():
        raise AssertionError("must not create case state")

    report = asyncio.run(replay(replace(case, factory=factory), schedule))
    assert report.outcome is Outcome.UNRESOLVED
    assert report.attempts[0].setup.outcome is None


@pytest.mark.parametrize("kwargs", [{"backend": "trio"}, {"cancellation_mode": "unknown"}])
def test_unsupported_mode_and_backend_are_rejected(kwargs):
    async def operation(state):
        await asyncio.sleep(0.001)

    with pytest.raises(ValueError):
        asyncio.run(sweep(plain_case(operation), **kwargs))


def test_explicit_replay_mode_cannot_reinterpret_anyio_schedule():
    async def operation(state):
        await asyncio.sleep(0.001)

    case = plain_case(operation)
    schedule = selected(run(case)).schedule
    with pytest.raises(ValueError, match="mode"):
        asyncio.run(replay(case, schedule, cancellation_mode="asyncio"))


def test_anyio_schedule_rejects_double_requests():
    async def operation(state):
        await asyncio.sleep(0.001)
        await asyncio.sleep(0.001)

    report = run(plain_case(operation))
    single = selected(report, 2).schedule
    with pytest.raises(ValueError):
        replace(single, cancel_at=(1, 2))


def test_legacy_schedule_retains_task_cancel_semantics():
    async def operation(state):
        try:
            await asyncio.sleep(0.001)
        finally:
            await asyncio.sleep(0.001)

    case = plain_case(operation)
    edge = asyncio.run(sweep(case))
    double = next(a for a in edge.attempts if len(a.schedule.cancel_at) == 2)
    legacy = double.schedule.to_dict()
    for field in ("schema_version", "cancellation_mode", "backend", "anyio_version"):
        legacy.pop(field, None)
    restored = Schedule.from_dict(legacy)
    assert restored.cancellation_mode == "asyncio"
    report = asyncio.run(replay(case, restored))
    report.assert_passed()
    assert len(report.attempts[0].cancellations) == 2
    assert report.attempts[0].completion.state == "cancelled"
