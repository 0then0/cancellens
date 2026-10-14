from __future__ import annotations

import asyncio
import sys
import time

import pytest

from cancellens import Case, Limits, Outcome, sweep
from tests.test_sweep import simple_case
from validation.watchdog import run as watchdog


def test_child_task_is_a_boundary_and_real_cancel_propagates_to_waited_task():
    async def child(state):
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            state["child_cancelled"] = True
            raise

    async def operation(state):
        state["child"] = asyncio.create_task(child(state))
        await state["child"]

    async def oracle(state, completion):
        assert state["child"].done()
        if completion.state == "cancelled":
            assert state["child_cancelled"]

    report = asyncio.run(sweep(simple_case(operation, oracle)))
    if sys.version_info[:2] == (3, 10):
        # CPython 3.10 drops Task.cancel(msg) at a cancelled child Task boundary.
        assert report.outcome is Outcome.UNRESOLVED
        assert report.attempts[2].oracle.outcome is Outcome.PASS
        assert report.attempts[2].cancellations[0].accepted
        assert not report.attempts[2].cancellations[0].delivered
    else:
        report.assert_passed()
    assert report.coverage.baseline_points == 1
    assert report.attempts[0].trace[0].kind == "_asyncio.Task"
    assert all(not f.function.endswith("child") for f in report.attempts[0].trace[0].stack)


@pytest.mark.parametrize("wait_for_cleanup", [False, True])
def test_shield_caller_completion_does_not_mean_cleanup_finished(wait_for_cleanup):
    from cancellens import require

    async def factory():
        return {"gate": asyncio.Event(), "released": False}

    async def cleanup(state):
        await state["gate"].wait()
        # Separate tasks have their own suspension points, outside the observed
        # caller. Multiple event-loop turns make the unfinished work observable.
        for _ in range(10):
            await asyncio.sleep(0)
        state["released"] = True

    async def operation(state):
        state["task"] = asyncio.create_task(cleanup(state))
        # Baseline's child completes without introducing an operation checkpoint.
        asyncio.get_running_loop().call_soon(state["gate"].set)
        await asyncio.shield(state["task"])

    async def oracle(state, completion):
        if wait_for_cleanup:
            await state["task"]
        require(state["released"], "shielded resource actually released")

    async def teardown(state):
        state["gate"].set()
        await state["task"]

    report = asyncio.run(sweep(Case(factory, operation, oracle, teardown)))
    assert report.outcome is (Outcome.PASS if wait_for_cleanup else Outcome.FAIL)
    assert report.coverage.baseline_points == 1


def test_external_caller_cancellation_propagates_and_teardown_runs():
    async def scenario():
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def operation(state):
            started.set()
            await asyncio.Event().wait()

        async def teardown(state):
            cleaned.set()

        task = asyncio.create_task(sweep(simple_case(operation, teardown=teardown)))
        await started.wait()
        task.cancel("outside")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()

    asyncio.run(scenario())


def test_deadline_observes_state_before_harness_cancellation():
    async def scenario():
        observed = []
        state_ref = {}

        async def operation(state):
            state_ref.update(state=state)
            state["held"] = True
            try:
                await asyncio.Event().wait()
            finally:
                state["held"] = False

        async def oracle(state, completion):
            observed.append((state["held"], completion.state))

        report = await sweep(
            simple_case(operation, oracle),
            limits=Limits(phase_timeout=0.02, total_timeout=0.1),
        )
        assert observed == [(True, "pending")]
        assert not state_ref["state"]["held"]
        assert report.outcome is Outcome.UNRESOLVED
        assert not report.coverage.complete

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["factory", "oracle", "teardown"])
def test_lifecycle_deadlines(phase):
    async def blocked(*args):
        await asyncio.Event().wait()

    async def operation(state):
        await asyncio.sleep(0)

    kwargs = {phase: blocked}
    report = asyncio.run(sweep(simple_case(operation, **kwargs), limits=Limits(phase_timeout=0.02)))
    assert report.outcome is Outcome.UNRESOLVED
    assert not report.coverage.complete
    assert (
        getattr(report.attempts[0], phase if phase != "factory" else "setup").outcome
        is Outcome.UNRESOLVED
    )


@pytest.mark.parametrize("phase", ["factory", "oracle", "teardown"])
def test_lifecycle_self_cancellation_is_error_not_caller_cancellation(phase):
    async def cancelled(*args):
        raise asyncio.CancelledError("phase cancellation")

    async def operation(state):
        await asyncio.sleep(0)

    report = asyncio.run(sweep(simple_case(operation, **{phase: cancelled})))
    assert report.outcome is Outcome.ERROR
    result = getattr(report.attempts[0], "setup" if phase == "factory" else phase)
    assert result.detail == "CancelledError (lifecycle task)"


def test_total_deadline_stops_starting_new_attempts():
    async def operation(state):
        await asyncio.sleep(0.015)

    report = asyncio.run(sweep(simple_case(operation), limits=Limits(total_timeout=0.01)))
    assert report.outcome is Outcome.UNRESOLVED
    assert len(report.attempts) == 1


def test_timed_out_operation_cannot_extend_trace_during_oracle():
    async def factory():
        return {"gate": asyncio.Event()}

    async def operation(state):
        await state["gate"].wait()
        await asyncio.sleep(0)

    async def oracle(state, completion):
        state["gate"].set()
        for _ in range(3):
            await asyncio.sleep(0)

    report = asyncio.run(
        sweep(simple_case(operation, oracle, factory=factory), limits=Limits(phase_timeout=0.02))
    )
    assert report.outcome is Outcome.UNRESOLVED
    assert report.attempts[0].completion.state == "pending"
    assert len(report.attempts[0].trace) == 1
    assert not report.attempts[0].trace_complete


@pytest.mark.parametrize("mode", ["blocked_loop", "suppressed_cancel", "thread"])
def test_external_watchdog_kills_adversarial_programs(tmp_path, mode):
    common = """
import asyncio
import threading
from pathlib import Path
from cancellens import Case, Limits, sweep
async def factory(): return None
async def oracle(state, completion): pass
async def teardown(state): pass
"""
    operations = {
        "blocked_loop": "async def operation(state):\n    while True: pass\n",
        "suppressed_cancel": """async def operation(state):
    while True:
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
""",
        "thread": """async def operation(state):
    threading.Thread(target=threading.Event().wait).start()
    await asyncio.sleep(0)
""",
    }
    marker = tmp_path / "started"
    operation = operations[mode].replace(
        "async def operation(state):\n",
        f"async def operation(state):\n    Path({str(marker)!r}).write_text('started')\n",
    )
    script = tmp_path / "adversarial.py"
    script.write_text(
        common
        + operation
        + "asyncio.run(sweep(Case(factory, operation, oracle, teardown), "
        + "limits=Limits(phase_timeout=0.01, total_timeout=0.05)))\n"
    )
    before = time.monotonic()
    assert watchdog([sys.executable, str(script)], timeout=1) == 124
    assert marker.exists(), "adversarial operation actually started before watchdog expired"
    assert time.monotonic() - before < 4


def test_real_socket_io_and_resource_close_mutation():
    from cancellens import require

    async def scenario(broken):
        async def factory():
            state = {"server_writers": []}

            async def handler(reader, writer):
                state["server_writers"].append(writer)
                try:
                    await reader.read()
                finally:
                    writer.close()
                    await writer.wait_closed()

            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            state["server"] = server
            state["address"] = server.sockets[0].getsockname()
            state["reader"], state["writer"] = await asyncio.open_connection(*state["address"])
            return state

        async def operation(state):
            try:
                state["writer"].write(b"hello")
                await state["writer"].drain()
                state["writer"].write_eof()
                await state["reader"].read()  # Real peer EOF, not an injected checkpoint.
            finally:
                if not broken:
                    state["writer"].close()
                    await state["writer"].wait_closed()
            if broken:
                state["writer"].close()
                await state["writer"].wait_closed()

        async def oracle(state, completion):
            require(state["writer"].is_closing(), "transport closed")

        async def teardown(state):
            state["writer"].close()
            await state["writer"].wait_closed()
            for writer in state["server_writers"]:
                writer.close()
                await writer.wait_closed()
            state["server"].close()
            await state["server"].wait_closed()

        return await sweep(Case(factory, operation, oracle, teardown), limits=Limits(max_depth=1))

    asyncio.run(scenario(False)).assert_passed()
    broken = asyncio.run(scenario(True))
    assert broken.outcome is Outcome.FAIL
    assert broken.coverage.baseline_points >= 1


def test_custom_task_factory_rejected():
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_task_factory(lambda loop, coro, **kw: asyncio.Task(coro, loop=loop, **kw))
        try:
            with pytest.raises(RuntimeError, match="task factories"):
                await sweep(simple_case(lambda state: asyncio.sleep(0)))
        finally:
            loop.set_task_factory(None)

    asyncio.run(scenario())
