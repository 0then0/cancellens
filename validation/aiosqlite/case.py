"""Real aiosqlite/SQLite, with only the connector and observation under test control."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any

import aiosqlite

from cancellens import Case, Completion, require


def connection_case(observations: list[dict[str, Any]]) -> Case[dict[str, Any]]:
    async def factory() -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        directory = tempfile.TemporaryDirectory(prefix="cancellens-sqlite-")
        entered = loop.create_future()
        release = threading.Event()
        state: dict[str, Any] = {
            "directory": directory,
            "release": release,
            "native": [],
        }

        def connector() -> sqlite3.Connection:
            # check_same_thread=False permits independent inspection AFTER join.
            native = sqlite3.connect(Path(directory.name) / "case.db", check_same_thread=False)
            state["native"].append(native)  # Keep a strong reference even in the broken case.
            loop.call_soon_threadsafe(entered.set_result, None)
            if not release.wait(timeout=5):
                native.close()
                raise TimeoutError("connector gate was not released")
            return native

        state["proxy"] = aiosqlite.Connection(connector, iter_chunk_size=64)

        async def controller() -> None:
            await entered
            # The observer has queued Task.cancel before a connector notification
            # can resume this controller. No delay/race against a sleep is needed.
            release.set()

        state["controller"] = asyncio.create_task(controller())
        return state

    async def operation(state: dict[str, Any]) -> None:
        # Upstream opening, ownership transfer, close and cancellation are untouched.
        async with state["proxy"]:
            pass

    async def oracle(state: dict[str, Any], completion: Completion) -> None:
        await state["controller"]
        worker = state["proxy"]._thread  # Observation hook for the immutable snapshots.
        await asyncio.to_thread(worker.join, 2)
        terminated = not worker.is_alive()
        native = state["native"][0] if state["native"] else None
        closed = False
        if terminated and native is not None:
            try:
                cursor = native.execute("SELECT 1")
            except sqlite3.ProgrammingError as exc:
                if "closed" not in str(exc).lower():
                    raise
                closed = True
            else:
                cursor.close()
        observations.append(
            {
                "operation": completion.state,
                "native_created": native is not None,
                "worker_terminated": terminated,
                "native_closed_before_teardown": closed,
            }
        )
        require(terminated, "aiosqlite worker terminated")
        require(native is not None, "connector created a native SQLite connection")
        require(closed, "native SQLite connection closed before harness teardown")

    async def teardown(state: dict[str, Any]) -> None:
        state["release"].set()
        controller = state["controller"]
        if not controller.done():
            controller.cancel()
        await asyncio.gather(controller, return_exceptions=True)
        proxy = state["proxy"]
        worker = proxy._thread
        if worker.is_alive():
            proxy.stop()
            await asyncio.to_thread(worker.join, 2)
        if worker.is_alive():
            raise RuntimeError("worker still alive during teardown")
        for native in state["native"]:
            native.close()  # Happens only AFTER the oracle outcome was recorded.
        state["directory"].cleanup()

    return Case(factory, operation, oracle, teardown)
