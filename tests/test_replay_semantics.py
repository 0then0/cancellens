from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import replace

import pytest

from cancellens import Limits, Outcome, replay, require, sweep
from cancellens._observe import _code_key
from examples.pool import pool_case
from tests.test_sweep import simple_case


def test_child_suppression_can_prevent_delivery_to_selected_caller():
    async def child():
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            return 3
        return 3

    async def operation(state):
        return await asyncio.create_task(child())

    report = asyncio.run(sweep(simple_case(operation)))
    assert report.outcome is Outcome.UNRESOLVED
    attempt = report.attempts[2]
    assert attempt.completion.state == "returned"
    assert attempt.cancellations[0].accepted
    assert not attempt.cancellations[0].delivered
    assert not attempt.valid_schedule


@pytest.mark.parametrize("shielded", [False, True])
def test_removing_shield_mutation_loses_cleanup_resource(shielded):
    async def cleanup(state):
        await asyncio.sleep(0)
        state["held"] = False

    async def operation(state):
        state["held"] = True
        try:
            await asyncio.sleep(0)
        finally:
            task = asyncio.create_task(cleanup(state))
            state["cleanup"] = task
            try:
                await (asyncio.shield(task) if shielded else task)
            except asyncio.CancelledError:
                await task
                raise

    async def oracle(state, completion):
        require(not state["held"], "cleanup resource released")

    async def teardown(state):
        task = state.get("cleanup")
        if task:
            await asyncio.gather(task, return_exceptions=True)

    report = asyncio.run(sweep(simple_case(operation, oracle, teardown=teardown)))
    if sys.version_info[:2] == (3, 10) and not shielded:
        assert report.outcome is Outcome.UNRESOLVED
        assert any(a.oracle.outcome is Outcome.FAIL for a in report.attempts)
    else:
        assert report.outcome is (Outcome.PASS if shielded else Outcome.FAIL)
        assert report.coverage.double_valid >= 1


def test_invalid_operation_type_is_instrumentation_error():
    def operation(state):
        return 42

    report = asyncio.run(sweep(simple_case(operation)))
    assert report.outcome is Outcome.ERROR
    assert "instrumentation error: InstrumentationError" in report.attempts[0].reasons


def test_observation_failure_is_not_an_operation_result(monkeypatch):
    from cancellens import _observe

    def broken_description(*args):
        raise RuntimeError("instrumentation failed")

    monkeypatch.setattr(_observe, "describe", broken_description)

    async def operation(state):
        await asyncio.sleep(0)

    async def accepting_oracle(state, completion):
        pass

    report = asyncio.run(sweep(simple_case(operation, accepting_oracle)))
    assert report.outcome is Outcome.ERROR
    assert not report.attempts[0].valid_schedule


def test_portable_code_key_ignores_installation_path():
    def outer():
        def inner():
            return 1

        return inner

    code = outer.__code__
    nested = tuple(
        c.replace(co_filename="/other/install/path.py") if isinstance(c, types.CodeType) else c
        for c in code.co_consts
    )
    relocated = code.replace(co_filename="/other/install/path.py", co_consts=nested)
    assert _code_key(code) == _code_key(relocated)
    assert _code_key(code) != _code_key(code.replace(co_consts=code.co_consts + ("changed",)))


def test_replay_rejects_depth_outside_requested_scope():
    async def operation(state):
        try:
            await asyncio.sleep(0)
        finally:
            await asyncio.sleep(0)

    case = simple_case(operation)
    report = asyncio.run(sweep(case))
    double = next(a.schedule for a in report.attempts if len(a.schedule.cancel_at) == 2)
    with pytest.raises(ValueError, match="max_depth"):
        asyncio.run(replay(case, double, limits=Limits(max_depth=1)))


def test_replay_changed_code_fingerprint_withholds_request():
    case = pool_case()
    schedule = asyncio.run(sweep(case)).attempts[2].schedule
    frame = schedule.prefix[0].stack[0]
    point = replace(schedule.prefix[0], stack=(replace(frame, code="changed"),))
    report = asyncio.run(replay(case, replace(schedule, prefix=(point,))))
    assert report.outcome is Outcome.UNRESOLVED
    assert not report.attempts[0].cancellations


def test_replay_failure_does_not_hide_teardown_error_or_claim_complete_coverage():
    case = pool_case(broken=True)
    schedule = asyncio.run(sweep(case)).attempts[2].schedule

    async def broken_teardown(state):
        raise OSError("cleanup error")

    report = asyncio.run(replay(replace(case, teardown=broken_teardown), schedule))
    assert report.outcome is Outcome.FAIL
    assert not report.coverage.complete
    assert report.attempts[0].teardown.outcome is Outcome.ERROR


def test_oracle_may_run_next_operation_on_same_resource_to_find_corruption():
    async def operation(state):
        state["balance"] = 1
        await asyncio.sleep(0)
        state["balance"] = 0

    async def next_operation(state):
        require(state["balance"] == 0, "next transaction sees balanced state")
        state["balance"] += 1
        await asyncio.sleep(0)
        state["balance"] -= 1

    async def oracle(state, completion):
        await next_operation(state)

    report = asyncio.run(sweep(simple_case(operation, oracle)))
    assert report.outcome is Outcome.FAIL
    assert report.attempts[2].oracle.detail == "next transaction sees balanced state"
