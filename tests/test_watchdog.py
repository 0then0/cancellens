"""Exercise deadlines and interruption with real, disposable subprocesses."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from validation.watchdog import run

ROOT = Path(__file__).resolve().parents[1]


def wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"subprocess did not create {path.name}")
        time.sleep(0.01)


def assert_socket_released(port: int) -> None:
    deadline = time.monotonic() + 2
    while True:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("worker still owns its listening socket") from None
        time.sleep(0.01)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_command_status_and_working_directory(tmp_path, exit_code):
    command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path('ran').touch(); exit({exit_code})",
    ]
    assert run(command, timeout=5, cwd=str(tmp_path)) == exit_code
    assert (tmp_path / "ran").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group ownership")
@pytest.mark.parametrize("interrupt", [None, signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("ignore_term", [False, True])
def test_outer_watchdog_stops_nested_worker(tmp_path, interrupt, ignore_term):
    """The worker's bound is long; only the outer supervisor can stop this run."""
    resource = tmp_path / "resource.json"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, os, pathlib, signal, socket, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + "sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen()\n"
        + "data = {'port': sock.getsockname()[1], 'group': os.getpgrp()}\n"
        + f"ready = pathlib.Path({str(resource)!r})\n"
        + "temporary = ready.with_suffix('.tmp')\n"
        + "temporary.write_text(json.dumps(data)); temporary.replace(ready)\n"
        + "time.sleep(30)\n"
    )
    manager = tmp_path / "manager.py"
    manager.write_text(
        f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
        "from validation.watchdog import run\n"
        f"raise SystemExit(run({[sys.executable, str(worker)]!r}, timeout=20, new_session=False))\n"
    )
    # Arm the outer deadline only after the worker owns its socket. Startup speed
    # does not select whether this test reaches the process-group failure window.
    armed = tmp_path / "armed"
    outer_timeout = 0.2 if interrupt is None else 20
    outer = tmp_path / "outer.py"
    outer.write_text(
        f"import pathlib, subprocess, sys, time; sys.path.insert(0, {str(ROOT)!r})\n"
        "from validation.watchdog import run\n"
        "original_wait = subprocess.Popen.wait\n"
        "def wait(process, timeout=None):\n"
        f"    if timeout == {outer_timeout!r}:\n"
        "        bound = time.monotonic() + 5\n"
        f"        while not pathlib.Path({str(resource)!r}).exists():\n"
        "            if time.monotonic() >= bound: raise TimeoutError('worker startup')\n"
        "            time.sleep(0.01)\n"
        f"        pathlib.Path({str(armed)!r}).touch()\n"
        "    return original_wait(process, timeout=timeout)\n"
        "subprocess.Popen.wait = wait\n"
        f"raise SystemExit(run({[sys.executable, str(manager)]!r}, timeout={outer_timeout!r}))\n"
    )
    process = subprocess.Popen([sys.executable, str(outer)])
    data = None
    try:
        wait_for_file(armed)
        data = json.loads(resource.read_text())
        if interrupt is not None:
            # SIGTERM is sent to the owned group, as the outer watchdog does;
            # SIGINT interrupts the supervisor's wait() (the Ctrl-C path).
            if interrupt == signal.SIGTERM:
                os.killpg(data["group"], interrupt)
            else:
                process.send_signal(interrupt)
        code = process.wait(timeout=5)
        if interrupt is None:
            assert code == 124
        else:
            assert code != 0
        assert_socket_released(data["port"])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if data is None and resource.exists():
            data = json.loads(resource.read_text())
        if data is not None:
            try:
                os.killpg(data["group"], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_individual_worker_timeout_preserves_manager(tmp_path):
    # This test process is the manager. Sharing its process group must not cause
    # the per-worker deadline to signal the manager or unrelated sibling work.
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    assert run(command, timeout=0.2, cwd=str(tmp_path), new_session=False) == 124
