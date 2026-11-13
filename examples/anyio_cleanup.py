"""One scope request can interrupt an awaited cleanup more than once.

Install ``cancellens[anyio]``. These synthetic contracts complement the real
LangChain callback validation; they are not a reproduction of that integration.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass, field

import anyio

from cancellens import Case, Completion, Limits, require, sweep


@dataclass
class ResourceState:
    pool: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    released: bool = False
    reused: bool = False
    teardown_done: bool = False


def resource_case(
    *, shielded: bool, observations: list[ResourceState] | None = None
) -> Case[ResourceState]:
    async def factory() -> ResourceState:
        state = ResourceState()
        if observations is not None:
            observations.append(state)
        return state

    async def operation(state: ResourceState) -> None:
        await state.pool.acquire()
        try:
            await asyncio.sleep(0.001)
        except BaseException:
            with anyio.fail_after(0.1, shield=True) if shielded else nullcontext():
                await asyncio.sleep(0.001)
                state.pool.release()
                state.released = True
            raise
        else:
            state.pool.release()
            state.released = True

    async def oracle(state: ResourceState, completion: Completion) -> None:
        # Both observations happen before emergency teardown can repair a leak.
        available = not state.pool.locked()
        if available:
            await state.pool.acquire()
            state.pool.release()
            state.reused = True
        require(state.released, "awaited resource cleanup completed")
        require(available and state.reused, "the same resource can be acquired again")

    async def teardown(state: ResourceState) -> None:
        await asyncio.sleep(0)
        if state.pool.locked():
            state.pool.release()
        state.teardown_done = True

    return Case(factory, operation, oracle, teardown)


@dataclass
class CallbackState:
    events: list[str] = field(default_factory=list)
    teardown_done: bool = False


def callback_case(
    *, shielded: bool, observations: list[CallbackState] | None = None
) -> Case[CallbackState]:
    async def factory() -> CallbackState:
        state = CallbackState()
        if observations is not None:
            observations.append(state)
        return state

    async def terminal(state: CallbackState) -> None:
        await asyncio.sleep(0.001)
        state.events.append("terminal")

    async def operation(state: CallbackState) -> None:
        state.events.append("start")
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            with anyio.fail_after(0.1, shield=True) if shielded else nullcontext():
                await terminal(state)
            raise
        else:
            state.events.append("terminal")

    async def oracle(state: CallbackState, completion: Completion) -> None:
        await asyncio.sleep(0)
        require(state.events == ["start", "terminal"], "each start has a terminal callback event")

    async def teardown(state: CallbackState) -> None:
        await asyncio.sleep(0)
        state.teardown_done = True

    return Case(factory, operation, oracle, teardown)


if __name__ == "__main__":
    for case_factory in (resource_case, callback_case):
        for mode in ("asyncio", "anyio"):
            for shielded in (False, True):
                report = asyncio.run(
                    sweep(
                        case_factory(shielded=shielded),
                        cancellation_mode=mode,
                        limits=Limits(max_depth=1),
                    )
                )
                print(f"{case_factory.__name__} {mode=} {shielded=}: {report.outcome.value}")
