<p align="center"><img src="https://raw.githubusercontent.com/0then0/cancellens/main/docs/icon.svg" width="112" height="112" alt="CancelLens: a lens inspecting an interrupted async path"></p>

# CancelLens

[![Checks](https://img.shields.io/github/actions/workflow/status/0then0/cancellens/ci.yml?branch=main&label=checks)](https://github.com/0then0/cancellens/actions/workflows/ci.yml)
[![CPython](https://img.shields.io/badge/CPython-3.10%E2%80%933.14-blue)](#supported-environments)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/0then0/cancellens/blob/main/LICENSE)
[![Runtime dependencies](https://img.shields.io/badge/runtime_dependencies-0-green)](https://github.com/0then0/cancellens/blob/main/pyproject.toml)

CancelLens tests what happens when an asyncio operation is cancelled while suspended.
It observes actual suspension points, repeats the operation on fresh state, and sends
real `Task.cancel()` requests. Your oracle checks resource release, state consistency,
and whether another operation can use the same pool or resource.

It works with ordinary pytest tests, native Python coroutines and real I/O. Cancellation
sweeps are an existing technique; a passing sweep is evidence for the checked schedules
and your invariants, not a proof of general cancellation safety.

## Installation

Install a published release:

```sh
python -m pip install cancellens
```

For a source checkout:

```sh
uv sync --locked
uv build
uv pip install dist/cancellens-0.1.0-py3-none-any.whl
```

The runtime uses only the standard library. Tests can use pytest without a plugin or
pytest-asyncio.

## Quick start

```python
import asyncio

from cancellens import Case, Completion, require, sweep


def test_pool_survives_cancellation():
    async def factory():
        return asyncio.Semaphore(1)

    async def operation(pool):
        await pool.acquire()
        try:
            await asyncio.sleep(0)
        finally:
            pool.release()

    async def oracle(pool, completion: Completion):
        require(completion.state in ("returned", "cancelled"), "operation terminated")
        require(not pool.locked(), "pool slot returned")
        await pool.acquire()  # Check reuse of the SAME resource.
        pool.release()

    async def teardown(pool):
        if pool.locked():
            pool.release()

    asyncio.run(sweep(Case(factory, operation, oracle, teardown))).assert_passed()
```

Move `pool.release()` out of `finally` to the happy path and cancellation loses the
slot. The oracle reports FAIL before teardown repairs it. The
[pool example](https://github.com/0then0/cancellens/blob/main/examples/pool.py) includes
both versions.

## Case lifecycle

Every attempt creates fresh state through four async callbacks:

1. `factory()` creates and returns the state. It owns cleanup of incomplete setup.
2. `operation(state)` returns a native Python coroutine and runs in a real Task.
3. `oracle(state, completion)` checks state before harness cleanup. It can explicitly
   await application-owned cleanup and exercise the same resource again.
4. `teardown(state)` cleans up the attempt, including failed attempts. A successfully
   returned factory resource is retained for teardown even if caller cancellation
   interrupts the handoff.

Use `require(condition, "invariant name")` or raise `InvariantViolation` to report a
contract failure. Other oracle exceptions, including `AssertionError`, are errors in
evaluating the test. Use static, non-sensitive invariant text: it appears in reports.

`Completion.state` is `returned`, `cancelled`, `raised`, `pending`, or `not_started`.
`value` and `exception` are available in memory. Operation return values, exceptions,
and suppressed cancellation are judged by your oracle. Operation Task completion
preserves `cancelled()` and `exception()` for done callbacks.

## Schedules, outcomes and coverage

A sweep runs two uncancelled baselines and compares their complete traces. It then
cancels at each baseline point. With `max_depth=2`, the default, it also tests a second
cancellation at later points observed on each valid single-cancel path. Those points
can include cleanup that never ran in the successful baseline.

Requests are sent only after the required trace prefix matches. The second request
also requires confirmed delivery of the first. There are no random delays selecting
cleanup targets, and no extra awaits are inserted into the operation.

- **PASS:** the schedules in the report's scope were evaluated and satisfied the oracle.
- **FAIL:** an attributable schedule violated an explicit invariant.
- **UNRESOLVED:** a target or delivery could not be confirmed, replay diverged, a budget
  cut coverage short, or no suspension points existed.
- **ERROR:** setup, instrumentation, oracle code or infrastructure failed.

Each attempt retains its schedule, trace, cancellation acceptance/delivery, operation
completion, oracle and teardown outcomes. FAIL and ERROR are preserved independently
of cleanup failures. An oracle violation on an invalid schedule remains visible but
is not promoted to an established cancellation bug.

`report.coverage` exposes planned/valid single and double schedules, baseline points,
attempt count, completeness and reasons. Incomplete coverage cannot produce PASS.
`report.assert_passed()` gives a short pytest failure with schedule and coverage.

```python
from cancellens import Limits

limits = Limits(
    max_points=100,
    max_attempts=100,  # Includes both baselines.
    max_depth=2,  # Choose 1 for a single-cancel sweep.
    phase_timeout=2.0,
    total_timeout=30.0,
    cleanup_timeout=0.1,
)
```

## Reports and replay

```python
import json
from cancellens import Schedule, replay

# Inside an async test, with a Case already defined:
report = await sweep(case)
saved = json.loads(report.to_json())
assert saved["schema_version"] == 1
schedule_data = next(a["schedule"] for a in saved["attempts"] if a["schedule"]["cancel_at"])
replayed = await replay(case, Schedule.from_dict(schedule_data))
replayed.assert_passed()
```

A schedule stores one-based targets and the full observed prefix through its last
target. Replay checks visible frame locations, bytecode offsets, code fingerprints
and yielded types. Different Python minor versions are refused; changed source or
scheduling may make a schedule unresolved. A replay report covers the selected
schedule, not a whole sweep. An empty schedule is an uncancelled replay control.

Reports omit locals, object reprs, absolute paths, SQL, response bodies, operation values
and exception messages. Code metadata, exception types and explicit invariant text
remain visible. See the [design and report reference](https://github.com/0then0/cancellens/blob/main/docs/design.md).

## Supported environments

CPython 3.10–3.14, standard asyncio event loop, default Task factory. CI runs the suite
on Linux and Windows across all five versions. macOS is also used for local validation.
Python 3.15+, PyPy, uvloop, free-threaded builds, custom/eager factories and Trio/AnyIO
cancellation semantics are outside v0.1's supported scope.

Observation follows nested Python awaits. Child Tasks and shielded work are boundaries:
only the caller's wait is visible, and shielded cleanup may outlive the caller. The
oracle must check or explicitly await it. On CPython 3.10, a cancelled child Task can
lose the cancellation request marker; uncertain delivery is UNRESOLVED. Child suppression
or replacement of the marker can also prevent attribution on later versions.

C awaitables and `coroutine_wrapper` can hide deeper source frames. No exact source
location is promised for every extension awaitable. Wrapper frames and `Task.get_coro()`
identity are not preserved. Lifecycle phases run in separate Tasks, so pass setup
configuration explicitly in case state rather than relying on changed ContextVars.
Prefix repeatability does not imply program-wide determinism or exhaustive interleavings.

## Deadlines and cleanup

In-process deadlines are cooperative. They cannot stop a blocked event loop, arbitrary
native code, a cancellation-resistant coroutine or a running thread. After a deadline,
the oracle observes state before teardown and emergency cancellation. The exploration
budget can be extended by the current oracle/teardown windows and final drain.

Caller cancellation propagates after attempted bounded teardown; another external
cancellation can interrupt finalization. Cases own application Tasks, threads and
external resources. CancelLens does not automatically roll back arbitrary side effects.

Use an external watchdog for potentially hanging tests:

```sh
uv run python validation/watchdog.py --timeout 90 -- uv run pytest -q
```

The validation watchdog terminates/kills the child process group on POSIX and the
immediate child on Windows. It cannot undo external side effects.

## External validation: aiosqlite

The [aiosqlite case](https://github.com/0then0/cancellens/blob/main/validation/aiosqlite/README.md)
uses real SQLite connections, temporary files and upstream worker threads. Event-controlled
connector hooks select cancelled opening without replacing the upstream lifecycle.
Independent native queries and worker joins check the resource before final cleanup.

- Base `9b127cecc7de843bf4f343b6da63b641866533fb`: FAIL because the native connection remains open.
- Proposed fix `005b3aa77a67606d2264996aeff4f69448fb79fc`: PASS for the same logical opening schedule.

[Recorded evidence](https://github.com/0then0/cancellens/blob/main/validation/aiosqlite/evidence.json)
contains repeated fresh sweeps, opening replays and uncancelled controls. This verifies
the selected proposed patch's opening contract, not all aiosqlite cancellation behavior.
The optional HTTPX/httpcore double-cancel integration is not verified in v0.1.

## Contributing and releases

See [Contributing](https://github.com/0then0/cancellens/blob/main/CONTRIBUTING.md) for
locked development setup, regression checks and integration reproduction.
[Publishing](https://github.com/0then0/cancellens/blob/main/docs/publishing.md) documents
PyPI Trusted Publishing and release setup. Changes are listed in the
[changelog](https://github.com/0then0/cancellens/blob/main/CHANGELOG.md).

## Related work

- [SQLAlchemy cancellation testing](https://github.com/sqlalchemy/sqlalchemy/blob/main/lib/sqlalchemy/testing/cancellation.py)
  provides greenlet-aware sweeps and connection/pool accounting for its async dialects.
- [Pyleak](https://github.com/deepankarm/pyleak) detects task/thread leaks and loop blocking.
- [Aiotools cancellation utilities](https://aiotools.readthedocs.io/en/latest/aiotools.cancel.html)
  provide application cancellation/waiting helpers with transparent caller cancellation.
- [Simloom](https://github.com/mandipadk/simloom) explores asyncio programs in a simulated
  scheduler/network environment.

CancelLens uses independent coroutine-protocol instrumentation, the standard loop,
real I/O and user-defined resource oracles. No code from these projects is incorporated.
