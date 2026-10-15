"""Download verified immutable snapshots, install alongside the built wheel, validate.

Does not clone, edit or use an upstream checkout. All environments are disposable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "validation"))
from watchdog import run as watchdog  # noqa: E402

SNAPSHOTS = {
    "base": ("9b127cecc7de843bf4f343b6da63b641866533fb", "FAIL"),
    "proposed_fix": ("005b3aa77a67606d2264996aeff4f69448fb79fc", "PASS"),
}


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "CancelLens-acceptance/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def extract(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            resolved = (destination / member.name).resolve()
            if (
                not resolved.is_relative_to(destination.resolve())
                or member.issym()
                or member.islnk()
            ):
                raise ValueError("unsafe snapshot archive member")
        # Compatibility with 3.10/3.11, with path/link validation above.
        tar.extractall(
            destination, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {})
        )
    return next(p for p in destination.iterdir() if p.is_dir())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "validation/aiosqlite/evidence.json")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    if not wheel.is_file() or args.repeats < 1:
        parser.error("existing wheel and positive repeats required")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.setdefault("UV_CACHE_DIR", str(Path(tempfile.gettempdir()) / "cancellens-uv-cache"))
    result = {
        "acceptance_schema_version": 1,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "snapshots": {},
    }
    cores = {}
    for label, (commit, expected) in SNAPSHOTS.items():
        metadata = json.loads(
            download(f"https://api.github.com/repos/omnilib/aiosqlite/commits/{commit}")
        )
        assert metadata["sha"] == commit
        blob = download(f"https://codeload.github.com/omnilib/aiosqlite/tar.gz/{commit}")
        with tempfile.TemporaryDirectory(prefix=f"cancellens-{label}-") as temporary:
            work = Path(temporary)
            archive = work / "snapshot.tar.gz"
            archive.write_bytes(blob)
            source = extract(archive, work / "source")
            core = source / "aiosqlite/core.py"
            cores[label] = core.read_text()
            venv = work / "venv"
            subprocess.run(["uv", "venv", "--python", args.python, str(venv)], env=env, check=True)
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--build-constraint",
                    str(ROOT / "validation/aiosqlite/build-constraints.txt"),
                    str(wheel),
                    str(source),
                ],
                env=env,
                check=True,
            )
            output = work / "result.json"
            command = [
                str(python),
                str(ROOT / "validation/aiosqlite/worker.py"),
                "--expected",
                expected,
                "--repeats",
                str(args.repeats),
                "--output",
                str(output),
            ]
            code = watchdog(
                command,
                timeout=max(60, args.repeats * 20),
                cwd=str(work),
                env=env,
                new_session=False,
            )
            if code:
                raise RuntimeError(f"{label} acceptance exited with {code}")
            data = json.loads(output.read_text())
            assert data["installed_core_sha256"] == hashlib.sha256(core.read_bytes()).hexdigest()
            data.update(
                {
                    "commit": commit,
                    "commit_url": f"https://github.com/omnilib/aiosqlite/commit/{commit}",
                    "archive_sha256": hashlib.sha256(blob).hexdigest(),
                    "commit_message": metadata["commit"]["message"].splitlines()[0],
                    "parents": [p["sha"] for p in metadata["parents"]],
                }
            )
            result["snapshots"][label] = data
    # Verify the mechanism in the actual selected fix, rather than relying on PR text.
    assert "self._connection = await future" in cores["base"]
    assert "self._connection = self._connector()" in cores["proposed_fix"]
    assert "_call_soon_threadsafe" in cores["proposed_fix"]
    result["verified_fix"] = [
        "worker assigns native connection before Future handoff",
        "cancellation no longer clears worker ownership",
        "worker releases the executed callable",
        "result delivery tolerates a closed event loop",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"evidence: {args.output}")


if __name__ == "__main__":
    main()
