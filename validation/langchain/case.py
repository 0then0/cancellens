"""Actual BaseTool.ainvoke -> arun -> on_tool_error callback contract.

The upstream code is unchanged. The shielded variant protects only this local
handler's terminal write, with a finite AnyIO deadline in the same Task.
"""

from __future__ import annotations

import asyncio
from typing import Any

import anyio
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.tools import BaseTool

from cancellens import Case, Completion, require


class Recorder(AsyncCallbackHandler):
    # Inline handlers execute in the observed operation Task. Non-inline callback
    # Tasks are deliberately outside CancelLens's current observation scope.
    run_inline = True
    raise_error = True

    def __init__(self, shielded: bool) -> None:
        self.shielded = shielded
        self.events: list[str] = []
        self.callback_starts: list[str] = []

    async def on_tool_start(self, *args: Any, **kwargs: Any) -> None:
        self.events.append("start")

    async def terminal(self, name: str) -> None:
        self.callback_starts.append(name)
        if self.shielded:
            with anyio.fail_after(0.2, shield=True):
                await asyncio.sleep(0)
                self.events.append(name)
        else:
            await asyncio.sleep(0)
            self.events.append(name)

    async def on_tool_error(self, error: BaseException, *args: Any, **kwargs: Any) -> None:
        await self.terminal("error:" + type(error).__name__)

    async def on_tool_end(self, output: Any, *args: Any, **kwargs: Any) -> None:
        await self.terminal("end")


class LocalTool(BaseTool):
    name: str = "local_cancellation_probe"
    description: str = "A local tool requiring no provider or network."

    def _run(self, query: str) -> str:
        return query

    async def _arun(self, query: str) -> str:
        await asyncio.sleep(0)
        return query


def callback_case(observations: list[dict[str, Any]], *, shielded: bool = False) -> Case[Recorder]:
    async def factory() -> Recorder:
        recorder = Recorder(shielded)
        observations.append(
            {"events": recorder.events, "callback_starts": recorder.callback_starts}
        )
        return recorder

    async def operation(recorder: Recorder) -> None:
        await LocalTool().ainvoke({"query": "q"}, config={"callbacks": [recorder]})

    async def oracle(recorder: Recorder, completion: Completion) -> None:
        await asyncio.sleep(0)  # Must remain executable outside the operation scope.
        observation = observations[-1]
        observation["completion"] = completion.state
        observation["oracle_ran"] = True
        require(recorder.events[:1] == ["start"], "tool start event recorded")
        require(len(recorder.events) == 2, "started tool run records exactly one terminal event")
        # Exercise the same callback handler and a new call through the same real
        # tool path before harness teardown, rather than trusting no exception.
        await LocalTool().ainvoke({"query": "reuse"}, config={"callbacks": [recorder]})
        observation["reused"] = recorder.events[-2:] == ["start", "end"]
        require(observation["reused"], "same callback handler records the next tool run")

    async def teardown(recorder: Recorder) -> None:
        await asyncio.sleep(0)
        observations[-1]["teardown_ran"] = True

    return Case(factory, operation, oracle, teardown)
