"""Sweep and JSON replay of an unchanged LangChain tool callback path."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path

import langchain_core
from case import callback_case

import cancellens
from cancellens import Limits, Outcome, Schedule, replay, sweep

LIMITS = Limits(max_depth=1, max_points=40, max_attempts=45, phase_timeout=1, total_timeout=15)


def module_digests(package) -> dict[str, str]:
    root = Path(package.__file__).parent
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.py"))
    }


def matched(report, observations: list[dict], expected: str) -> bool:
    attempt = report.attempts[0]
    if not (
        attempt.outcome.value == expected
        and attempt.valid_schedule
        and attempt.oracle.outcome in (Outcome.PASS, Outcome.FAIL)
        and attempt.teardown.outcome is Outcome.PASS
        and observations[0].get("oracle_ran")
        and observations[0].get("teardown_ran")
    ):
        return False
    events = observations[0]["events"]
    if expected == "FAIL":
        return (
            events == ["start"]
            and observations[0]["callback_starts"] == ["error:CancelledError"]
            and attempt.oracle.detail == "started tool run records exactly one terminal event"
        )
    return events == ["start", "error:CancelledError", "start", "end"] and observations[0].get(
        "reused"
    )


async def validate(data: dict, save, repeats: int) -> None:
    # Each control discovers its own prefixes; code/environment-specific schedules
    # are never transplanted across modes or shield settings.
    for label, mode, shielded, expected in (
        ("anyio_unshielded", "anyio", False, "FAIL"),
        ("anyio_local_handler_shield", "anyio", True, "PASS"),
        ("asyncio_unshielded", "asyncio", False, "PASS"),
    ):
        observations: list[dict] = []
        report = await sweep(
            callback_case(observations, shielded=shielded), cancellation_mode=mode, limits=LIMITS
        )
        entry = {"sweep": report.to_dict(), "observations": observations, "replays": []}
        data["variants"][label] = entry
        if (
            len(report.attempts) < 2
            or any(a.outcome is not Outcome.PASS for a in report.attempts[:2])
            or report.attempts[0].trace != report.attempts[1].trace
        ):
            data["errors"].append(label + ": stable passing baselines unavailable")
        # The first baseline suspension is the real BaseTool.arun wait for
        # coro_with_context(_arun()). It is chosen by logical path, never verdict.
        selected = next((a for a in report.attempts if a.schedule.cancel_at == (1,)), None)
        if selected is None:
            data["errors"].append(label + ": first tool suspension unavailable")
            save()
            continue
        entry["selected_schedule"] = selected.schedule.to_dict()
        first = selected.schedule.prefix[0]
        entry["selected_upstream_path"] = [f.function for f in first.stack]
        if not any("BaseTool.arun" in f.function for f in first.stack):
            data["errors"].append(label + ": selected point is outside BaseTool.arun")
        # Inspect discovery independently, including actual terminal-event state.
        index = report.attempts.index(selected)
        from cancellens import Coverage, Report

        view = Report([selected], Coverage(complete=selected.valid_schedule), {})
        if not matched(view, observations[index : index + 1], expected):
            data["errors"].append(label + ": discovery contract mismatch")
        save()
        for repetition in range(repeats):
            schedule = Schedule.from_dict(json.loads(json.dumps(entry["selected_schedule"])))
            obs: list[dict] = []
            replayed = await replay(callback_case(obs, shielded=shielded), schedule, limits=LIMITS)
            valid = matched(replayed, obs, expected)
            entry["replays"].append(
                {
                    "repetition": repetition + 1,
                    "report": replayed.to_dict(),
                    "observations": obs,
                    "contract_matched": valid,
                }
            )
            if not valid:
                data["errors"].append(f"{label}: replay {repetition + 1} contract mismatch")
            save()
    pending = [
        t.get_coro().__qualname__
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]
    data["pending_tasks_before_loop_shutdown"] = pending
    if pending:
        data["errors"].append("tasks pending before loop shutdown")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    data = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "import_path": cancellens.__file__,
        "versions": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "module_sha256": {
            "cancellens": module_digests(cancellens),
            "langchain_core": module_digests(langchain_core),
        },
        "variants": {},
        "errors": [],
    }

    def save() -> None:
        args.output.write_text(json.dumps(data, indent=2) + "\n")

    save()
    try:
        asyncio.run(validate(data, save, args.repeats))
    except Exception as exc:
        data["errors"].append(type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        save()
    print(f"LangChain callback contracts: errors={data['errors']}")
    return int(bool(data["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
