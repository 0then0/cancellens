# HTTPX/httpcore: repeated cancellation during response cleanup

This case exercises interrupted HTTP/1.1 response cleanup using real loopback TCP,
HTTPX's default transport, and a one-slot connection pool. Both cancellation requests
come from the published CancelLens 0.1.0 wheel. The oracle checks connection state and
runs another request on the same pool before final cleanup.

The scenario is based on [HTTPX #3782](https://github.com/encode/httpx/issues/3782) and
the state-lock regression test in [httpcore PR #1113](https://github.com/encode/httpcore/pull/1113).
It validates one retry contract, not general cancellation safety for HTTPX or httpcore.

## Snapshots and expected contract

- Base: [`10a658221deb38a4c5b16db55ab554b0bf731707`](https://github.com/encode/httpcore/commit/10a658221deb38a4c5b16db55ab554b0bf731707).
- Proposed fix: [`1fb0ecec6d38da241c5aca08777195aae3830d1d`](https://github.com/encode/httpcore/commit/1fb0ecec6d38da241c5aca08777195aae3830d1d).

Both snapshots report httpcore 1.0.9, so their version strings do not distinguish them.
The proposed patch resets `HTTP11ConnectionByteStream._closed` after an exception,
allowing an outer caller to retry cleanup. The base leaves the flag set even if
cleanup was interrupted before releasing the connection.

For the selected double-cancel schedule `(1, 2)`, the base must demonstrate a stranded
ACTIVE connection and failure of the next request to acquire the only slot. The
proposed snapshot must complete the retry, close the connection, and serve the next
request successfully. Baselines and body-read single-cancel controls must pass on
both snapshots.

PR status is recorded with a timestamp in each report. It was open and unmerged when
first checked on October 14, 2026; this case describes the selected proposed snapshot,
not a confirmed released correction.

## Run the comparison

Use an existing development environment and run from the repository root:

```sh
uv run --no-sync python validation/watchdog.py --timeout 300 -- uv run --no-sync python validation/httpx/run.py --repeats 10 --output acceptance-httpx.json
```

Network access is required for GitHub, PyPI, and the package index. The HTTP workload
uses only `127.0.0.1`. `--python /path/to/python` selects the acceptance interpreter.
Recorded evidence uses CPython 3.12.15 on macOS arm64 with the standard asyncio loop.
The Linux CI job pins Python 3.12.15 and uses `ubuntu-latest`; the OS image is not pinned.

The runner downloads the published `cancellens-0.1.0-py3-none-any.whl` and verifies
its bytes against PyPI metadata and this pinned SHA-256:

```text
e80118f7c3f232ebb3772731ed6a3eb0b91ee1d75fdc2fb31ad25e18182adabd
```

An optional `--wheel /path/to/cached.whl` must contain that exact artifact. A rebuilt
local wheel is not substituted. Each upstream snapshot is built in a disposable
environment beside the published wheel. Workers run outside the source checkout with
PYTHONPATH and PYTHONHOME removed.

Runtime versions are pinned in [requirements.txt](requirements.txt): HTTPX 0.28.1,
AnyIO 4.12.1, h11 0.16.0, certifi 2026.2.25, idna 3.11, and typing-extensions 4.15.0.
Upstream build dependencies are pinned in [build-constraints.txt](build-constraints.txt).
These dependencies do not become CancelLens runtime dependencies.

## Server, controller, and cancellation ordering

The factory starts a local asyncio HTTP/1.1 server and opens a streaming response.
Connection establishment and response headers happen before observation, keeping
connection setup outside the selected cancellation scenario. The server sends headers
with Content-Length 1, then waits for the controller to release the response body.
The operation consumes `Response.aiter_raw()` and calls `Response.aclose()` in `finally`.

1. The upstream `receive_response_body.started` trace callback signals the controller
   without suspending. When the controller resumes, it confirms that the operation
   Task's waiter belongs to the native socket protocol's read event. CancelLens has
   queued the first real `Task.cancel(token)` request at this suspension.
2. The controller queues server body release after that cancellation callback. This
   lets uncancelled baselines finish while preserving cancellation of a pending read.
3. Cancellation enters the real HTTP11 byte-stream cleanup. A controller Task already
   owns the actual upstream `_state_lock`, acquired after response headers. At the
   `response_closed.started` notification, the controller confirms that the operation
   Task/Future pair is waiting on that real AnyIO-backed lock.
4. CancelLens queues its second cancellation. A controller-only ready-queue handoff
   keeps the lock held through delivery: the cancellation callback queues the operation
   before the handoff queues controller resumption. `response_closed.failed` records
   that the lock was still held when cleanup received CancelledError. Without the
   second cancellation, the same handoff releases the lock normally.
5. On the base, the byte-stream closed flag prevents the real outer `PoolByteStream`
   error path from retrying unfinished cleanup. On the proposed snapshot, resetting
   that flag allows the retry to acquire the released lock and close the connection.

No sleep selects cancellation targets. The handoff adds no await inside upstream
cleanup. Timeouts are failure bounds. The controller never cancels the operation.

## Hooks and oracle

The hooks control server body delivery, notify read/cleanup phases, hold the real state
lock, and observe resource state. They do not replace upstream iteration, `aclose()`,
`_response_closed()`, pool release, or assignment. HTTPX handling, httpcore retry and
bookkeeping, and AnyIO's asyncio socket operations all use their installed code.

Private Task, lock, byte-stream, pool, and socket attributes are used only for these
pinned versions. Each report verifies the installed httpcore source against its
snapshot and installed CancelLens modules against the wheel.

After operation completion, the oracle records connection state, the byte-stream
flag, socket closure, pool requests, and active connections. It then sends `GET /next`
on the same client and pool, before `client.aclose()`:

- Base selected double: one ACTIVE unavailable connection, zero original requests,
  an open socket, one cleanup start, and PoolTimeout on the next request. The observed
  stranded slot establishes the invariant violation; the timeout alone would not.
- Proposed selected double: two cleanup starts, CLOSED connection and socket, no
  original pool entries, and a successful 200 response to the next request.
- Baseline: an IDLE reusable connection and a successful next request.
- Single `(1,)`: a CLOSED connection, a freed slot, and a successful next request.

Server errors and unexplained pool timeouts are ERROR. Undelivered cancellation,
prefix mismatch, and phase bounds retain CancelLens's UNRESOLVED outcome. Acceptance
combines pre-teardown observations with the report's schedule and delivery records;
invalid schedules cannot establish the selected contract.

Only after the oracle does teardown release gates, join the controller, close the
client, listener, and server streams, and consume task exceptions. Evidence checks
actual socket descriptors, completed owned tasks, and zero pool connections before
asyncio.run shutdown. Teardown outcomes remain separate from oracle outcomes.

The outer watchdog owns the POSIX process group; workers inherit it while retaining
individual deadlines. See [process supervision](../README.md#process-supervision).
Summary and incremental worker JSON files are retained on failure and uploaded by CI.

## Recorded results

[evidence.json](evidence.json) contains 72 fresh attempts, 36 per snapshot:

- One discovery sweep per snapshot: two matching baselines, two singles, and two doubles.
- Ten fresh repetitions per snapshot: baseline, single `(1,)`, and a JSON-restored
  double `(1, 2)` replay in each repetition.
- Selected double, including discovery: base 11 FAIL; proposed snapshot 11 PASS.
- Baseline controls: 24 PASS. Selected single controls: 22 PASS.
- All experimental cancellation requests accepted and delivered; all teardown phases
  PASS; no ERROR or UNRESOLVED attempts and no pending owned tasks at loop shutdown.

The full sweep is FAIL on both snapshots. Single `(2,)`, cancellation during ordinary
response close after consuming the body, leaves an ACTIVE connection and one original
request on both sides. That path has no streaming-error outer retry and is outside
the selected double-cancel contract. It remains in `outside_selected_contract` and
the full reports. Double `(1, 3)` passes on both snapshots.

Each snapshot discovers its own prefix. The visible Python stack stops at the
operation's `async for` because `async_generator_asend` hides deeper frames. The two
selected prefixes therefore happen to be equal; prefixes alone do not identify the
upstream commit. Verified module hashes and independent live waiter checks provide
that attribution. Prefix and delivery checks are not weakened. Changed code or
interpreter versions may require rediscovery.

[evidence-history.json](evidence-history.json) preserves two earlier complete comparison
runs, including their errors and source hashes. These historical reports are not the
acceptance evidence for the current harness. One contains a corrected package-name
normalization error; the other predates holding the state lock through delivery of
the second cancellation. Neither report is discarded or counted toward current results.

## Scope

This case covers one incomplete-body HTTP/1.1 response, loopback TCP, a contended
state lock, raw asyncio Task.cancel, and the pinned dependencies. It does not certify
HTTP/2, concurrent close callers, network-close interruption, or all cleanup paths.
AnyIO inside HTTPX does not imply support for AnyIO level cancellation or Trio.
Loopback HTTP requires no TLS; TLS verification is not disabled.
