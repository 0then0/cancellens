# Contributing

CancelLens observes native asyncio suspension points and injects real Task cancellation.
Changes should preserve those semantics, resource ownership, and explicit unresolved
outcomes when a schedule cannot be evaluated.

## Before opening an issue or pull request

Search existing issues, then use the [issue forms](https://github.com/0then0/cancellens/issues/new/choose)
for bugs, feature requests, or usage questions. A bug report should include a runnable
case, the expected and actual behavior, and the relevant versions. Use disposable data
and review reports before attaching them.

Follow the [Code of Conduct](CODE_OF_CONDUCT.md). Report suspected vulnerabilities
privately as described in [SECURITY.md](SECURITY.md), rather than posting exploit
details in an issue or pull request.

## Set up a checkout

Use CPython 3.10–3.14 and [uv](https://docs.astral.sh/uv/). The checkout defaults to
Python 3.12; dependencies and development tools are pinned in `uv.lock`.

```sh
git clone https://github.com/0then0/cancellens.git
cd cancellens
uv sync --locked
```

To select another supported interpreter, use `uv sync --locked --python 3.10` and set
`UV_PYTHON=3.10` when running commands. Do not update the lockfile for unrelated work.
If the environment is already prepared, `uv run --no-sync` runs checks without syncing it.

## Editing conventions

`.editorconfig` uses UTF-8, LF, four spaces for Python, and two spaces for structured
configuration and Markdown. Python lines follow Ruff's 100-character limit. Markdown
trailing spaces are preserved for intentional hard line breaks. `.gitattributes`
normalizes text line endings on every platform; do not renormalize unrelated files.
Native Windows batch scripts use CRLF in both editor and checkout settings.

Keep generated build output and local validation reports out of commits. Checked-in
`validation/*/evidence*.json` files are intentional reproducibility artifacts, marked
as generated for GitHub diffs. Do not reformat or edit their observations by hand.
`.env.example` may be committed as a template; it must not contain real credentials.

## Run checks

The unit suite uses disposable local resources and does not download upstream code.
Run it under the external watchdog because some tests deliberately exercise blocked
loops or cancellation-resistant work.

```sh
uv run --no-sync python validation/watchdog.py --timeout 90 -- uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
```

Mypy checks the typed runtime package. Tests and validation scripts are checked by
Ruff and their behavioral checks. For a restricted cache directory, pass
`--cache-dir /tmp/cancellens-mypy` to mypy.

Add regression tests for meaningful behavior: resource invariants, Task status,
lifecycle ownership, replay, or coverage. Oracle evaluation errors must remain distinct
from explicit invariant violations. Cases must release their owned Tasks, threads,
connections, and sockets rather than relying on interpreter shutdown.

## Build and test a wheel

```sh
uv build
uv run --no-sync python validation/wheel_smoke.py --wheel dist/cancellens-0.1.0-py3-none-any.whl
```

The smoke check installs the wheel in a temporary environment outside the checkout,
runs the README and pool examples, and checks that runtime dependencies remain empty.

External integration checks require network access for pinned dependencies and
immutable upstream snapshots. They use disposable environments and local resources.
See [Validation](validation/README.md) for commands and the acceptance contracts.

## CI

Pushes to `main`, pull requests, and manual runs trigger CI. Tagged releases reuse
the same checks before publishing rather than starting a second standalone CI run.

Static checks run once in a dedicated Linux job. The unit and wheel-consumer matrix
runs CPython 3.10–3.14 on Linux and Windows. Separate Linux jobs run
the aiosqlite and native HTTPX/httpcore cases. The HTTPX job pins Python 3.12.15 and
uploads its evidence even when validation fails. Runner images use `ubuntu-latest`
and `windows-latest`; their OS versions are not fixed by this repository.

Actions are pinned to full commit SHAs, with their release versions recorded in
comments. Keep the comment and SHA consistent when updating an action. Checkout does
not retain repository credentials; write and OIDC permissions are scoped to the
release jobs that need them. Branch and pull-request CI supersedes older runs;
tagged release checks are allowed to finish. See GitHub's
[secure use reference](https://docs.github.com/en/actions/reference/security/secure-use)
for the action-pinning and permission model.

POSIX process-group tests cover nested workers, outer timeouts, interruption, and
termination escalation. These tests are skipped on Windows, where the watchdog only
terminates its immediate child.

## Submit changes

Keep changes focused and preserve real Task cancellation. Runtime dependencies should
remain at zero unless a new dependency is necessary and explicitly agreed upon.
Describe the behavior changed, a reproducing scenario, and checks actually run. Update
public documentation when changing a contract. Use English Conventional Commits, for
example `fix: preserve cancellation delivery during cleanup`.

## Releases

Only publish a new package version when a runtime or packaging change requires it.
Validation and documentation updates can be committed without a version bump.

1. Update `pyproject.toml`, `src/cancellens/__init__.py`, and the report version in
   `src/cancellens/_model.py`. Update wheel filenames in CI and release commands,
   then add the release notes to `CHANGELOG.md`.
2. Prepare the pinned release tools with `uv sync --locked --group release`.
3. Run checks, `uv build`, the wheel smoke check, and
   `uv run --group release twine check --strict dist/*`.
4. Push a matching version tag, such as `v0.1.0`, only when ready to publish.

The [publish workflow](.github/workflows/publish.yml) validates the tag, runs CI, builds
and tests distributions, creates a GitHub Release, and uploads to PyPI through the
`pypi` environment's Trusted Publisher. Only the upload job receives OIDC permission.
Ordinary branch pushes run CI without publishing a package.

The workflow also supports a manually published GitHub Release. The PyPI Trusted
Publisher must reference owner `0then0`, repository `cancellens`, workflow `publish.yml`,
and environment `pypi`. Published PyPI files cannot be replaced; a package correction
requires a new version.
