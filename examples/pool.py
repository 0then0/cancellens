"""A lost semaphore slot is observable before teardown repairs it."""

import asyncio

from cancellens import Case, Completion, Limits, require, sweep


def pool_case(*, broken: bool = False) -> Case[asyncio.Semaphore]:
    async def factory() -> asyncio.Semaphore:
        return asyncio.Semaphore(1)

    async def operation(pool: asyncio.Semaphore) -> None:
        await pool.acquire()  # Available slot: no suspension point here.
        if broken:
            await asyncio.sleep(0)
            pool.release()
        else:
            try:
                await asyncio.sleep(0)
            finally:
                pool.release()

    async def oracle(pool: asyncio.Semaphore, completion: Completion) -> None:
        require(completion.state in ("returned", "cancelled"), "expected operation termination")
        # Check and use the same resource before harness teardown.
        require(not pool.locked(), "pool slot returned; next operation can acquire")
        await pool.acquire()
        pool.release()

    async def teardown(pool: asyncio.Semaphore) -> None:
        if pool.locked():
            pool.release()

    return Case(factory, operation, oracle, teardown)


def test_pool() -> None:
    asyncio.run(sweep(pool_case())).assert_passed()


if __name__ == "__main__":
    for broken in (False, True):
        report = asyncio.run(sweep(pool_case(broken=broken), limits=Limits(max_depth=1)))
        print(f"broken={broken}: {report.outcome.value}")
