# Cancelled aiosqlite opening

This integration case exercises [aiosqlite issue #259](https://github.com/omnilib/aiosqlite/issues/259)
through the installed CancelLens wheel. It compares an immutable base with the proposed
[upstream fix #389](https://github.com/omnilib/aiosqlite/pull/389):

- Base: [`9b127cecc7de843bf4f343b6da63b641866533fb`](https://github.com/omnilib/aiosqlite/commit/9b127cecc7de843bf4f343b6da63b641866533fb).
- Proposed fix: [`005b3aa77a67606d2264996aeff4f69448fb79fc`](https://github.com/omnilib/aiosqlite/commit/005b3aa77a67606d2264996aeff4f69448fb79fc).

The base assigns native SQLite ownership only after awaiting the connector result.
Cancellation can discard that result while leaving the worker-created connection open.
The proposed fix assigns ownership on the worker before handing off the result, so
its stop operation can close the native connection. This document reports the selected
proposed patch, not a released correction or general aiosqlite cancellation safety.

## Reproduce

Use CPython 3.10–3.14 and uv. The checked-in evidence uses CPython 3.12.15 on macOS arm64;
SQLite version and platform are included in the JSON report. Network access to GitHub
and the package index is needed for disposable upstream build environments.

```sh
uv sync --locked
uv build
uv run --no-sync python validation/watchdog.py --timeout 300 -- uv run --no-sync python validation/aiosqlite/run.py --wheel dist/cancellens-0.1.0-py3-none-any.whl --repeats 5 --output acceptance-aiosqlite.json
```

`--python /path/to/python` selects another acceptance interpreter. The acceptance
interpreter is independent of the regression matrix. Downloaded snapshots, environments
and databases are temporary; existing environments and upstream checkouts are not
modified. Flit 3.12.0 is pinned because both snapshots require `flit_core<4`.

`run.py` verifies full commit identities through GitHub's API, downloads each snapshot,
checks archive paths, builds real aiosqlite and installs it beside the wheel. Workers
run outside the source checkout with PYTHONPATH removed and an external watchdog.
Installed core.py hashes must match the selected snapshot. No third-party code is
vendored or incorporated in CancelLens.

The checked-in report uses a locally built CancelLens wheel with SHA-256
`0935bf0e14d5aca00d76a8d463deb995208dab50544e50d5d6fdb03ef0da5bf3`.
It is not a direct run of the published PyPI artifact. The HTTPX case separately
validates the exact published CancelLens 0.1.0 wheel.

Workers inherit the enclosing POSIX watchdog's process group while retaining their
individual deadlines, so the whole-run deadline can stop them if their manager exits.
See [process supervision](../README.md#process-supervision). The aggregate JSON is
written after both comparisons complete; an interrupted run may have only its log
and nonzero exit status.

## Ordering and oracle

Factory supplies a controlled connector to the upstream `aiosqlite.Connection`:

1. The worker creates a real SQLite connection in a temporary file and retains an
   independent strong reference.
2. It notifies an asyncio Future through `call_soon_threadsafe`, then waits on a
   threading Event before returning the connection.
3. The operation is an ordinary `async with proxy:`. At its first pending Future,
   CancelLens queues a real cancellation request.
4. The controller's notification resumes it after that request has been queued.
   It opens the connector gate, allowing upstream worker/result handling to continue.

No timing-dependent sleep selects the target. The connector's bounded wait is a
failure timeout rather than schedule control. Upstream opening, cancellation, close,
stop and worker execution are not replaced.

The oracle joins the worker with a bound and independently attempts `SELECT 1` on the
captured native connection. `check_same_thread=False` permits inspection after worker
termination. A closed-connection ProgrammingError proves closure; a successful query
means the connection remains open. Worker termination and native closure are separate
invariants. Teardown closes captured connections only after recording the oracle result.

The baseline discovers open, close and stop Future points in these snapshots. Source
attribution for opening ends at `Connection.__aenter__`: a C coroutine_wrapper hides
_connect. Each snapshot therefore discovers its own prefix. The same logical schedule
is `cancel_at=(1,)`, but source prefixes are not transplanted blindly between changed
commits. Saved prefixes are serialized and replayed on fresh state within each snapshot.

## Expected results

[`evidence.json`](evidence.json) records five series per snapshot, with 60 fresh attempts
in total: 50 sweep attempts and 10 additional opening replays.

- Two uncancelled baselines per series: PASS in both snapshots.
- Base opening cancellation `(1,)`: FAIL on `native SQLite connection closed before
  harness teardown`. Cancellation is accepted/delivered; worker terminates; native
  query still succeeds.
- Proposed-fix opening cancellation `(1,)`: PASS, with connection closed and worker
  terminated.
- Close `(2,)` and stop `(3,)` cancellation: PASS in both snapshots.
- Opening replay: reproduces the corresponding result in each series.

Each sweep has complete single-cancel coverage (`max_depth=1`). Double-cancel contracts
are covered by the library's resource and shield tests rather than this external case.
An uncancelled run is the negative control, and repairing the base leak in teardown
does not change its saved FAIL.

The evidence records immutable commits, archive/core/wheel hashes, interpreter and
SQLite versions, schedules, phase outcomes and pre-teardown resource observations.
The proposed patch's closed-loop result delivery and other changes are not behaviorally
verified by this case. Only cancellation during opening with a running loop is covered.
