"""Task completion semantics and ownership transfer across cancellation boundaries."""

import asyncio
import tempfile

import pytest

from cancellens import Case, Limits, Outcome, require, sweep
from tests.test_sweep import simple_case


def test_cancelled_task_done_callback_can_return_pool_slot():
    async def factory():
        return {"pool": asyncio.Semaphore(1)}

    async def operation(state):
        pool = state["pool"]
        await pool.acquire()
        task = asyncio.current_task()
        state["task"] = task

        def release(cancelled):
            if cancelled.cancelled():
                pool.release()

        task.add_done_callback(release)
        await asyncio.sleep(0)
        pool.release()

    async def oracle(state, completion):
        require(not state["pool"].locked(), "done callback returned pool slot")
        require(
            state["task"].cancelled() == (completion.state == "cancelled"),
            "Task cancellation status preserved",
        )
        await state["pool"].acquire()
        state["pool"].release()

    async def teardown(state):
        if state["pool"].locked():
            state["pool"].release()

    asyncio.run(sweep(Case(factory, operation, oracle, teardown))).assert_passed()


def test_done_callback_observes_original_operation_exception():
    async def operation(state):
        task = asyncio.current_task()

        def observe(done):
            state["exception"] = None if done.cancelled() else done.exception()

        task.add_done_callback(observe)
        await asyncio.sleep(0)
        raise LookupError("operation failed")

    async def oracle(state, completion):
        if completion.state == "raised":
            require(state["exception"] is completion.exception, "original exception preserved")
        else:
            require(completion.state == "cancelled", "expected operation outcome")

    asyncio.run(sweep(simple_case(operation, oracle))).assert_passed()


def test_external_cancellation_during_factory_handoff_closes_returned_resource():
    async def scenario():
        resource = tempfile.TemporaryFile()
        runner = None
        calls = []

        async def factory():
            asyncio.get_running_loop().call_soon(runner.cancel)
            return resource

        async def operation(state):
            calls.append("operation")
            await asyncio.sleep(0)

        async def oracle(state, completion):
            calls.append("oracle")

        async def teardown(state):
            calls.append("teardown")
            state.close()

        try:
            runner = asyncio.create_task(sweep(Case(factory, operation, oracle, teardown)))
            with pytest.raises(asyncio.CancelledError):
                await runner
            assert resource.closed
            assert calls == ["teardown"]
        finally:
            resource.close()

    asyncio.run(scenario())


def test_factory_that_returns_state_while_draining_is_finalized():
    async def scenario():
        resource = tempfile.TemporaryFile()

        async def factory():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return resource

        async def operation(state):
            await asyncio.sleep(0)

        async def teardown(state):
            state.close()

        try:
            report = await sweep(
                simple_case(operation, factory=factory, teardown=teardown),
                limits=Limits(phase_timeout=0.02),
            )
            assert report.outcome is Outcome.UNRESOLVED
            assert report.attempts[0].teardown.outcome is Outcome.PASS
            assert resource.closed
        finally:
            resource.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("instrumentation_error", [False, True])
def test_teardown_timeout_preserves_original_error(instrumentation_error):
    async def operation(state):
        await asyncio.sleep(0)

    async def oracle(state, completion):
        raise KeyError("oracle failed")

    async def teardown(state):
        await asyncio.Event().wait()

    case = simple_case(operation, oracle, teardown=teardown)
    if instrumentation_error:
        case = Case(case.factory, lambda state: 42, case.oracle, case.teardown)
    report = asyncio.run(sweep(case, limits=Limits(phase_timeout=0.02)))
    assert report.outcome is Outcome.ERROR
    assert report.attempts[0].outcome is Outcome.ERROR
    assert report.attempts[0].teardown.outcome is Outcome.UNRESOLVED
    assert not report.coverage.complete
