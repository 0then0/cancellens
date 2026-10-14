"""Executed in the isolated wheel environment by run.py's external watchdog."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
from pathlib import Path

import aiosqlite
from case import connection_case

import cancellens
from cancellens import Limits, Outcome, Schedule, replay, sweep


async def validate(repeats: int, expected: str) -> dict:
    series = []
    limits = Limits(max_depth=1, max_attempts=20, total_timeout=15, phase_timeout=4)
    for _ in range(repeats):
        observations = []
        case = connection_case(observations)
        report = await sweep(case, limits=limits)
        assert report.outcome.value == expected, report.to_json()
        assert report.coverage.complete, report.to_json()
        assert report.coverage.baseline_points >= 1
        for baseline in report.attempts[:2]:
            assert baseline.outcome is Outcome.PASS
            assert not baseline.cancellations
        opening = next(a for a in report.attempts if a.schedule.cancel_at == (1,))
        # Connection.__await__ returns a C coroutine_wrapper, so the visible
        # Python chain ends at __aenter__. This is an explicitly tested boundary.
        assert any(
            f.function in ("Connection.__aenter__", "__aenter__") for f in opening.trace[0].stack
        )
        assert opening.trace[0].kind == "_asyncio.Future"
        assert opening.completion.state == "cancelled"
        assert opening.cancellations[0].accepted and opening.cancellations[0].delivered
        assert opening.outcome.value == expected
        if expected == "FAIL":
            assert (
                opening.oracle.detail == "native SQLite connection closed before harness teardown"
            )
        # Serialize/restore the selected schedule, then repeat it on a fresh case.
        replay_observations = []
        schedule = Schedule.from_dict(json.loads(json.dumps(opening.schedule.to_dict())))
        repeated = await replay(connection_case(replay_observations), schedule, limits=limits)
        assert repeated.outcome.value == expected, repeated.to_json()
        assert repeated.coverage.complete
        series.append(
            {
                "sweep": report.to_dict(),
                "observations": observations,
                "replay": repeated.to_dict(),
                "replay_observations": replay_observations,
            }
        )
    return {
        "interpreter": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "sqlite": __import__("sqlite3").sqlite_version,
        "cancellens_version": cancellens.__version__,
        "aiosqlite_version": aiosqlite.__version__,
        "installed_core_sha256": hashlib.sha256(
            Path(aiosqlite.__file__).with_name("core.py").read_bytes()
        ).hexdigest(),
        "expected": expected,
        "repeats": repeats,
        "series": series,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected", choices=["PASS", "FAIL"], required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    result = asyncio.run(validate(args.repeats, args.expected))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"aiosqlite {args.expected}: {args.repeats} fresh sweeps + opening replays")


if __name__ == "__main__":
    main()
