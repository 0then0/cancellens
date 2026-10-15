"""Offline checks for artifact rejection and retention of failed native evidence."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

from validation.httpx import run


def test_unverified_wheel_is_rejected_before_environment_creation(tmp_path, monkeypatch):
    output = tmp_path / "evidence.json"
    wheel = tmp_path / run.WHEEL_NAME
    wheel.write_bytes(b"unverified wheel")
    metadata = {"urls": [{"filename": run.WHEEL_NAME, "digests": {"sha256": run.WHEEL_SHA256}}]}
    monkeypatch.setattr(run, "download", lambda url: json.dumps(metadata).encode())
    monkeypatch.setattr(sys, "argv", ["run.py", "--wheel", str(wheel), "--output", str(output)])
    assert run.main() == 1
    data = json.loads(output.read_text())
    assert data["snapshots"] == {}
    assert data["errors"] == ["ValueError: CancelLens wheel differs from pinned published artifact"]


def test_failed_worker_evidence_survives_and_other_snapshot_still_runs(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("cancellens/__init__.py", b"wheel module")
    blob = buffer.getvalue()
    digest = hashlib.sha256(blob).hexdigest()
    monkeypatch.setattr(run, "WHEEL_SHA256", digest)
    output = tmp_path / "evidence.json"
    wheel = tmp_path / run.WHEEL_NAME
    wheel.write_bytes(blob)
    base, fixed = (v[0] for v in run.SNAPSHOTS.values())

    def download(url):
        if "pypi.org" in url:
            data = {
                "urls": [
                    {
                        "filename": run.WHEEL_NAME,
                        "url": "https://example.invalid/wheel",
                        "digests": {"sha256": digest},
                        "upload_time_iso_8601": "fixture",
                    }
                ]
            }
        elif "/pulls/" in url:
            data = {
                "html_url": "fixture",
                "state": "open",
                "merged": False,
                "merged_at": None,
                "base": {"sha": base},
                "head": {"sha": fixed},
            }
        elif "/compare/" in url:
            data = {
                "html_url": "fixture",
                "merge_base_commit": {"sha": base},
                "commits": [{"sha": fixed}],
                "files": [],
            }
        elif "/commits/" in url:
            data = {
                "sha": url.rsplit("/", 1)[1],
                "html_url": "fixture",
                "commit": {"message": "fixture"},
                "parents": [],
            }
        else:
            return b"snapshot fixture"
        return json.dumps(data).encode()

    def extract(archive, destination):
        (destination / "httpcore").mkdir(parents=True)
        (destination / "httpcore/__init__.py").write_bytes(b"upstream module")
        (destination / "tests/_async").mkdir(parents=True)
        (destination / "tests/test_cancellations.py").write_bytes(b"test fixture")
        (destination / "tests/_async/test_http11.py").write_bytes(b"test fixture")
        return destination

    called = []

    def worker(command, *, timeout, cwd, env, new_session):
        assert not new_session
        called.append(Path(cwd).name)
        result = {
            "module_sha256": {
                "httpcore": {"__init__.py": hashlib.sha256(b"upstream module").hexdigest()},
                "cancellens": {"__init__.py": hashlib.sha256(b"wheel module").hexdigest()},
            },
            "import_path": str(Path(cwd) / "venv/lib/cancellens/__init__.py"),
            "versions": dict(
                line.replace("-", "_").split("==")
                for line in (run.ROOT / "validation/httpx/requirements.txt")
                .read_text()
                .splitlines()
            ),
            "series": [{"outcome": "UNRESOLVED", "reason": "infrastructure deadline"}],
        }
        result["versions"].update({"httpcore": "1.0.9", "cancellens": "0.1.0"})
        Path(command[command.index("--output") + 1]).write_text(json.dumps(result))
        return 124

    monkeypatch.setattr(run, "download", download)
    monkeypatch.setattr(run, "extract", extract)
    monkeypatch.setattr(run.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "watchdog", worker)
    monkeypatch.setattr(sys, "argv", ["run.py", "--wheel", str(wheel), "--output", str(output)])
    assert run.main() == 1
    data = json.loads(output.read_text())
    assert called == ["base", "proposed_fix"]  # No retry or early abort after first failure.
    for label in called:
        entry = data["snapshots"][label]
        assert entry["worker_exit_code"] == 124
        assert entry["runtime_versions_match_pins"]
        assert entry["worker"]["series"][0]["outcome"] == "UNRESOLVED"
        assert output.with_name(f"evidence.{label}.worker.json").exists()
    assert len(data["errors"]) == 2
