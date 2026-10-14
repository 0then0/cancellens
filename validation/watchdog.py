"""External wall-clock limit; unlike an asyncio deadline, can kill a blocked loop.

Usage: python validation/watchdog.py --timeout 90 -- python -m pytest
On POSIX kills the child's process group; on Windows kills the immediate child.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from collections.abc import Sequence


def run(
    command: Sequence[str],
    *,
    timeout: float,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> int:
    process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=os.name == "posix")
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"watchdog: external deadline exceeded ({timeout}s)", file=sys.stderr, flush=True)
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
        return 124
    except BaseException:
        if process.poll() is None:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.timeout <= 0:
        parser.error("positive timeout and command required")
    return run(command, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
