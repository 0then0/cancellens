"""Native HTTP/1.1 body-read cancellation followed by contended-lock cleanup.

The server body gate, real state lock, and non-suspending trace hooks control ordering.
Upstream close/release code is unchanged; no cleanup await is added.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from cancellens import Case, Completion, require


class LoopbackServer:
    def __init__(self) -> None:
        self.body = asyncio.Event()
        self.tasks: list[asyncio.Task[Any]] = []
        self.writers: list[asyncio.StreamWriter] = []
        self.sockets: list[Any] = []
        self.errors: list[str] = []
        self.requests: list[str] = []

    def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        self.sockets.append(writer.get_extra_info("socket"))
        self.tasks.append(asyncio.create_task(self.serve(reader, writer)))

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    headers = await reader.readuntil(b"\r\n\r\n")
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        raise RuntimeError("partial request headers") from exc
                    break
                path = headers.split(b" ")[1].decode("ascii")
                self.requests.append(path)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\n")
                await writer.drain()
                if path == "/case":
                    await self.body.wait()
                elif path != "/next":
                    raise RuntimeError("unexpected request path")
                writer.write(b"x")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            # Expected when cancellation closes a client with unread body bytes.
            pass
        except Exception as exc:
            self.errors.append(type(exc).__name__)
        finally:
            writer.close()
            await wait_writer_closed(writer)


async def wait_writer_closed(writer: asyncio.StreamWriter) -> None:
    try:
        await writer.wait_closed()
    except (ConnectionResetError, BrokenPipeError):
        pass  # TCP reset during cancellation is compatible with a closed socket.


async def dispose(state: dict[str, Any]) -> None:
    server = state["server"]
    server.body.set()
    controller = state.get("controller")
    if controller is not None:
        if not controller.done():
            controller.cancel()  # Teardown only; never sends the two operation cancels.
        await asyncio.gather(controller, return_exceptions=True)
    if "client" in state:
        await state["client"].aclose()
    if "listener" in state:
        state["listener"].close()
        await state["listener"].wait_closed()
    for writer in server.writers:
        writer.close()
    await asyncio.gather(*(wait_writer_closed(w) for w in server.writers))
    if server.tasks:
        done, pending = await asyncio.wait(server.tasks, timeout=1)
        for task in pending:
            task.cancel()
        await asyncio.gather(*server.tasks, return_exceptions=True)
        if pending:
            raise RuntimeError("server tasks required forced cancellation")
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                raise RuntimeError("server task raised during cleanup") from task.exception()


def response_case(observations: list[dict[str, Any]]) -> Case[dict[str, Any]]:
    async def factory() -> dict[str, Any]:
        state: dict[str, Any] = {
            "server": LoopbackServer(),
            "reading": asyncio.Event(),
            "closing": asyncio.Event(),
            "locked": asyncio.Event(),
            "events": [],
            "close_starts": 0,
        }
        state["observation"] = {"events": state["events"]}
        observations.append(state["observation"])

        async def trace(name: str, info: dict[str, Any]) -> None:
            # Synchronous event notifications; this callback itself never suspends.
            if name == "http11.receive_response_body.started":
                state["events"].append("body_read_started")
                state["reading"].set()
            if name == "http11.receive_response_body.failed":
                state["events"].append("body_read_failed:" + type(info["exception"]).__name__)
            if name == "http11.response_closed.started":
                state["close_starts"] += 1
                state["events"].append("response_cleanup_started")
                state["closing"].set()
            if name == "http11.response_closed.failed":
                state["observation"]["cleanup_failed_while_lock_held"] = state[
                    "connection"
                ]._state_lock._anyio_lock.locked()
                state["events"].append(
                    "response_cleanup_failed:" + type(info["exception"]).__name__
                )
            if name == "http11.response_closed.complete":
                state["events"].append("response_cleanup_complete")

        try:
            state["listener"] = await asyncio.start_server(state["server"].accept, "127.0.0.1", 0)
            state["listener_sockets"] = list(state["listener"].sockets)
            port = state["listener_sockets"][0].getsockname()[1]
            state["url"] = f"http://127.0.0.1:{port}"
            state["client"] = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                timeout=httpx.Timeout(2, pool=0.2),
                trust_env=False,
            )
            client = state["client"]
            request = client.build_request(
                "GET", state["url"] + "/case", extensions={"trace": trace}
            )
            state["response"] = await client.send(request, stream=True)
            state["pool"] = client._transport._pool
            state["connection"] = state["pool"].connections[0]._connection
            state["socket"] = state["connection"]._network_stream.get_extra_info("socket")
            # Private observation only; HTTPX -> PoolByteStream -> HTTP11 byte stream.
            state["byte_stream"] = state["response"].stream._stream._httpcore_stream._stream

            async def controller() -> None:
                # The task that acquires the actual AnyIO-backed lock releases it.
                async with state["connection"]._state_lock:
                    state["events"].append("state_lock_held")
                    state["locked"].set()
                    await state["reading"].wait()
                    task = state["operation_task"]
                    waiter = task._fut_waiter
                    protocol = state["connection"]._network_stream._stream._protocol
                    state["observation"]["read_waiter_is_native"] = (
                        waiter is not None and waiter in protocol.read_event._waiters
                    )
                    # The synchronous read-start trace wakes us, but cannot run us
                    # until the operation yields its native read Future. Server
                    # body release is queued after CancelLens's cancellation.
                    asyncio.get_running_loop().call_soon(state["server"].body.set)
                    await state["closing"].wait()
                    state["observation"]["cleanup_waiter_is_state_lock"] = any(
                        owner is task and future is task._fut_waiter
                        for owner, future in state["connection"]._state_lock._anyio_lock._waiters
                    )
                    # One controller-only ready-queue handoff keeps the lock held
                    # through delivery: the observer's cancel callback is already
                    # queued. It cancels the waiter and queues the operation before
                    # this handoff can queue the controller's own resumption.
                    # No timer, cancellation request or await is added to cleanup.
                    handoff = asyncio.get_running_loop().create_future()
                    asyncio.get_running_loop().call_soon(handoff.set_result, None)
                    await handoff
                    state["events"].append("state_lock_released")

            state["controller"] = asyncio.create_task(controller())
            await state["locked"].wait()
            return state
        except BaseException:
            await dispose(state)
            raise

    async def operation(state: dict[str, Any]) -> None:
        state["operation_task"] = asyncio.current_task()
        try:
            async for _ in state["response"].aiter_raw():
                pass
        finally:
            await state["response"].aclose()

    async def oracle(state: dict[str, Any], completion: Completion) -> None:
        await state["controller"]
        connection, pool = state["connection"], state["pool"]
        observation = state["observation"]
        observation.update(
            {
                "operation": completion.state,
                "close_starts": state["close_starts"],
                "byte_stream_closed": state["byte_stream"]._closed,
                "connection_closed": connection.is_closed(),
                "connection_idle": connection.is_idle(),
                "connection_available": connection.is_available(),
                "client_socket_closed": state["socket"].fileno() == -1,
                "pool_connections": len(pool.connections),
                "pool_requests": len(pool._requests),
                "pool_active": sum(not c.is_idle() and not c.is_closed() for c in pool.connections),
            }
        )
        stranded = (
            observation["pool_connections"] == 1
            and observation["pool_active"] == 1
            and not observation["connection_available"]
        )
        # Always exercise the SAME pool before client/pool closure, even for FAIL.
        try:
            response = await state["client"].get(state["url"] + "/next")
            observation["next_operation"] = {"result": "success", "status": response.status_code}
            if response.status_code != 200 or response.content != b"x":
                raise RuntimeError("unexpected next response")
        except httpx.PoolTimeout:
            observation["next_operation"] = {"result": "PoolTimeout"}
            if not stranded:
                raise RuntimeError("pool timeout without observed stranded slot") from None
        observation["server_requests"] = list(state["server"].requests)
        observation["server_errors"] = list(state["server"].errors)
        if state["server"].errors:
            raise RuntimeError("server error")
        if completion.state == "raised":
            raise RuntimeError("operation raised unexpectedly") from completion.exception
        require(not stranded, "HTTP/1.1 cleanup releases the active slot before harness teardown")
        require(observation["pool_requests"] == 0, "response removed from pool bookkeeping")
        require(
            observation["next_operation"]["result"] == "success",
            "same one-slot pool serves next request",
        )

    async def teardown(state: dict[str, Any]) -> None:
        observation = state["observation"]
        try:
            await dispose(state)
        finally:
            server = state["server"]
            observation["cleanup"] = {
                "controller_done": state["controller"].done(),
                "server_tasks_done": all(t.done() for t in server.tasks),
                "server_writers_closed": all(w.is_closing() for w in server.writers),
                "listener_closed": not state["listener"].is_serving(),
                "server_sockets_closed": all(s.fileno() == -1 for s in server.sockets),
                "listener_sockets_closed": all(s.fileno() == -1 for s in state["listener_sockets"]),
                "client_socket_closed": state["socket"].fileno() == -1,
                "pool_connections": len(state["pool"].connections),
                "server_errors": list(server.errors),
            }
        if server.errors:
            raise RuntimeError("server error during cleanup")

    return Case(factory, operation, oracle, teardown)
