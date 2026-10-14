"""Transparent delegation of the coroutine protocol to a real asyncio Task.

Nothing here awaits/re-awaits a yielded Future or changes its blocking flag.
Only exceptions actually supplied by Task are forwarded with throw().
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import marshal
import types
from collections.abc import Callable, Coroutine, Generator
from functools import lru_cache
from pathlib import Path
from typing import Any

from ._model import Attempt, Cancellation, Frame, Point


class InstrumentationError(Exception):
    pass


def _portable_code(code: types.CodeType) -> types.CodeType:
    return code.replace(
        co_filename="",
        co_consts=tuple(
            _portable_code(value) if isinstance(value, types.CodeType) else value
            for value in code.co_consts
        ),
    )


@lru_cache(maxsize=512)
def _code_key(code: types.CodeType) -> str:
    # No absolute installation paths in attribution, including nested code objects.
    return hashlib.sha256(marshal.dumps(_portable_code(code))).hexdigest()[:16]


def describe(coro: Coroutine[Any, Any, Any], yielded: object, index: int) -> Point:
    frames = []
    node: Any = coro
    seen: set[int] = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        frame = getattr(node, "cr_frame", None) or getattr(node, "gi_frame", None)
        if frame is not None:
            code = frame.f_code
            frames.append(
                Frame(
                    Path(code.co_filename).name,
                    getattr(code, "co_qualname", code.co_name),
                    frame.f_lineno,
                    frame.f_lasti,
                    _code_key(code),
                )
            )
        node = getattr(node, "cr_await", None) or getattr(node, "gi_yieldfrom", None)
    kind = (
        "bare_yield" if yielded is None else f"{type(yielded).__module__}.{type(yielded).__name__}"
    )
    return Point(index, kind, tuple(frames))


class Observer:
    def __init__(self, attempt: Attempt, max_points: int) -> None:
        self.attempt = attempt
        self.max_points = max_points
        self.count = 0
        self.active = True
        self.mismatch = False
        self.pending: list[tuple[object, Cancellation]] = []
        self.handles: list[asyncio.Handle] = []

    def point(self, coro: Coroutine[Any, Any, Any], yielded: object) -> None:
        if not self.active:
            return
        self.count += 1
        if self.count > self.max_points:
            self.attempt.trace_complete = False
            if "suspension point budget exhausted" not in self.attempt.reasons:
                self.attempt.reasons.append("suspension point budget exhausted")
            return
        point = describe(coro, yielded, self.count)
        self.attempt.trace.append(point)
        schedule = self.attempt.schedule
        if self.count <= len(schedule.prefix) and point != schedule.prefix[self.count - 1]:
            self.mismatch = True
            self.attempt.reasons.append(
                f"trace diverged at point {self.count}; cancellation withheld"
            )
        if self.mismatch or self.count not in schedule.cancel_at:
            return
        if self.pending and not self.pending[-1][1].delivered:
            self.attempt.reasons.append(
                "previous cancellation was not delivered before next target"
            )
            return
        task = asyncio.current_task()
        if task is None:
            raise InstrumentationError("observation requires an asyncio Task")
        record = Cancellation(self.count)
        token = object()
        self.pending.append((token, record))
        self.attempt.cancellations.append(record)

        def cancel() -> None:
            record.accepted = task.cancel(token)

        # Task first registers the original yielded waiter. This callback then requests
        # cancellation before the resumption callback of a bare yield can run.
        self.handles.append(asyncio.get_running_loop().call_soon(cancel))

    def exception(self, error: BaseException) -> None:
        if not self.active:
            return
        if isinstance(error, asyncio.CancelledError):
            matched = False
            for token, record in self.pending:
                if error.args and error.args[0] is token:
                    record.delivered = True
                    matched = True
            if self.pending and not matched:
                self.attempt.reasons.append(
                    "CancelledError reached caller without matching request token; "
                    "delivery attribution is ambiguous"
                )

    def close(self) -> None:
        self.active = False
        for handle in self.handles:
            handle.cancel()


@types.coroutine
def delegate(
    coro: Coroutine[Any, Any, Any],
    on_point: Callable[[Coroutine[Any, Any, Any], object], None],
    on_exception: Callable[[BaseException], None],
) -> Generator[Any, Any, Any]:
    if not inspect.iscoroutine(coro):
        raise InstrumentationError("operation must return a native Python coroutine")
    value: Any = None
    error: BaseException | None = None
    observation_failed = False
    try:
        while True:
            try:
                yielded = coro.throw(error) if error is not None else coro.send(value)
            except StopIteration as stop:
                return stop.value
            try:
                on_point(coro, yielded)
            except Exception as exc:
                observation_failed = True
                raise InstrumentationError("observation failed") from exc
            try:
                value = yield yielded
                error = None
            except BaseException as exc:
                on_exception(exc)
                error = exc
                value = None
    finally:
        try:
            coro.close()
        except Exception:
            if not observation_failed:
                raise
