# Instrumentation, lifecycle and reports

This reference describes the current CancelLens API and report schema. For installation
and a complete test, start with the [README](../README.md). External resource contracts
and evidence are documented under [Validation](../validation/README.md).

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

In the default asyncio mode, `loop.call_soon(task.cancel, request_token)` schedules a real
cancellation request. The Task registers its original waiter before the callback
runs. For bare yield, the request callback precedes the Task's next resumption.
`throw` only forwards an exception asyncio actually delivered; it never fabricates
cancellation. Completion is inspected outside the operation Task, preserving its
terminal cancellation/exception status and the behavior of done callbacks. AnyIO
scope suppression is recorded separately as `scope_cancelled`, since that Task
returns normally after the scope interrupts its operation.

In asyncio mode, an in-memory request token attributes delivery to the selected caller. Accepted
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
source edits, dependency changes and scheduling can also invalidate them. AnyIO
schedules additionally require the same exact AnyIO version and the asyncio backend.
Mode/backend mismatches are refused before execution, and environment incompatibility
is recorded before the factory runs. Replay infers cancellation mode from a saved
schedule unless an agreeing mode is explicitly supplied. Discover a new schedule
after changes rather than transplanting a numeric target.

## Exploration and ownership

1. Run two complete, uncancelled attempts on fresh factory state and compare traces.
2. Replay each baseline point with one cancellation at its matching prefix.
3. In asyncio mode, replay later observed points on each valid single-cancel path
   with a second request when `max_depth=2`.

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

## AnyIO scope ownership and delivery

Select `sweep(case, cancellation_mode="anyio", backend="asyncio")` after installing
`cancellens[anyio]`. The optional dependency supports AnyIO 4. The normal asyncio mode
requires no AnyIO installation and does not import it. The underlying semantics are
described in [AnyIO's cancellation documentation](https://anyio.readthedocs.io/en/stable/cancellation.html).

The operation Task creates and enters one harness-owned `CancelScope` before delegating
the native coroutine, and exits it in the same Task. Delegation never moves coroutine
steps across Tasks or scopes. At a matched target, a scheduled callback calls the
scope's real public `cancel()` exactly once. CancelLens does not emulate level
cancellation with repeated `Task.cancel()` calls or manufactured exceptions. AnyIO
may deliver cancellation repeatedly while that scope remains effectively cancelled.
The sweep plans singles only, including when the configured depth limit is two.

An operation's inner shield can delay delivery until it exits, or let the operation
finish without an observed exception. Cancellation during cleanup can interrupt a
second await from the same request. Application cleanup can use a bounded shield,
such as `anyio.fail_after(timeout, shield=True)`, to finish within its own deadline.
Shielding is not proof that a requested cancellation was delivered.

The observer records `target_reached` and `prefix_matched` independently of
`scope_cancel_called`, `exception_observed`, and `scope_cancelled_caught`.
`Attempt.cancellation_exceptions` counts cancellation exceptions supplied to the
observed operation, not requests. `cancel_called` confirms a request only. AnyIO's
public `cancelled_caught` confirms that the owned scope suppressed its own
cancellation exception. Only that fact together with a cancellation exception
observed in the operation establishes `delivered=True` for this mode.

When delivery cannot be attributed, `delivered` is null and
`delivery_unknown_reason` explains the missing evidence. For example, user code can
suppress cancellation before the owner sees it, or the owner can suppress cancellation
without the observer seeing delivery through a child boundary. Such schedules stay
UNRESOLVED. Exception text, memory addresses, and private AnyIO implementation details
are not used for attribution. Repeated exceptions do not establish multiple requests.

`Completion.state="scope_cancelled"` means the operation was interrupted and its
owner suppressed the exception; the operation Task returned normally. `returned`
means the operation itself returned, including after user suppression; it does not
prove absence of cancellation. `raised`, `cancelled`, `pending`, and `not_started`
retain their existing meanings. A scope stack or harness lifecycle error is ERROR.
An explicit oracle violation establishes FAIL only on a confirmed applicable schedule.

Oracle and teardown execute in separate Tasks outside the owned operation scope. They
can await, inspect resources, and attempt reuse before emergency cleanup. A pending
operation keeps its scope in its own Task until it can unwind; the runner never exits
that scope from a lifecycle Task. After deadlines, delivery and completion evidence
remain the snapshot used for the oracle rather than being retroactively established
by emergency cancellation.

This milestone excludes Trio, child-Task sweeps, multiple controlled scopes,
double-cancel scheduling, and comprehensive AnyIO API coverage. Child Tasks remain
observation boundaries. The oracle must inspect or await their owned work; a parent
wait and scope state alone cannot prove what a child completed.

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
of the sweep caller propagates after attempted teardown where state exists. In AnyIO
mode, shield scopes in the runner protect final oracle/teardown wait windows and the
bounded drain when the caller itself is inside an effectively cancelled external
scope. These lifecycle shields are separate from the operation-owned scheduled scope;
they add no cancellation targets or operation awaits. After finalization, a public
AnyIO cancellation checkpoint propagates deferred caller cancellation before the next
attempt or return. Repeated direct `Task.cancel()` requests can still interrupt these
windows because AnyIO shielding blocks scope cancellation only. Default asyncio mode
retains its existing finalization behavior. Exploration may exceed total_timeout
by the current reserved oracle/teardown windows and drain. In-process deadlines need
a functioning loop and cannot kill threads or native code. Use the subprocess watchdog
for potentially hanging cases. The validation supervisor owns a POSIX process group;
its nested workers inherit that group so an outer deadline can terminate them along
with their manager. Windows supports immediate-child termination only. See
[process supervision](../validation/README.md#process-supervision) for the command and
ownership rules.

## JSON schema version 2

`Report.to_dict()` and `Report.to_json()` contain:

- `schema_version`: integer 2; reject unsupported versions when consuming reports.
- `package_version`, `interpreter`, `implementation`: runtime provenance.
- `mode`: `sweep` or `replay`; replay coverage is only for its selected schedule.
- `cancellation_mode`: `asyncio` or `anyio`; `backend`: `asyncio`; `anyio_version`: the
  actual installed version for AnyIO mode, otherwise null.
- `outcome`: `PASS`, `FAIL`, `UNRESOLVED`, or `ERROR`.
- `limits`: configured point, attempt, depth and deadline bounds.
- `coverage`: baseline points, planned/valid single/double schedules, attempt count,
  completeness and reasons.
- `attempts`: lifecycle records.

Attempts expose `schedule`, `trace`, `cancellations`, `completion`, `setup`, `oracle`,
`teardown`, `outcome`, `reasons`, `valid_schedule`, `trace_complete`, and
`cancellation_exceptions`. A schedule stores `cancel_at`, its required `prefix`, a
CPython-major.minor interpreter key, schema version, cancellation mode, backend, and
AnyIO version. Restore it with `Schedule.from_dict`. Version 2 requires explicit
`cancellation_mode`, `backend`, and `anyio_version` keys, including a null AnyIO version
for asyncio mode. Missing mode/environment metadata or prefix attribution is rejected;
an empty schedule is an uncancelled replay control.

Version 1 reports still contain valid JSON with their original Task cancellation
meaning. Schedules without version/mode metadata deserialize as version 1 asyncio
schedules. Version 1 cannot describe AnyIO cancellation; replay never converts an
old `Task.cancel()` schedule into a scope cancellation. Reports have no Python-object
deserializer; consumers can read either supported schema and extract a schedule.

Cancellations retain point, acceptance, and delivery fields. Asyncio delivery uses
the existing request token; AnyIO adds the public scope and observation facts above.
An unknown AnyIO delivery is null, while confirmed delivery is true. Completion
exports state and exception type, excluding values and exception instances. Phases
export outcome/detail or null when not run. Automatic error details contain exception
types, never messages or traceback locals; explicit invariant text is included.

Aggregate precedence is FAIL, ERROR, UNRESOLVED, PASS. Coverage is computed independently:
FAIL or ERROR may coexist with incomplete coverage. There is no deserializer restoring
Python exceptions or operation values; extract a saved schedule to replay instead.
