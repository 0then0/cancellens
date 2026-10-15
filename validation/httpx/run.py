"""Published CancelLens wheel + immutable httpcore snapshots, disposable native runs."""

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
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "validation"))
from watchdog import run as watchdog  # noqa: E402

SNAPSHOTS = {
    "base": ("10a658221deb38a4c5b16db55ab554b0bf731707", "FAIL"),
    "proposed_fix": ("1fb0ecec6d38da241c5aca08777195aae3830d1d", "PASS"),
}
WHEEL_NAME = "cancellens-0.1.0-py3-none-any.whl"
WHEEL_SHA256 = "e80118f7c3f232ebb3772731ed6a3eb0b91ee1d75fdc2fb31ad25e18182adabd"


def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "CancelLens-acceptance/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def extract(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if (
                not (destination / member.name).resolve().is_relative_to(destination.resolve())
                or member.issym()
                or member.islnk()
            ):
                raise ValueError("unsafe snapshot archive member")
        tar.extractall(
            destination, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {})
        )
    directories = [p for p in destination.iterdir() if p.is_dir()]
    if len(directories) != 1:
        raise ValueError("snapshot requires one root directory")
    return directories[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel", type=Path, help="optional cached wheel; must match published hash"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "validation/httpx/evidence.json")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args()
    if args.repeats < 10:
        parser.error("at least ten repetitions required; no retries or discarded outcomes")
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(name, None)
    env.setdefault("UV_CACHE_DIR", str(Path(tempfile.gettempdir()) / "cancellens-uv-cache"))
    result = {
        "acceptance_schema_version": 1,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "contract": (
            "body-read cancellation then cancellation of contended HTTP/1.1 state-lock cleanup"
        ),
        "snapshots": {},
        "errors": [],
    }

    def save() -> None:
        args.output.write_text(json.dumps(result, indent=2) + "\n")

    save()
    try:
        pypi = json.loads(download("https://pypi.org/pypi/cancellens/0.1.0/json"))
        published = next(f for f in pypi["urls"] if f["filename"] == WHEEL_NAME)
        blob = args.wheel.read_bytes() if args.wheel else download(published["url"])
        if sha256(blob) != WHEEL_SHA256 or published["digests"]["sha256"] != WHEEL_SHA256:
            raise ValueError("CancelLens wheel differs from pinned published artifact")
        result["artifact"] = {
            "filename": WHEEL_NAME,
            "sha256": sha256(blob),
            "url": published["url"],
            "upload_time_utc": published["upload_time_iso_8601"],
            "metadata_url": "https://pypi.org/pypi/cancellens/0.1.0/json",
        }
        pr = json.loads(download("https://api.github.com/repos/encode/httpcore/pulls/1113"))
        result["upstream_pr"] = {
            "url": pr["html_url"],
            "state": pr["state"],
            "merged": pr["merged"],
            "merged_at": pr["merged_at"],
            "base_sha": pr["base"]["sha"],
            "head_sha": pr["head"]["sha"],
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        base, fixed = (v[0] for v in SNAPSHOTS.values())
        compare = json.loads(
            download(f"https://api.github.com/repos/encode/httpcore/compare/{base}...{fixed}")
        )
        result["upstream_diff"] = {
            "url": compare["html_url"],
            "merge_base_sha": compare["merge_base_commit"]["sha"],
            "commits": [c["sha"] for c in compare["commits"]],
            "files": [
                {"path": f["filename"], "status": f["status"], "patch": f.get("patch")}
                for f in compare["files"]
            ],
        }
        if result["upstream_diff"]["merge_base_sha"] != base:
            raise ValueError("snapshots are not based on the specified base")
        save()
        with tempfile.TemporaryDirectory(prefix="cancellens-httpx-") as temporary:
            work = Path(temporary)
            wheel = work / WHEEL_NAME
            wheel.write_bytes(blob)
            with zipfile.ZipFile(wheel) as archive:
                wheel_modules = {
                    p.removeprefix("cancellens/"): sha256(archive.read(p))
                    for p in archive.namelist()
                    if p.startswith("cancellens/") and p.endswith(".py")
                }
            for label, (commit, expected) in SNAPSHOTS.items():
                entry: dict = {"commit": commit, "expected_selected_double": expected}
                result["snapshots"][label] = entry
                save()
                code = None
                try:
                    metadata = json.loads(
                        download(f"https://api.github.com/repos/encode/httpcore/commits/{commit}")
                    )
                    if metadata["sha"] != commit:
                        raise ValueError("commit identity not verified")
                    entry.update(
                        {
                            "commit_url": metadata["html_url"],
                            "commit_message": metadata["commit"]["message"].splitlines()[0],
                            "parents": [p["sha"] for p in metadata["parents"]],
                        }
                    )
                    snapshot = download(
                        f"https://codeload.github.com/encode/httpcore/tar.gz/{commit}"
                    )
                    case_work = work / label
                    case_work.mkdir()
                    archive_path = case_work / "snapshot.tar.gz"
                    archive_path.write_bytes(snapshot)
                    source = extract(archive_path, case_work / "source")
                    entry["archive_sha256"] = sha256(snapshot)
                    upstream_modules = {
                        str(p.relative_to(source / "httpcore")): sha256(p.read_bytes())
                        for p in sorted((source / "httpcore").rglob("*.py"))
                    }
                    entry["upstream_test_sha256"] = {
                        name: sha256((source / name).read_bytes())
                        for name in ("tests/test_cancellations.py", "tests/_async/test_http11.py")
                    }
                    venv = case_work / "venv"
                    subprocess.run(
                        ["uv", "venv", "--python", args.python, str(venv)], env=env, check=True
                    )
                    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                    subprocess.run(
                        [
                            "uv",
                            "pip",
                            "install",
                            "--python",
                            str(python),
                            "--build-constraint",
                            str(ROOT / "validation/httpx/build-constraints.txt"),
                            "-r",
                            str(ROOT / "validation/httpx/requirements.txt"),
                            str(wheel),
                            str(source),
                        ],
                        env=env,
                        check=True,
                    )
                    output = args.output.with_name(f"{args.output.stem}.{label}.worker.json")
                    save()
                    code = watchdog(
                        [
                            str(python),
                            str(ROOT / "validation/httpx/worker.py"),
                            "--expected",
                            expected,
                            "--repeats",
                            str(args.repeats),
                            "--output",
                            str(output),
                        ],
                        timeout=max(90, args.repeats * 10),
                        cwd=str(case_work),
                        env=env,
                        new_session=False,
                    )
                    entry["worker_exit_code"] = code
                    if output.exists():
                        entry["worker"] = json.loads(output.read_text())
                        installed = entry["worker"]["module_sha256"]
                        entry["httpcore_modules_match_snapshot"] = (
                            installed["httpcore"] == upstream_modules
                        )
                        entry["cancellens_modules_match_wheel"] = (
                            installed["cancellens"] == wheel_modules
                        )
                        entry["import_outside_checkout"] = Path(
                            entry["worker"]["import_path"]
                        ).is_relative_to(venv) and not Path(
                            entry["worker"]["import_path"]
                        ).is_relative_to(ROOT)
                        versions = {
                            name.lower().replace("_", "-"): version
                            for name, version in entry["worker"]["versions"].items()
                        }
                        pinned = dict(
                            line.split("==")
                            for line in (ROOT / "validation/httpx/requirements.txt")
                            .read_text()
                            .splitlines()
                        )
                        entry["runtime_versions_match_pins"] = (
                            all(versions.get(name) == version for name, version in pinned.items())
                            and versions.get("httpcore") == "1.0.9"
                            and versions.get("cancellens") == "0.1.0"
                        )
                        if not all(
                            entry[k]
                            for k in (
                                "httpcore_modules_match_snapshot",
                                "cancellens_modules_match_wheel",
                                "import_outside_checkout",
                                "runtime_versions_match_pins",
                            )
                        ):
                            raise ValueError("installed artifact provenance mismatch")
                    if code or "worker" not in entry:
                        result["errors"].append(
                            f"{label}: worker exit {code}; partial evidence retained"
                        )
                except Exception as exc:
                    entry["error"] = type(exc).__name__ + ": " + str(exc)
                    result["errors"].append(f"{label}: {type(exc).__name__}")
                finally:
                    save()
                    # Keep incremental sidecars on failure or process interruption.
                    if "worker" in entry and not code and "error" not in entry:
                        output.unlink(missing_ok=True)
    except Exception as exc:
        result["errors"].append(type(exc).__name__ + ": " + str(exc))
    finally:
        save()
    print(f"evidence: {args.output}; errors={result['errors']}")
    return int(bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
