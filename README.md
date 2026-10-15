<p align="center"><img src="https://raw.githubusercontent.com/0then0/cancellens/main/docs/icon.svg" width="112" height="112" alt="CancelLens: a lens inspecting an interrupted async path"></p>

# CancelLens

[![Checks](https://img.shields.io/github/actions/workflow/status/0then0/cancellens/ci.yml?branch=main&label=checks)](https://github.com/0then0/cancellens/actions/workflows/ci.yml)
[![CPython](https://img.shields.io/badge/CPython-3.10%E2%80%933.14-blue)](#supported-environments)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/0then0/cancellens/blob/main/LICENSE)
[![Runtime dependencies](https://img.shields.io/badge/runtime_dependencies-0-green)](https://github.com/0then0/cancellens/blob/main/pyproject.toml)

CancelLens tests what happens when an asyncio operation is cancelled while suspended.
It observes real suspension points, repeats the operation on fresh state, and sends
real `Task.cancel()` requests. Your oracle checks resource release, state consistency,
and whether the same pool or resource can serve another operation.

Use it in ordinary pytest tests with native Python coroutines and real I/O. A passing
sweep confirms the evaluated schedules and your invariants; it does not prove general
cancellation safety or cover every possible task interleaving.

## Installation

```sh
python -m pip install cancellens
```

The runtime uses only the standard library. No pytest plugin or pytest-asyncio is
required. For a source checkout, see [Contributing](https://github.com/0then0/cancellens/blob/main/CONTRIBUTING.md).

## Quick start

Save this test as `test_pool.py` in a pytest project:

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

Run it with `python -m pytest -q test_pool.py`.

Moving `pool.release()` from `finally` to the happy path makes cancellation lose the
slot. The oracle reports FAIL before teardown repairs it. The [pool example](https://github.com/0then0/cancellens/blob/main/examples/pool.py)
contains both implementations.

## Define a case

Each attempt creates fresh state through four async callbacks:

1. `factory()` creates and returns state. It must clean up incomplete setup if it fails.
2. `operation(state)` returns a native Python coroutine and runs in a real Task.
3. `oracle(state, completion)` checks state before harness cleanup. It can await
   application-owned cleanup and exercise the same resource again.
4. `teardown(state)` releases the attempt's resources, including after a failed oracle.

Use `require(condition, "invariant name")` or raise `InvariantViolation` to report a
contract failure. Other oracle exceptions, including `AssertionError`, mean the check
could not be evaluated correctly. Use static, non-sensitive invariant text because
it is included in reports.

`Completion.state` is `returned`, `cancelled`, `raised`, `pending`, or `not_started`.
The operation's value and exception are available to the oracle in memory. Your oracle
judges their meaning, including whether suppressed cancellation is acceptable.

## Explore cancellation schedules

`sweep(case)` runs two uncancelled baselines and compares their traces. It then cancels
at each baseline suspension point. By default it also tests a second cancellation at
later points discovered on each valid single-cancel path, including cleanup paths
that never ran in the baseline.

Requests are sent only after the required trace prefix matches. A second request
requires confirmed delivery of the first. CancelLens adds no awaits to the operation
and does not select cancellation targets with random delays.

- **PASS:** the report's schedules were evaluated and satisfied the oracle.
- **FAIL:** an attributable schedule violated an explicit invariant.
- **UNRESOLVED:** a target or delivery could not be confirmed, replay diverged, a budget
  cut coverage short, or the operation had no suspension points.
- **ERROR:** setup, instrumentation, oracle code, or infrastructure failed.

Each attempt records its schedule, trace, cancellation acceptance and delivery,
operation completion, oracle, and teardown. A confirmed FAIL or ERROR remains visible
if cleanup also fails. An oracle violation on an invalid schedule is recorded without
being classified as an established cancellation bug.

`report.coverage` describes planned and valid schedules, baseline points, attempt
count, completeness, and reasons for incomplete coverage. Incomplete coverage cannot
produce PASS. `report.assert_passed()` raises an assertion with the schedule and coverage.

```python
from cancellens import Limits

limits = Limits(
    max_points=100,
    max_attempts=100,  # Includes both baselines.
    max_depth=2,  # Use 1 for single cancellation only.
    phase_timeout=2.0,
    total_timeout=30.0,
    cleanup_timeout=0.1,
)
```

Pass limits with `await sweep(case, limits=limits)` or `await replay(case, schedule, limits=limits)`.

## Save reports and replay a schedule

Inside an async test, with a case already defined:

```python
import json

from cancellens import Schedule, replay, sweep

report = await sweep(case)
saved = json.loads(report.to_json())
schedule_data = next(a["schedule"] for a in saved["attempts"] if a["schedule"]["cancel_at"])
replayed = await replay(case, Schedule.from_dict(schedule_data))
replayed.assert_passed()
```

A schedule contains one-based targets and the observed prefix through its last target.
Replay checks visible frame locations, bytecode offsets, code fingerprints, and yielded
types. Different Python minor versions are refused; changed code or scheduling may
make replay unresolved. Replay covers only the selected schedule. An empty schedule
is an uncancelled control.

Reports omit locals, object reprs, absolute paths, SQL, response bodies, operation
values, and exception messages. They retain code metadata, exception types, and explicit
invariant text. See the [instrumentation and report reference](https://github.com/0then0/cancellens/blob/main/docs/design.md).

## Supported environments

CancelLens v0.1 supports CPython 3.10–3.14, the standard asyncio event loop, and the
default Task factory. CI tests Linux and Windows across those versions. macOS is also
used for local validation. Python 3.15+, PyPy, uvloop, free-threaded builds, custom or
eager factories, and Trio/AnyIO cancellation semantics are outside this scope.

Observation follows nested Python awaits. Child Tasks and shielded work are boundaries:
only the caller's wait is visible, and cleanup may outlive the caller. The oracle must
check or explicitly await that work. On CPython 3.10, a cancelled child Task can lose
the request marker; uncertain delivery is UNRESOLVED. Child suppression or replacement
of the marker can also prevent attribution on later versions.

C awaitables can hide deeper source frames. Exact source attribution is not guaranteed
for every extension awaitable. Wrapper frames and `Task.get_coro()` identity are not
preserved. Lifecycle phases run in separate Tasks, so pass setup configuration through
case state rather than relying on factory changes to ContextVars.

## Deadlines and resource ownership

In-process deadlines require a functioning event loop. They cannot stop blocked native
code, a cancellation-resistant coroutine, or a running thread. After an operation
deadline, the oracle observes state before teardown and emergency cancellation.
Reserved oracle, teardown, and drain windows can extend the exploration deadline.

Cases own their application Tasks, threads, and external resources. Caller cancellation
propagates after attempted bounded teardown; another cancellation can interrupt that
finalization. CancelLens cannot roll back arbitrary external side effects.

For potentially hanging tests in a checkout, use the validation watchdog:

```sh
uv run --no-sync python validation/watchdog.py --timeout 90 -- uv run --no-sync pytest -q
```

On POSIX it terminates the owned process group and kills remaining members. Nested
validation workers inherit that group, so a whole-run deadline includes them. On
Windows it can terminate only the immediate child. See [Validation](https://github.com/0then0/cancellens/blob/main/validation/README.md)
for commands, evidence, and supervision details.

## Validated integrations

These cases use real resources and inspect them before final cleanup:

- [aiosqlite cancelled opening](https://github.com/0then0/cancellens/blob/main/validation/aiosqlite/README.md): a native SQLite
  connection and worker thread expose a lost connection during opening. The pinned
  base fails; the selected proposed patch passes the opening contract.
- [HTTPX/httpcore double cancellation](https://github.com/0then0/cancellens/blob/main/validation/httpx/README.md): loopback TCP and
  a one-slot HTTP/1.1 pool expose interrupted response cleanup. The pinned base loses
  the slot; the selected proposed patch allows cleanup to retry and the next request
  to succeed. An additional single-cancel close failure is retained on both snapshots.

Each case publishes immutable commits, schedules, controls, and resource observations.
These are bounded integration results, not general safety claims about either library.

## Documentation

- [Contributing](https://github.com/0then0/cancellens/blob/main/CONTRIBUTING.md): development setup, checks, CI, and releases.
- [Instrumentation and report reference](https://github.com/0then0/cancellens/blob/main/docs/design.md): observation, attribution,
  lifecycle ownership, and JSON schema.
- [Validation](https://github.com/0then0/cancellens/blob/main/validation/README.md): reproducible external cases and evidence.
- [Changelog](https://github.com/0then0/cancellens/blob/main/CHANGELOG.md): released changes and unreleased project updates.

## Related work

- [SQLAlchemy cancellation testing](https://github.com/sqlalchemy/sqlalchemy/blob/main/lib/sqlalchemy/testing/cancellation.py)
  uses greenlet-aware sweeps and connection/pool accounting for async dialects.
- [Pyleak](https://github.com/deepankarm/pyleak) detects task, thread, and loop-blocking leaks.
- [Aiotools cancellation utilities](https://aiotools.readthedocs.io/en/latest/aiotools.cancel.html)
  provide application cancellation and waiting helpers.
- [Simloom](https://github.com/deepankarm/simloom) explores asyncio programs with a simulated scheduler.

CancelLens uses its own coroutine-protocol instrumentation, the standard loop, real
I/O, and user-defined resource oracles. No code from these projects is incorporated.
