# Contributing

CancelLens is a small asyncio testing library. Changes should preserve real Task
cancellation, avoid adding suspension points to the operation, and retain explicit
coverage and unresolved outcomes when a schedule cannot be evaluated.

## Development environment

Use CPython 3.10–3.14 and uv. The default local interpreter is Python 3.12.

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python validation/watchdog.py --timeout 90 -- uv run pytest -q
```

For another interpreter, set `UV_PYTHON`, for example `UV_PYTHON=3.10 uv run pytest`.
CI sets this variable for every matrix job and checks the selected interpreter before
running tests. CI covers Linux and Windows; local tests can also run on macOS.

Use disposable resources in tests. Tests that may block the loop, resist cancellation
or leave a worker thread running must execute under an external subprocess watchdog.
Add behavior tests for resource invariants, Task status, lifecycle ownership, replay
or coverage when changing those contracts. An oracle error must remain distinct from
an explicit invariant violation.

## Build and integration checks

```sh
uv build
uv run --group release twine check --strict dist/*
uv run python validation/wheel_smoke.py --wheel dist/cancellens-0.1.0-py3-none-any.whl
uv run python validation/watchdog.py --timeout 300 -- uv run python validation/aiosqlite/run.py --wheel dist/cancellens-0.1.0-py3-none-any.whl --output acceptance.json
```

The wheel smoke check installs the wheel in a temporary environment outside the
checkout and executes the README example. The aiosqlite check downloads immutable
snapshots and uses temporary SQLite databases and worker threads. It needs network
access to GitHub and the package index. Read the [case documentation](validation/aiosqlite/README.md)
before changing its hooks or oracle.

## Pull requests

Describe the behavior being changed, a reproducing case when applicable, and checks
actually executed. Update user documentation when changing a public contract. Keep
runtime dependencies at zero unless a dependency is required and explicitly approved.
Use the lockfile and preserve unsupported-runtime boundaries.

## Releases

Update the package version and changelog, then push a matching version tag such as
`v0.1.0`. The `publish.yml` workflow runs the full checks, validates the tag, builds and
tests distributions, creates a GitHub Release with changelog notes and artifacts, and
uploads to PyPI through the `pypi` environment's Trusted Publisher. Only the upload job
receives OIDC permission. Pushing ordinary commits does not publish a package.

The workflow also accepts manually published GitHub Releases. Configure the PyPI
publisher for owner `0then0`, repository `cancellens`, workflow filename `publish.yml`
and environment `pypi` before releasing. PyPI files cannot be overwritten; corrections
to a published package require a new version.
