"""Public results. Reports deliberately exclude operation values and exception messages."""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = 2


class Outcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNRESOLVED = "UNRESOLVED"
    ERROR = "ERROR"


class InvariantViolation(Exception):
    """A named contract violation, rather than an error executing the oracle."""


def require(condition: bool, invariant: str) -> None:
    """Raise a reportable violation. The explicit invariant text is included in JSON."""
    if not condition:
        raise InvariantViolation(invariant)


@dataclass(frozen=True)
class Frame:
    file: str
    function: str
    line: int
    offset: int
    code: str


@dataclass(frozen=True)
class Point:
    index: int
    kind: str
    stack: tuple[Frame, ...]


def interpreter_key() -> str:
    return f"{platform.python_implementation()}-{sys.version_info.major}.{sys.version_info.minor}"


@dataclass(frozen=True)
class Schedule:
    """Cancellation targets and the full required prefix, indexed from one."""

    cancel_at: tuple[int, ...] = ()
    prefix: tuple[Point, ...] = ()
    interpreter: str = field(default_factory=interpreter_key)
    cancellation_mode: str = "asyncio"
    backend: str = "asyncio"
    anyio_version: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in (1, SCHEMA_VERSION):
            raise ValueError("Unsupported schedule schema version")
        if self.cancellation_mode not in ("asyncio", "anyio"):
            raise ValueError("Unsupported cancellation mode")
        if self.backend != "asyncio":
            raise ValueError("Only the asyncio backend is supported")
        if self.schema_version == 1 and self.cancellation_mode != "asyncio":
            raise ValueError("Version 1 schedules only describe Task.cancel cancellation")
        if self.cancellation_mode == "anyio":
            if len(self.cancel_at) > 1:
                raise ValueError("AnyIO mode supports only one scope cancellation")
            if not self.anyio_version or self.anyio_version.split(".")[0] != "4":
                raise ValueError("AnyIO schedules require an AnyIO 4 version")
        elif self.anyio_version is not None:
            raise ValueError("An asyncio schedule cannot specify an AnyIO version")
        if len(self.cancel_at) > 2:
            raise ValueError("At most two cancellations are supported")
        if not self.cancel_at and self.prefix:
            raise ValueError("An uncancelled schedule cannot have a cancellation prefix")
        if (
            any(i < 1 for i in self.cancel_at)
            or tuple(sorted(set(self.cancel_at))) != self.cancel_at
        ):
            raise ValueError("Cancellation targets must be positive, unique, and increasing")
        if self.cancel_at and len(self.prefix) != self.cancel_at[-1]:
            raise ValueError(
                "A cancellation schedule needs its full prefix through the last target"
            )
        if any(p.index != i for i, p in enumerate(self.prefix, 1)):
            raise ValueError("Prefix indices must be contiguous from one")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Schedule:
        version = data.get("schema_version", 1)
        if version == SCHEMA_VERSION and any(
            key not in data for key in ("cancellation_mode", "backend", "anyio_version")
        ):
            raise ValueError(
                "Version 2 schedules require cancellation mode and environment metadata"
            )
        prefix = tuple(
            Point(p["index"], p["kind"], tuple(Frame(**f) for f in p["stack"]))
            for p in data["prefix"]
        )
        return cls(
            tuple(data["cancel_at"]),
            prefix,
            data["interpreter"],
            data.get("cancellation_mode", "asyncio"),
            data.get("backend", "asyncio"),
            data.get("anyio_version"),
            version,
        )


@dataclass
class Cancellation:
    point: int
    accepted: bool = False
    delivered: bool | None = False
    target_reached: bool = True
    prefix_matched: bool = True
    scope_cancel_called: bool | None = None
    exception_observed: bool = False
    scope_cancelled_caught: bool | None = None
    delivery_unknown_reason: str | None = None


@dataclass(frozen=True)
class Completion:
    """The operation's state when the oracle runs. Value/exception stay in memory only."""

    state: str
    value: Any = field(default=None, repr=False)
    exception: BaseException | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "exception_type": type(self.exception).__name__ if self.exception else None,
        }


@dataclass
class PhaseResult:
    outcome: Outcome | None = None
    detail: str | None = None


@dataclass
class Attempt:
    schedule: Schedule
    trace: list[Point] = field(default_factory=list)
    cancellations: list[Cancellation] = field(default_factory=list)
    completion: Completion = field(default_factory=lambda: Completion("not_started"))
    setup: PhaseResult = field(default_factory=PhaseResult)
    oracle: PhaseResult = field(default_factory=PhaseResult)
    teardown: PhaseResult = field(default_factory=PhaseResult)
    outcome: Outcome = Outcome.UNRESOLVED
    reasons: list[str] = field(default_factory=list)
    valid_schedule: bool = False
    trace_complete: bool = True
    cancellation_exceptions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule": self.schedule.to_dict(),
            "trace": [asdict(p) for p in self.trace],
            "cancellations": [asdict(c) for c in self.cancellations],
            "completion": self.completion.to_dict(),
            "setup": asdict(self.setup),
            "oracle": asdict(self.oracle),
            "teardown": asdict(self.teardown),
            "outcome": self.outcome.value,
            "reasons": self.reasons,
            "valid_schedule": self.valid_schedule,
            "trace_complete": self.trace_complete,
            "cancellation_exceptions": self.cancellation_exceptions,
        }


@dataclass
class Coverage:
    baseline_points: int = 0
    single_planned: int = 0
    single_valid: int = 0
    double_planned: int = 0
    double_valid: int = 0
    attempts_run: int = 0
    complete: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class Report:
    attempts: list[Attempt]
    coverage: Coverage
    limits: dict[str, Any]
    mode: str = "sweep"
    cancellation_mode: str = "asyncio"
    backend: str = "asyncio"
    anyio_version: str | None = None

    @property
    def outcome(self) -> Outcome:
        outcomes = {a.outcome for a in self.attempts}
        # An independently established violation survives infrastructure failures.
        if Outcome.FAIL in outcomes:
            return Outcome.FAIL
        if Outcome.ERROR in outcomes:
            return Outcome.ERROR
        if Outcome.UNRESOLVED in outcomes or not self.coverage.complete:
            return Outcome.UNRESOLVED
        return Outcome.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "package_version": "0.1.0",
            "interpreter": platform.python_version(),
            "implementation": platform.python_implementation(),
            "mode": self.mode,
            "cancellation_mode": self.cancellation_mode,
            "backend": self.backend,
            "anyio_version": self.anyio_version,
            "outcome": self.outcome.value,
            "coverage": asdict(self.coverage),
            "limits": self.limits,
            "attempts": [a.to_dict() for a in self.attempts],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def assert_passed(self) -> None:
        if self.outcome is Outcome.PASS:
            return
        failed = next((a for a in self.attempts if a.outcome is Outcome.FAIL), None)
        issue = failed or next((a for a in self.attempts if a.outcome is not Outcome.PASS), None)
        detail = ""
        if issue:
            details = [
                f"{name}: {phase.detail}"
                for name, phase in (
                    ("setup", issue.setup),
                    ("oracle", issue.oracle),
                    ("teardown", issue.teardown),
                )
                if phase.detail
            ]
            detail = "; ".join(details + issue.reasons)
        if not detail:
            detail = "; ".join(self.coverage.reasons)
        schedule = issue.schedule.cancel_at if issue else ()
        raise AssertionError(
            f"CancelLens {self.outcome.value}: cancel_at={schedule}: {detail}; "
            f"coverage single={self.coverage.single_valid}/{self.coverage.single_planned}, "
            f"double={self.coverage.double_valid}/{self.coverage.double_planned}, "
            f"complete={self.coverage.complete}"
        )
