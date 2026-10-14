# Changelog

## 0.1.0

Initial release.

- Fresh-case lifecycle with separate setup, operation, oracle and teardown callbacks.
- Actual asyncio suspension tracing with real Task cancellation and preserved Task completion state.
- Single-cancel sweeps and bounded second cancellation on observed cancellation paths.
- Prefix-checked replay, explicit coverage, four outcomes and versioned JSON reports.
- Cooperative deadlines and a subprocess watchdog for validation.
- Reproducible aiosqlite cancelled-opening case against immutable base and proposed-fix commits.

Supports CPython 3.10–3.14 with the standard asyncio loop and default Task factory.
