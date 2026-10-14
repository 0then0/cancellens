from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest

from cancellens import Case, Limits, Outcome, Schedule, replay, require, sweep
from examples.pool import pool_case


def run(case: Case[Any], **kwargs: Any):
    return asyncio.run(sweep(case, **kwargs))


def simple_case(operation, oracle=None, factory=None, teardown=None):
    async def default_factory():
        return {}

    async def default_oracle(state, completion):
        require(completion.state in ("returned", "cancelled"), "normal or cancelled")

    async def default_teardown(state):
        pass

    return Case(
        factory or default_factory,
        operation,
        oracle or default_oracle,
        teardown or default_teardown,
    )


def test_correct_finally_and_lost_slot_mutation():
    fixed = run(pool_case())
    fixed.assert_passed()
    assert fixed.coverage.single_valid == 1
    assert fixed.coverage.complete
    broken = run(pool_case(broken=True))
    assert broken.outcome is Outcome.FAIL
    attempt = broken.attempts[2]
    assert attempt.oracle.detail == "pool slot returned; next operation can acquire"
    assert attempt.teardown.outcome is Outcome.PASS
    with pytest.raises(AssertionError, match=r"FAIL: cancel_at=\(1,\).*pool slot"):
        broken.assert_passed()


@pytest.mark.parametrize("protected", [False, True])
def test_double_cancel_interrupts_async_finally_and_protection_mutation(protected):
    async def operation(state):
        state["held"] = True
        try:
            await asyncio.sleep(0)
        finally:
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                if not protected:
                    raise
            state["held"] = False

    async def oracle(state, completion):
        require(not state["held"], "resource released even during repeated cancellation")

    report = run(simple_case(operation, oracle))
    assert report.outcome is (Outcome.PASS if protected else Outcome.FAIL)
    assert report.coverage.double_valid == 1
    assert report.coverage.complete
    double = next(a for a in report.attempts if len(a.schedule.cancel_at) == 2)
    assert double.schedule.cancel_at == (1, 2)
    assert all(c.accepted and c.delivered for c in double.cancellations)


def test_cleanup_path_points_absent_from_baseline_are_discovered():
    async def operation(state):
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            raise

    report = run(simple_case(operation))
    report.assert_passed()
    assert report.coverage.baseline_points == 1
    assert report.coverage.double_planned == 2
    assert [a.schedule.cancel_at for a in report.attempts] == [(), (), (1,), (1, 2), (1, 3)]


def test_ready_future_is_not_a_suspension_point():
    async def operation(state):
        future = asyncio.get_running_loop().create_future()
        future.set_result(41)
        state["result"] = await future
        return 42

    async def oracle(state, completion):
        require(state["result"] == 41 and completion.value == 42, "values preserved")

    report = run(simple_case(operation, oracle))
    assert report.outcome is Outcome.UNRESOLVED
    assert report.coverage.baseline_points == 0
    assert report.coverage.reasons == ["no suspension points; no cancellation coverage"]
    assert all(a.oracle.outcome is Outcome.PASS for a in report.attempts)


def test_pending_future_bare_yield_nested_values_and_exception():
    async def inner(state):
        future = asyncio.get_running_loop().create_future()

        def complete():
            if not future.done():
                future.set_result(12)

        asyncio.get_running_loop().call_soon(complete)
        result = await future
        await asyncio.sleep(0)
        try:
            raise LookupError("SECRET: never serialized")
        except LookupError:
            state["caught"] = True
        return result + 30

    async def operation(state):
        return await inner(state)

    async def oracle(state, completion):
        if completion.state == "returned":
            require(completion.value == 42 and state["caught"], "nested values and exceptions")

    report = run(simple_case(operation, oracle))
    report.assert_passed()
    baseline = report.attempts[0]
    assert [p.kind for p in baseline.trace] == ["_asyncio.Future", "bare_yield"]
    assert any(f.function.endswith("inner") for f in baseline.trace[0].stack)
    assert "SECRET" not in report.to_json()
    assert "0x" not in report.to_json()


def test_pending_future_exception_is_forwarded():
    async def operation(state):
        future = asyncio.get_running_loop().create_future()

        def finish():
            if not future.done():
                future.set_exception(LookupError("private detail"))

        asyncio.get_running_loop().call_soon(finish)
        return await future

    async def oracle(state, completion):
        require(
            completion.state == "cancelled" or isinstance(completion.exception, LookupError),
            "exception type preserved",
        )

    report = run(simple_case(operation, oracle))
    report.assert_passed()
    assert report.attempts[0].completion.state == "raised"
    assert "private detail" not in report.to_json()


def test_baseline_matches_plain_task_execution():
    async def operation(state):
        state["task"] = asyncio.current_task()
        state["log"] = ["start"]
        await asyncio.sleep(0)
        state["log"].append("end")
        return 19

    async def compare():
        plain_state = {}
        plain = await asyncio.create_task(operation(plain_state))

        async def oracle(state, completion):
            require(state["task"] is not None, "operation has a real task")
            if completion.state == "returned":
                require(completion.value == plain, "same return value")
                require(state["log"] == plain_state["log"], "same side effects")

        return await sweep(simple_case(operation, oracle))

    asyncio.run(compare()).assert_passed()


def test_suppressed_cancellation_is_decided_by_oracle():
    async def operation(state):
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            state["suppressed"] = True
        return 7

    async def oracle(state, completion):
        require(completion.state == "returned" and completion.value == 7, "result contract")

    report = run(simple_case(operation, oracle))
    report.assert_passed()
    assert report.attempts[2].cancellations[0].delivered
    assert report.attempts[2].completion.state == "returned"


def test_finally_does_not_guarantee_release():
    async def operation(state):
        state["held"] = True
        try:
            await asyncio.sleep(0)
        finally:
            state["finally"] = True
        state["held"] = False

    async def oracle(state, completion):
        require(state["finally"], "finally executed")
        require(not state["held"], "resource released")

    assert run(simple_case(operation, oracle)).outcome is Outcome.FAIL


def test_oracle_error_is_not_invariant_violation():
    async def operation(state):
        await asyncio.sleep(0)

    async def oracle(state, completion):
        raise KeyError("credentials should not appear")

    report = run(simple_case(operation, oracle))
    assert report.outcome is Outcome.ERROR
    assert report.attempts[0].oracle.detail == "KeyError"
    assert "credentials" not in report.to_json()


def test_plain_assertion_in_oracle_is_error():
    async def operation(state):
        await asyncio.sleep(0)

    async def oracle(state, completion):
        raise AssertionError("oracle programmer mistake")

    assert run(simple_case(operation, oracle)).outcome is Outcome.ERROR


def test_factory_error_and_teardown_error_are_distinct():
    async def operation(state):
        await asyncio.sleep(0)

    async def factory():
        raise OSError("private")

    report = run(simple_case(operation, factory=factory))
    assert report.outcome is Outcome.ERROR
    assert report.attempts[0].setup.detail == "OSError"
    assert report.attempts[0].oracle.outcome is None

    async def teardown(state):
        raise OSError("private")

    report = run(simple_case(operation, teardown=teardown))
    assert report.outcome is Outcome.ERROR
    assert report.attempts[0].teardown.detail == "OSError"


def test_cleanup_error_preserves_original_violation():
    async def operation(state):
        await asyncio.sleep(0)

    async def oracle(state, completion):
        require(False, "original leak")

    async def teardown(state):
        raise RuntimeError("teardown problem")

    report = run(simple_case(operation, oracle, teardown=teardown))
    assert report.outcome is Outcome.FAIL
    assert report.attempts[0].oracle.detail == "original leak"
    assert report.attempts[0].teardown.outcome is Outcome.ERROR
    assert not report.coverage.complete


def test_each_attempt_has_fresh_state():
    states = []

    async def factory():
        state = {"used": False}
        states.append(state)
        return state

    async def operation(state):
        assert not state["used"]
        state["used"] = True
        await asyncio.sleep(0)

    report = run(simple_case(operation, factory=factory))
    report.assert_passed()
    assert len(states) == 3 and len({id(s) for s in states}) == 3


def test_diverging_baseline_is_unresolved():
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return {"branch": calls == 1}

    async def operation(state):
        if state["branch"]:
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(0)

    report = run(simple_case(operation, factory=factory))
    assert report.outcome is Outcome.UNRESOLVED
    assert not report.coverage.complete
    assert report.attempts[1].reasons == ["baseline trace is not repeatable"]


def test_replay_withholds_cancellation_on_different_path_and_reports_missing_target():
    async def operation(state):
        if state.get("different"):
            await asyncio.sleep(0)
        elif not state.get("skip"):
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    original = simple_case(operation)
    report = run(original, limits=Limits(max_depth=1))
    schedule = report.attempts[-1].schedule
    restored = Schedule.from_dict(json.loads(json.dumps(schedule.to_dict())))
    assert restored == schedule
    asyncio.run(replay(original, restored)).assert_passed()

    async def other_factory():
        return {"different": True}

    divergent = asyncio.run(replay(simple_case(operation, factory=other_factory), schedule))
    assert divergent.outcome is Outcome.UNRESOLVED
    assert divergent.attempts[0].cancellations == []
    assert any("diverged" in r for r in divergent.attempts[0].reasons)

    async def skipped_factory():
        return {"skip": True}

    missing = asyncio.run(replay(simple_case(operation, factory=skipped_factory), schedule))
    assert missing.outcome is Outcome.UNRESOLVED
    assert any("not reached" in r for r in missing.attempts[0].reasons)


def test_single_sweep_diverges_after_repeated_baselines():
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return {"different": calls >= 3}

    async def operation(state):
        if state["different"]:
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(0)

    report = run(simple_case(operation, factory=factory))
    assert report.outcome is Outcome.UNRESOLVED
    assert report.coverage.single_valid == 0
    assert report.attempts[2].cancellations == []


def test_replay_refuses_interpreter_mismatch_without_allocating_case():
    case = pool_case()
    schedule = run(case).attempts[2].schedule
    report = asyncio.run(replay(case, replace(schedule, interpreter="CPython-0.0")))
    assert report.outcome is Outcome.UNRESOLVED
    assert report.attempts[0].setup.outcome is None


@pytest.mark.parametrize("limits", [Limits(max_attempts=2), Limits(max_points=1)])
def test_budget_never_reports_full_pass(limits):
    async def operation(state):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    report = run(simple_case(operation), limits=limits)
    assert report.outcome is Outcome.UNRESOLVED
    assert not report.coverage.complete
    assert len(report.attempts) <= limits.max_attempts


def test_fail_survives_later_unresolved_attempt():
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return {"skip": calls == 4}

    async def operation(state):
        state["held"] = True
        await asyncio.sleep(0)
        if not state["skip"]:
            await asyncio.sleep(0)
        state["held"] = False

    async def oracle(state, completion):
        require(not state["held"], "slot lost")

    report = run(simple_case(operation, oracle, factory=factory), limits=Limits(max_depth=1))
    assert report.outcome is Outcome.FAIL
    assert report.attempts[-1].outcome is Outcome.UNRESOLVED


def test_json_preserves_outcomes_coverage_and_omits_values():
    report = run(pool_case(broken=True))
    data = json.loads(report.to_json())
    assert data["schema_version"] == 1
    assert data["outcome"] == "FAIL"
    assert data["coverage"]["single_valid"] == 1
    assert data["coverage"]["complete"] is True
    assert data["attempts"][2]["oracle"]["outcome"] == "FAIL"
    assert data["attempts"][2]["cancellations"][0]["delivered"] is True
    assert "value" not in data["attempts"][0]["completion"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_depth": 3},
        {"max_attempts": 0},
        {"max_points": -1},
        {"phase_timeout": 0},
        {"total_timeout": float("inf")},
        {"cleanup_timeout": float("nan")},
    ],
)
def test_limits_validate(kwargs):
    with pytest.raises(ValueError):
        Limits(**kwargs)


def test_schedule_requires_attribution_and_limits_depth():
    with pytest.raises(ValueError, match="prefix"):
        Schedule((1,))
    with pytest.raises(ValueError, match="two"):
        Schedule((1, 2, 3))
