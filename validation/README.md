# Validation

The validation tools check installed CancelLens wheels and external resource contracts.
They are separate from the unit suite: integration runs download immutable upstream
snapshots and install pinned dependencies in disposable environments.

Run commands from the repository root after preparing the development environment
with `uv sync --locked`. Use `uv run --no-sync` to preserve an existing environment.

## Wheel smoke check

```sh
uv build
uv run --no-sync python validation/watchdog.py --timeout 90 -- uv run --no-sync python validation/wheel_smoke.py --wheel dist/cancellens-0.1.0-py3-none-any.whl
```

This installs the local wheel outside the source checkout, executes the README and
pool examples, verifies the import path, and checks for zero runtime dependencies.

## External cases

### aiosqlite: cancellation during opening

```sh
uv build
uv run --no-sync python validation/watchdog.py --timeout 300 -- uv run --no-sync python validation/aiosqlite/run.py --wheel dist/cancellens-0.1.0-py3-none-any.whl --repeats 5 --output acceptance-aiosqlite.json
```

The case checks native SQLite ownership and worker termination before teardown.
It compares a pinned base and proposed patch using the supplied local wheel.
See the [contract and evidence](aiosqlite/README.md).

### HTTPX/httpcore: repeated cancellation during HTTP/1.1 cleanup

```sh
uv run --no-sync python validation/watchdog.py --timeout 300 -- uv run --no-sync python validation/httpx/run.py --repeats 10 --output acceptance-httpx.json
```

This case downloads the exact published CancelLens 0.1.0 wheel and verifies its hash.
It uses real loopback TCP and a one-slot pool. Both cancellations come from CancelLens;
the oracle tests pool reuse before closing the client. See the [contract, hooks, and
evidence](httpx/README.md). A cached wheel is accepted only if it has the same published hash.

## Read the results

An integration command exits successfully when its selected contract matches the
expected comparison, including an expected FAIL on the base snapshot. It does not
mean every schedule in every embedded CancelLens report passed.

Read the selected schedules, cancellation acceptance/delivery, oracle observations,
coverage, and teardown results together. An unexplained timeout, unresolved prefix,
or undelivered cancellation does not establish a resource bug. Each case documents
outcomes outside its selected contract separately.

Checked-in evidence records the environment and artifacts used for that run. New runs
should write to a separate output file. Source or interpreter changes can invalidate
saved prefixes; discover new prefixes within each snapshot instead of transplanting
serialized schedules between commits.

## Process supervision

`watchdog.py --timeout SECONDS -- COMMAND` bounds a command with an external wall clock.
On deadline it returns 124. On POSIX it sends SIGTERM to its owned process group, then
SIGKILL to any remaining group members. It also kills the group if its command ends
from a signal or its own wait is interrupted. Windows supports immediate-child
termination only; process-group guarantees do not apply there.

A top-level watchdog creates a new process session. Integration workers and the wheel
consumer process call `watchdog.run(..., new_session=False)`, inheriting the outer
process group while retaining their individual timeouts. This lets the outer watchdog
stop a blocked worker even if its manager exits first. Worker code must not detach
itself into a separate session.

Deadlines are failure bounds, not cancellation target selectors. Cases must still
check owned tasks and resources explicitly before normal process exit. A forced
process stop closes OS resources but cannot undo external side effects.

## Evidence on failure

The HTTPX runner saves its summary before setup and retains completed worker reports
when checks fail. Workers save results after each replay. On interruption, incremental
`*.worker.json` files remain beside the requested output. The HTTPX CI job uploads
both the summary and these files with `if: always()`.

The aiosqlite runner writes its aggregate evidence after the comparison completes;
for an interrupted run, use the command's exit status and CI log. Its checked-in
report records a completed run.
