# Changelog

## Unreleased

- Extend CPython support through 3.15, including the asyncio and optional AnyIO modes,
  package metadata, and the Linux/Windows CI matrix.

- Add optional AnyIO 4 scope cancellation on the asyncio backend through public single
  sweeps and replay, with one operation-owned scope and independent lifecycle budgets.
- Record scope requests, observed cancellation, owner suppression, delivery uncertainty,
  and environment metadata in schema version 2; preserve legacy asyncio schedules.
- Preserve application errors left after an AnyIO scope suppresses cancellation in a
  mixed exception group, while reporting scope lifecycle failures as errors.
- Add synthetic resource-reuse and terminal-callback contracts, bounded shielding
  controls, and isolated validation of a pinned real LangChain callback path.

- Add EditorConfig and Git attributes for consistent text handling across platforms.
- Add security and conduct policies, issue forms, and a pull request template.
- Pin GitHub Actions to immutable commits and run static checks outside the test matrix.

- Add native HTTPX/httpcore double-cancel validation against immutable base and
  proposed-fix snapshots, using the published CancelLens 0.1.0 wheel.
- Keep nested validation workers in the outer watchdog's process group and stop
  remaining members after a timeout or signal.
- Add subprocess regression tests for deadlines, interruption, and resource release.
- Document installation, report interpretation, reproducible validation, and
  contributor workflows.

## 0.1.0

Initial release.

- Fresh-case lifecycle with separate setup, operation, oracle and teardown callbacks.
- Actual asyncio suspension tracing with real Task cancellation and preserved Task completion state.
- Single-cancel sweeps and bounded second cancellation on observed cancellation paths.
- Prefix-checked replay, explicit coverage, four outcomes and versioned JSON reports.
- Cooperative deadlines and a subprocess watchdog for validation.
- Reproducible aiosqlite cancelled-opening case against immutable base and proposed-fix commits.

Supports CPython 3.10–3.14 with the standard asyncio loop and default Task factory.
