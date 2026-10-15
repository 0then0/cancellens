# Instrumentation, lifecycle and reports

This reference describes CancelLens v0.1 semantics. For installation and a complete
test, start with the [README](../README.md). External resource contracts and evidence
are documented under [Validation](../validation/README.md).

## Coroutine protocol

CancelLens executes the native operation coroutine in a normal asyncio Task. A
`types.coroutine` generator delegates its `send`/`throw` protocol and yields the original
Future or bare `None` back to the Task. The Task's values and exceptions are forwarded
unchanged. Completed awaitables do not yield and therefore do not create points.

A pending Future yielded by `Future.__await__` already carries asyncio's blocking
flag. Re-awaiting it would violate the protocol or require changing that flag.
CancelLens passes it through without re-awaiting or mutating it. A bare yield follows
the ordinary `sleep(0)` path. No global patches, private Task stepping or loop-queue
access are used.

At a matched point, `loop.call_soon(task.cancel, request_token)` schedules a real
cancellation request. The Task registers its original waiter before the callback
runs. For bare yield, the request callback precedes the Task's next resumption.
`throw` only forwards an exception asyncio actually delivered; it never fabricates
cancellation. Completion is inspected outside the operation Task, preserving its
terminal cancellation/exception status and the behavior of done callbacks.

An in-memory request token attributes delivery to the selected caller. Accepted
requests without a matching delivered token are unresolved. A child Task can absorb
cancellation; CPython 3.10 can drop the marker at a cancelled Task boundary.

Instrumentation adds synchronous observation work and wrapper frames. Exact stack
shape, Task.get_coro() identity, wall-clock timing and other tasks' schedules are not
preserved. Each lifecycle phase runs in a separate Task; nested awaits within the
operation retain its Task/context. Factory ContextVar changes must be passed explicitly
through returned state if the operation needs them.

## Attribution and replay

A point contains a one-based index, yielded type (or `bare_yield`), and the visible
Python await chain. Frames contain file basename, function, source line, bytecode
offset and a 16-hex-character SHA-256 code fingerprint. Fingerprinting normalizes
filenames recursively, includes code constants in the hash without exporting them,
and caches at most 512 code objects rather than coroutine frames or resources.

Offsets distinguish awaits on the same source line. The full prefix distinguishes
repeated occurrences at one location. Identical visible paths do not guarantee
identical resource identities or semantic state; the case and oracle provide that
context.

`cr_await` and `gi_yieldfrom` expose Python delegation. C `coroutine_wrapper` can hide
further code. For example, the aiosqlite opening prefix ends at `Connection.__aenter__`
even though `_connect` awaits its Future. A child Task is represented by the parent's
Task wait; a shield by its outer Future. Child internals and lifecycle phases other
than operation are not cancellation targets.

Replay requires matching prefixes before requests are sent. Changed prefixes withhold
cancellation. Schedules cannot be replayed across different Python minor versions;
source edits, dependency changes and scheduling can also invalidate them. Discover a
new schedule after such changes rather than transplanting a numeric target.

## Exploration and ownership

1. Run two complete, uncancelled attempts on fresh factory state and compare traces.
2. Replay each baseline point with one cancellation at its matching prefix.
3. For each valid single-cancel path, replay later observed points with a second request.

Only the prefix through the last request must match; later behavior may differ.
The first cancellation must be delivered before sending the second. Points on cleanup
paths need not exist in the successful baseline. There is no deeper search, general
scheduler, interleaving proof or schedule shrinker.

A successfully returned factory result is captured before waking the runner, so an
external cancellation at handoff still invokes teardown. Factory owns cleanup if it
never returns state. If a cooperative factory returns state while its cancelled phase
is draining, that state is finalized too. A factory that outlives the bounded drain
cannot be forcibly stopped; it must own its partial setup and requires a watchdog.

The oracle defines correctness and may use the same resource again, inspect operation
values/exceptions or wait for application-owned cleanup. Explicit `InvariantViolation`
is a contract failure. Other oracle exceptions are evaluation errors.

## Deadlines and finalization

The runner uses bounded `asyncio.wait` observation windows. On operation timeout,
completion is snapshotted as pending and further tracing/injection disabled before
oracle. Oracle precedes teardown and emergency cancellation of harness phase Tasks.
Invalid/pending schedules cannot establish PASS or FAIL, but oracle observations remain
in the report.

Teardown runs after observation, and its outcome is independent. Confirmed FAIL and
ERROR survive unresolved cleanup. Remaining harness phase Tasks receive best-effort
cancellation and a bounded drain. Application Tasks/threads are the case's responsibility.

A factory/oracle/teardown that concludes as cancelled is ERROR. External cancellation
of the sweep caller propagates after attempted teardown where state exists; a second
external cancellation can interrupt finalization. Exploration may exceed total_timeout
by the current reserved oracle/teardown windows and drain. In-process deadlines need
a functioning loop and cannot kill threads or native code. Use the subprocess watchdog
for potentially hanging cases. The validation supervisor owns a POSIX process group;
its nested workers inherit that group so an outer deadline can terminate them along
with their manager. Windows supports immediate-child termination only. See
[process supervision](../validation/README.md#process-supervision) for the command and
ownership rules.

## JSON schema version 1

`Report.to_dict()` and `Report.to_json()` contain:

- `schema_version`: integer 1; reject unsupported versions when consuming reports.
- `package_version`, `interpreter`, `implementation`: runtime provenance.
- `mode`: `sweep` or `replay`; replay coverage is only for its selected schedule.
- `outcome`: `PASS`, `FAIL`, `UNRESOLVED`, or `ERROR`.
- `limits`: configured point, attempt, depth and deadline bounds.
- `coverage`: baseline points, planned/valid single/double schedules, attempt count,
  completeness and reasons.
- `attempts`: lifecycle records.

Attempts expose `schedule`, `trace`, `cancellations`, `completion`, `setup`, `oracle`,
`teardown`, `outcome`, `reasons`, `valid_schedule` and `trace_complete`. A schedule stores
`cancel_at`, its required `prefix` and a CPython-major.minor interpreter key. Restore
it with `Schedule.from_dict`. Missing attribution is rejected; an empty schedule is
an uncancelled replay control.

Cancellations record point, Task.cancel acceptance and token delivery. Completion
exports state and exception type, excluding values and exception instances. Phases
export outcome/detail or null when not run. Automatic error details contain exception
types, never messages or traceback locals; explicit invariant text is included.

Aggregate precedence is FAIL, ERROR, UNRESOLVED, PASS. Coverage is computed independently:
FAIL or ERROR may coexist with incomplete coverage. There is no deserializer restoring
Python exceptions or operation values; extract a saved schedule to replay instead.
