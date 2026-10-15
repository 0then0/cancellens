"""External wall-clock limit; unlike an asyncio deadline, can kill a blocked loop.

Usage: python validation/watchdog.py --timeout 90 -- python -m pytest
On POSIX kills the child's process group; on Windows kills the immediate child.
Nested validation workers use new_session=False to remain in the outer group.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from collections.abc import Sequence


def _signal(process: subprocess.Popen[bytes], *, grouped: bool, kill: bool) -> None:
    try:
        if grouped:
            os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
        elif kill:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        pass  # The process or its group exited between wait() and the signal.


def run(
    command: Sequence[str],
    *,
    timeout: float,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    new_session: bool = True,
) -> int:
    """Bound a command; nested workers must inherit the enclosing process group."""
    grouped = os.name == "posix" and new_session
    process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=grouped)
    try:
        code = process.wait(timeout=timeout)
        if code < 0 and grouped:
            _signal(process, grouped=True, kill=True)
        return code
    except subprocess.TimeoutExpired:
        print(f"watchdog: external deadline exceeded ({timeout}s)", file=sys.stderr, flush=True)
        _signal(process, grouped=grouped, kill=False)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        # A group leader can exit on SIGTERM while another group member ignores
        # it. Reap the whole owned group, even when wait() already returned.
        _signal(process, grouped=grouped, kill=True)
        process.wait()
        return 124
    except BaseException:
        _signal(process, grouped=grouped, kill=True)
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
