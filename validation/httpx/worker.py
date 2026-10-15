"""Run native HTTPX controls and snapshot-specific cancellation replays."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import platform
import sys
from collections import Counter
from pathlib import Path

from case import response_case

import cancellens
from cancellens import Coverage, Limits, Outcome, Report, Schedule, replay, sweep

LIMITS = Limits(max_points=30, max_attempts=40, phase_timeout=4, total_timeout=30)
INVARIANT = "HTTP/1.1 cleanup releases the active slot before harness teardown"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def installed_modules() -> dict:
    import anyio
    import httpcore
    import httpx

    modules = {}
    for name, module in (("cancellens", cancellens), ("httpcore", httpcore), ("httpx", httpx)):
        root = Path(module.__file__).parent
        modules[name] = {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob("*.py"))}
    modules["anyio"] = {
        "_backends/_asyncio.py": digest(Path(anyio.__file__).parent / "_backends/_asyncio.py")
    }
    return modules


def acceptable(report, observations: list[dict], expected: str, depth: int) -> bool:
    if len(report.attempts) != 1 or len(observations) != 1:
        return False
    attempt, observation = report.attempts[0], observations[0]
    cleanup = observation.get("cleanup", {})
    healthy_cleanup = (
        all(
            cleanup.get(k) is True
            for k in (
                "controller_done",
                "server_tasks_done",
                "server_writers_closed",
                "listener_closed",
                "server_sockets_closed",
                "listener_sockets_closed",
                "client_socket_closed",
            )
        )
        and cleanup.get("pool_connections") == 0
        and cleanup.get("server_errors") == []
    )
    common = (
        attempt.outcome.value == expected
        and report.coverage.complete
        and attempt.valid_schedule
        and attempt.trace_complete
        and attempt.teardown.outcome is Outcome.PASS
        and len(attempt.cancellations) == depth
        and (depth != 2 or observation.get("cleanup_failed_while_lock_held") is True)
        and all(c.accepted and c.delivered for c in attempt.cancellations)
        and attempt.completion.state == ("cancelled" if depth else "returned")
        and observation.get("read_waiter_is_native") is True
        and observation.get("cleanup_waiter_is_state_lock") is True
        and observation.get("pool_requests") == 0
        and observation.get("server_errors") == []
        and healthy_cleanup
    )
    if expected == "FAIL":
        return (
            common
            and attempt.oracle.detail == INVARIANT
            and observation.get("pool_active") == 1
            and observation.get("connection_closed") is False
            and observation.get("client_socket_closed") is False
            and observation.get("close_starts") == 1
            and observation.get("byte_stream_closed") is True
            and observation.get("next_operation", {}).get("result") == "PoolTimeout"
            and "response_cleanup_failed:CancelledError" in observation.get("events", [])
        )
    if depth:
        common = common and "body_read_failed:CancelledError" in observation.get("events", [])
    if depth:
        common = (
            common
            and observation.get("connection_closed") is True
            and observation.get("client_socket_closed") is True
            and observation.get("pool_connections") == 0
            and observation.get("close_starts") == (2 if depth == 2 else 1)
        )
    return common and observation.get("next_operation", {}).get("result") == "success"


async def validate(args, data: dict, save) -> None:
    observations = []
    discovery = await sweep(response_case(observations), limits=LIMITS)
    data["discovery"] = {"report": discovery.to_dict(), "observations": observations}
    # Fixed logical contract; never select a schedule by its desired verdict.
    single = next((a for a in discovery.attempts if a.schedule.cancel_at == (1,)), None)
    double = next((a for a in discovery.attempts if a.schedule.cancel_at == (1, 2)), None)
    data["discovery_counts"] = dict(Counter(a.outcome.value for a in discovery.attempts))
    data["outside_selected_contract"] = [
        {
            "cancel_at": list(a.schedule.cancel_at),
            "outcome": a.outcome.value,
            "oracle": a.oracle.detail,
            "reasons": a.reasons,
        }
        for a in discovery.attempts
        if a.schedule.cancel_at not in ((), (1,), (1, 2))
    ]
    save()
    if single is None or double is None:
        data["errors"].append("selected logical schedules unavailable; no retry")
        return
    schedules = {"baseline": Schedule(), "single": single.schedule, "double": double.schedule}
    data["selected_schedules"] = {k: s.to_dict() for k, s in schedules.items()}
    data["json_round_trip_equal"] = all(
        Schedule.from_dict(json.loads(json.dumps(s.to_dict()))) == s for s in schedules.values()
    )
    if not data["json_round_trip_equal"]:
        data["errors"].append("schedule JSON round trip changed schedule")
    # Validate discovery controls and selected attempt independently of full sweep.
    for index, attempt in enumerate(discovery.attempts):
        if attempt.schedule.cancel_at not in ((), (1,), (1, 2)):
            continue
        depth = len(attempt.schedule.cancel_at)
        expected = args.expected if depth == 2 else "PASS"
        # Replay report wrapper gives the same per-attempt validation contract.
        view = Report([attempt], Coverage(complete=attempt.valid_schedule), {})
        if not acceptable(view, observations[index : index + 1], expected, depth):
            data["errors"].append(f"discovery contract failed: {attempt.schedule.cancel_at}")
    for repetition in range(1, args.repeats + 1):
        series = {"repetition": repetition, "runs": {}}
        data["series"].append(series)
        for label, original in schedules.items():
            # Each replay restores JSON and creates an entirely fresh factory state.
            schedule = Schedule.from_dict(json.loads(json.dumps(original.to_dict())))
            obs = []
            report = await replay(response_case(obs), schedule, limits=LIMITS)
            depth = len(schedule.cancel_at)
            expected = args.expected if label == "double" else "PASS"
            matched = acceptable(report, obs, expected, depth)
            if label == "baseline":
                matched = matched and report.attempts[0].trace == discovery.attempts[0].trace
            series["runs"][label] = {
                "report": report.to_dict(),
                "observations": obs,
                "contract_matched": matched,
            }
            if not matched:
                data["errors"].append(f"repetition {repetition} {label}: contract not matched")
            save()  # Includes unexpected/UNRESOLVED results; no retry loop.
    data["counts"] = {
        label: dict(Counter(s["runs"][label]["report"]["outcome"] for s in data["series"]))
        for label in schedules
    }
    # Check owned tasks before asyncio.run gets any opportunity to cancel leftovers.
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
    parser.add_argument("--expected", choices=["PASS", "FAIL"], required=True)
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args()
    data = {
        "interpreter": sys.executable,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "import_path": cancellens.__file__,
        "versions": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "module_sha256": installed_modules(),
        "harness_sha256": {
            p.name: digest(p) for p in (Path(__file__), Path(__file__).with_name("case.py"))
        },
        "expected_selected_double": args.expected,
        "repeats": args.repeats,
        "series": [],
        "errors": [],
    }

    def save() -> None:
        args.output.write_text(json.dumps(data, indent=2) + "\n")

    save()
    try:
        asyncio.run(validate(args, data, save))
    except Exception as exc:
        data["errors"].append("worker exception: " + type(exc).__name__)
        raise
    finally:
        save()
    print(
        f"selected double expected {args.expected}: {data.get('counts')}; errors={data['errors']}"
    )
    return int(bool(data["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
