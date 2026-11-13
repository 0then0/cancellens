"""Base installation and schedule compatibility do not require AnyIO."""

import asyncio
from types import SimpleNamespace

import pytest

from cancellens import Schedule, sweep
from cancellens import _runner as runner
from tests.test_sweep import simple_case


def test_missing_anyio_does_not_affect_default_mode(monkeypatch):
    def absent(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(runner, "import_module", absent)

    async def operation(state):
        await asyncio.sleep(0)

    case = simple_case(operation)
    asyncio.run(sweep(case)).assert_passed()
    with pytest.raises(RuntimeError, match=r"cancellens\[anyio\]"):
        asyncio.run(sweep(case, cancellation_mode="anyio"))


@pytest.mark.parametrize("version", ["3.7.1", "5.0.0"])
def test_incompatible_anyio_major_is_refused_before_factory(monkeypatch, version):
    monkeypatch.setattr(runner, "import_module", lambda name: SimpleNamespace())
    monkeypatch.setattr(runner.metadata, "version", lambda name: version)

    async def factory():
        raise AssertionError("factory must not run")

    with pytest.raises(RuntimeError, match="AnyIO 4"):
        asyncio.run(sweep(simple_case(None, factory=factory), cancellation_mode="anyio"))


@pytest.mark.parametrize("version", [0, 3])
def test_unknown_schedule_schema_is_refused(version):
    data = Schedule().to_dict()
    data["schema_version"] = version
    with pytest.raises(ValueError, match="schema"):
        Schedule.from_dict(data)


def test_legacy_schedule_cannot_claim_scope_cancellation():
    data = Schedule().to_dict()
    data.update(schema_version=1, cancellation_mode="anyio", anyio_version="4.15.1")
    with pytest.raises(ValueError, match="Version 1"):
        Schedule.from_dict(data)


def test_new_schedule_cannot_silently_lose_mode_metadata():
    data = Schedule().to_dict()
    del data["cancellation_mode"]
    with pytest.raises(ValueError, match="metadata"):
        Schedule.from_dict(data)
