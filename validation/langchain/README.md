# LangChain: terminal callback interrupted by level cancellation

This validation calls the real `BaseTool.ainvoke()` / `BaseTool.arun()` /
`AsyncCallbackManagerForToolRun.on_tool_error()` path from unchanged upstream
revision [`980e4a80d01f699ba359db139cd7c8bfc348c110`](https://github.com/langchain-ai/langchain/commit/980e4a80d01f699ba359db139cd7c8bfc348c110),
whose `langchain-core` version is 1.8.0. It uses AnyIO 4.15.1 on asyncio.

On 2026-11-13, [issue #41744](https://github.com/langchain-ai/langchain/issues/41744)
was open. This revision was current `master`; its
[`on_tool_error` and `on_tool_end` methods](https://github.com/langchain-ai/langchain/blob/980e4a80d01f699ba359db139cd7c8bfc348c110/libs/core/langchain_core/callbacks/manager.py#L1189)
had no shielding decorators. The runner refreshes the current issue state and master
method decorators separately from its immutable test revision. The abbreviated
revision mentioned in the issue did not resolve upstream, so this validation does
not attribute its results to that abbreviation or to a published release.

## Run

From the repository root, using the existing development environment:

```sh
uv build
uv run --no-sync python validation/watchdog.py --timeout 300 -- uv run --no-sync python validation/langchain/run.py --wheel dist/cancellens-0.2.0-py3-none-any.whl --repeats 3 --output acceptance-langchain.json
```

The runner downloads a hash-checked archive of the pinned revision, extracts only
`libs/core`, and creates a disposable environment. It installs the supplied local
CancelLens wheel, unchanged `langchain-core`, and the exact runtime dependency pins
in `requirements.txt`. Build dependencies are constrained separately. No existing
environment is synchronized or reinstalled. Python 3.11 or newer is required by
this upstream revision, although CancelLens itself supports Python 3.10.

All tool calls are local. No provider package, credentials, paid inference, server,
or LangSmith tracing is used. The outer process watchdog bounds installation and
execution; the worker also inherits that process group and has an external deadline.

## Contract and controls

The local tool suspends once in `_arun()`. A local async callback handler records
`start`, then records a terminal event only after an actual asynchronous suspension.
The handler uses LangChain's supported `run_inline=True`, keeping callback execution
in the operation Task where CancelLens observes it. The upstream tool implementation
still creates its own `_arun()` Task through `coro_with_context`; this child is not a
separate sweep target.

For each variant, public `sweep()` first establishes two stable, passing baselines.
The selected logical target is point 1, where the actual `BaseTool.arun()` waits for
its tool coroutine. It is selected from its path, independently of its verdict.
Each variant discovers its own prefix and saves its own complete schedule as JSON.
Public `replay()` restores that JSON and repeats the selected schedule three times
on fresh state, with no retries or discarded attempts.

- **AnyIO, unchanged upstream, unshielded handler:** one harness-owned scope
  cancellation interrupts the tool. `on_tool_error()` enters the async handler;
  a subsequent delivery from the same cancelled scope interrupts its terminal
  write. The oracle sees only `start` and reports **FAIL**.
- **AnyIO, local handler shielding control:** the handler wraps its terminal write
  in `anyio.fail_after(0.2, shield=True)`. The real upstream callback path completes
  the terminal write and the selected schedule reports **PASS**. This is a local
  callback control, not an upstream patch or a claim that a released version is fixed.
- **Ordinary asyncio, unshielded handler:** one `Task.cancel()` interrupts the tool;
  its asynchronous error callback then completes. The selected schedule reports
  **PASS** on the same application contract.

The oracle runs outside the operation scope and checks the exact event sequence.
Passing controls also invoke the local tool again with the same callback handler
and verify a second start/end pair before teardown. Teardown awaits independently,
and the worker checks for pending Tasks before event-loop shutdown.

The selected contract is narrower than the full sweep. Cancelling an otherwise
successful terminal callback can also lose its event under ordinary asyncio.
Cancelling inside the local shield can reach the target without an observed
delivery before operation completion, producing **UNRESOLVED**. All these attempts
remain in the evidence, including their reasons; they are not substituted for the
selected tool-cancellation schedule.

## Evidence and limits

`evidence.json` records the completed native run, selected schedules and replay
results, actual events, scope-delivery evidence, package versions, source hashes,
upstream state, and import provenance. Installed upstream modules must match the
unchanged snapshot; CancelLens modules must match the supplied wheel. Failed or
interrupted runs retain incremental `*.worker.json` evidence.

This reproduces the reported terminal-event mechanism through a real LangChain
callback path. It does not reproduce an HTTP disconnect, a network provider, every
callback dispatch configuration, or the entire issue's production scenario.
Non-inline handler Tasks and arbitrary child Task interleavings are outside this
milestone's observation scope. The bounded handler shield is deliberately explicit
and does not modify upstream source or install a monkeypatch.
