"""Offline provenance and interrupted-evidence checks for the native validation."""

from __future__ import annotations

import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from validation.langchain import run

MANAGER = b"""class AsyncCallbackManagerForToolRun:
    async def on_tool_end(self): pass
    async def on_tool_error(self): pass
"""


def test_snapshot_links_cannot_escape_disposable_directory(tmp_path):
    archive = tmp_path / "snapshot.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        link = tarfile.TarInfo(f"langchain-{run.REVISION}/libs/core/outside")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp"
        tar.addfile(link)
    with pytest.raises(ValueError, match="unsafe snapshot"):
        run.extract_core(archive, tmp_path / "source")


def test_failed_native_worker_keeps_incremental_evidence(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("cancellens/__init__.py", b"wheel source")
    wheel = tmp_path / "cancellens.whl"
    wheel.write_bytes(buffer.getvalue())
    output = tmp_path / "evidence.json"
    blob = b"verified archive fixture"
    monkeypatch.setattr(run, "ARCHIVE_SHA256", run.digest(blob))

    def download(url):
        if "/issues/" in url:
            data = {"html_url": "fixture", "state": "open", "updated_at": "fixture"}
        elif "/commits/" in url:
            data = {"sha": run.REVISION, "html_url": "fixture"}
        elif "raw.githubusercontent.com" in url:
            return MANAGER
        else:
            return blob
        return json.dumps(data).encode()

    def extract(archive, destination):
        package = destination / "langchain_core"
        package.mkdir(parents=True)
        (package / "__init__.py").write_bytes(b"upstream source")
        return destination

    def worker(command, *, cwd, env, new_session, timeout):
        assert not new_session
        assert env["LANGSMITH_TRACING"] == "false"
        pins = dict(
            line.split("==")
            for line in (run.ROOT / "validation/langchain/requirements.txt")
            .read_text()
            .splitlines()
        )
        pins["langchain-core"] = "1.8.0"
        data = {
            "versions": pins,
            "import_path": str(Path(cwd) / "venv/lib/cancellens/__init__.py"),
            "module_sha256": {
                "langchain_core": {"__init__.py": run.digest(b"upstream source")},
                "cancellens": {"__init__.py": run.digest(b"wheel source")},
            },
            "variants": {"anyio_unshielded": {"partial": True}},
            "errors": ["interrupted after selected replay"],
        }
        Path(command[command.index("--output") + 1]).write_text(json.dumps(data))
        return 124

    monkeypatch.setattr(run, "download", download)
    monkeypatch.setattr(run, "extract_core", extract)
    monkeypatch.setattr(run.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "watchdog", worker)
    monkeypatch.setattr(sys, "argv", ["run.py", "--wheel", str(wheel), "--output", str(output)])
    assert run.main() == 1
    data = json.loads(output.read_text())
    assert data["worker_exit_code"] == 124
    assert data["upstream_modules_unchanged"]
    assert data["cancellens_modules_match_wheel"]
    assert data["runtime_versions_match_pins"]
    assert data["worker"]["variants"]["anyio_unshielded"]["partial"]
    assert output.with_name("evidence.worker.json").exists()
    assert "interrupted after selected replay" in data["errors"]


def test_modified_archive_is_rejected_before_installing_dependencies(tmp_path, monkeypatch):
    wheel = tmp_path / "cancellens.whl"
    wheel.write_bytes(b"local wheel fixture")
    output = tmp_path / "evidence.json"

    def download(url):
        if "/issues/" in url:
            return json.dumps(
                {"html_url": "fixture", "state": "open", "updated_at": "fixture"}
            ).encode()
        if "/commits/" in url:
            return json.dumps({"sha": run.REVISION, "html_url": "fixture"}).encode()
        if "raw.githubusercontent.com" in url:
            return MANAGER
        return b"unexpected snapshot"

    monkeypatch.setattr(run, "download", download)
    monkeypatch.setattr(run.subprocess, "run", lambda *a, **k: pytest.fail("unexpected install"))
    monkeypatch.setattr(sys, "argv", ["run.py", "--wheel", str(wheel), "--output", str(output)])
    assert run.main() == 1
    assert json.loads(output.read_text())["errors"] == [
        "ValueError: pinned upstream archive hash mismatch"
    ]
