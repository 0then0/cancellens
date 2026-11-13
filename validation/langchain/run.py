"""Pinned upstream LangChain + local CancelLens wheel, disposable native validation."""

from __future__ import annotations

import argparse
import ast
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

REVISION = "980e4a80d01f699ba359db139cd7c8bfc348c110"
ARCHIVE_SHA256 = "dbcb8da314fd001d605054ea49fd7e0b22d6e065de8bfcc39f795697122ac8f1"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "CancelLens-validation/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def tool_decorators(source: bytes) -> dict[str, list[str]]:
    tree = ast.parse(source)
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AsyncCallbackManagerForToolRun"
    )
    return {
        node.name: [ast.unparse(d) for d in node.decorator_list]
        for node in owner.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name in ("on_tool_end", "on_tool_error")
    }


def extract_core(archive: Path, destination: Path) -> Path:
    prefix = f"langchain-{REVISION}/libs/core/"
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if m.name.startswith(prefix)]
        if not members:
            raise ValueError("pinned archive does not contain langchain-core")
        for member in members:
            if (
                not (destination / member.name).resolve().is_relative_to(destination.resolve())
                or member.issym()
                or member.islnk()
            ):
                raise ValueError("unsafe snapshot archive member")
        tar.extractall(
            destination,
            members=members,
            **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}),
        )
    return destination / prefix


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "validation/langchain/evidence.json")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    if not wheel.is_file() or args.repeats < 1:
        parser.error("existing wheel and positive repeats required")
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "LANGCHAIN_API_KEY", "LANGSMITH_API_KEY"):
        env.pop(name, None)
    env.update({"LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"})
    env.setdefault("UV_CACHE_DIR", str(Path(tempfile.gettempdir()) / "cancellens-uv-cache"))
    data: dict = {
        "acceptance_schema_version": 1,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "revision": REVISION,
        "contract": "BaseTool.ainvoke cancellation records an inline terminal callback event",
        "control": "local callback handler uses bounded AnyIO shielding; upstream unchanged",
        "wheel_sha256": digest(wheel.read_bytes()),
        "errors": [],
    }

    def save() -> None:
        args.output.write_text(json.dumps(data, indent=2) + "\n")

    output = args.output.with_name(args.output.stem + ".worker.json")
    save()
    try:
        issue = json.loads(
            download("https://api.github.com/repos/langchain-ai/langchain/issues/41744")
        )
        head = json.loads(
            download("https://api.github.com/repos/langchain-ai/langchain/commits/master")
        )
        manager = download(
            "https://raw.githubusercontent.com/langchain-ai/langchain/"
            + head["sha"]
            + "/libs/core/langchain_core/callbacks/manager.py"
        )
        data["upstream_check"] = {
            "issue_url": issue["html_url"],
            "issue_state": issue["state"],
            "issue_updated_at": issue["updated_at"],
            "master_sha": head["sha"],
            "master_manager_sha256": digest(manager),
            "master_tool_method_decorators": tool_decorators(manager),
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        metadata = json.loads(
            download(f"https://api.github.com/repos/langchain-ai/langchain/commits/{REVISION}")
        )
        if metadata["sha"] != REVISION:
            raise ValueError("upstream revision identity mismatch")
        data["revision_url"] = metadata["html_url"]
        blob = download(f"https://codeload.github.com/langchain-ai/langchain/tar.gz/{REVISION}")
        data["archive_sha256"] = digest(blob)
        if data["archive_sha256"] != ARCHIVE_SHA256:
            raise ValueError("pinned upstream archive hash mismatch")
        save()
        with tempfile.TemporaryDirectory(prefix="cancellens-langchain-") as temporary:
            work = Path(temporary)
            archive = work / "upstream.tar.gz"
            archive.write_bytes(blob)
            source = extract_core(archive, work / "source")
            original = {
                str(p.relative_to(source / "langchain_core")): digest(p.read_bytes())
                for p in sorted((source / "langchain_core").rglob("*.py"))
            }
            with zipfile.ZipFile(wheel) as package:
                wheel_modules = {
                    p.removeprefix("cancellens/"): digest(package.read(p))
                    for p in package.namelist()
                    if p.startswith("cancellens/") and p.endswith(".py")
                }
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
                    str(ROOT / "validation/langchain/build-constraints.txt"),
                    "-r",
                    str(ROOT / "validation/langchain/requirements.txt"),
                    "--no-deps",
                    str(source),
                    str(wheel),
                ],
                env=env,
                check=True,
            )
            code = watchdog(
                [
                    str(python),
                    str(ROOT / "validation/langchain/worker.py"),
                    "--output",
                    str(output),
                    "--repeats",
                    str(args.repeats),
                ],
                timeout=max(60, args.repeats * 15),
                cwd=str(work),
                env=env,
                new_session=False,
            )
            data["worker_exit_code"] = code
            if output.exists():
                worker = json.loads(output.read_text())
                data["worker"] = worker
                versions = {k.lower().replace("_", "-"): v for k, v in worker["versions"].items()}
                pins = dict(
                    line.split("==")
                    for line in (ROOT / "validation/langchain/requirements.txt")
                    .read_text()
                    .splitlines()
                )
                data["runtime_versions_match_pins"] = (
                    all(versions.get(k) == v for k, v in pins.items())
                    and versions.get("langchain-core") == "1.8.0"
                )
                data["upstream_modules_unchanged"] = (
                    worker["module_sha256"]["langchain_core"] == original
                )
                data["cancellens_modules_match_wheel"] = (
                    worker["module_sha256"]["cancellens"] == wheel_modules
                )
                data["import_outside_checkout"] = Path(worker["import_path"]).is_relative_to(venv)
                if not all(
                    data[k]
                    for k in (
                        "runtime_versions_match_pins",
                        "upstream_modules_unchanged",
                        "cancellens_modules_match_wheel",
                        "import_outside_checkout",
                    )
                ):
                    data["errors"].append("installed provenance mismatch")
                data["errors"].extend(worker["errors"])
            if code or "worker" not in data:
                data["errors"].append(f"worker exit {code}; incremental evidence retained")
    except Exception as exc:
        data["errors"].append(type(exc).__name__ + ": " + str(exc))
    finally:
        save()
    if not data["errors"]:
        output.unlink(missing_ok=True)
    print(f"evidence: {args.output}; errors={data['errors']}")
    return int(bool(data["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
