"""Install a built wheel into a temporary venv and run pytest outside the checkout."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from watchdog import run as watchdog


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    wheel = args.wheel.resolve()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.setdefault("UV_CACHE_DIR", str(Path(tempfile.gettempdir()) / "cancellens-uv-cache"))
    # Pin all pytest dependencies to the project's lockfile, without installing
    # dev linters into this independent consumer environment.
    with tempfile.TemporaryDirectory(prefix="cancellens-wheel-") as temporary:
        work = Path(temporary)
        requirements = work / "test-requirements.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--no-emit-project",
                "--no-hashes",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ],
            env=env,
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        # uv export includes the dev group. Filter tools and their exclusive deps;
        # pytest's shared typing-extensions/packaging dependencies remain pinned.
        lines = requirements.read_text().splitlines()
        skip = ("mypy==", "mypy-extensions==", "ruff==", "pathspec==", "ast-serialize==", "librt==")
        requirements.write_text(
            "\n".join(line for line in lines if not line.startswith(skip)) + "\n"
        )
        venv = work / "venv"
        subprocess.run(["uv", "venv", "--python", args.python, str(venv)], env=env, check=True)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), str(wheel), "-r", str(requirements)],
            env=env,
            check=True,
        )
        readme_example = re.search(r"```python\n(.*?)```", (root / "README.md").read_text(), re.S)
        assert readme_example is not None
        example = readme_example.group(1) + "\n" + (root / "examples/pool.py").read_text()
        (work / "test_installed.py").write_text(
            example
            + "\n\ndef test_installed_path():\n"
            + "    import cancellens\n"
            + f"    assert {str(root)!r} not in cancellens.__file__\n"
        )
        code = watchdog(
            [str(python), "-m", "pytest", "-q", str(work / "test_installed.py")],
            timeout=30,
            cwd=str(work),
            env=env,
            new_session=False,
        )
        if code:
            raise SystemExit(code)
        metadata = subprocess.check_output(
            [
                str(python),
                "-c",
                "import importlib.metadata as m, json; "
                "print(json.dumps(m.requires('cancellens') or []))",
            ],
            cwd=work,
            env=env,
            text=True,
        )
        assert json.loads(metadata) == [], "runtime dependencies unexpectedly present"
        print("wheel smoke: pytest example passes outside checkout; zero runtime dependencies")


if __name__ == "__main__":
    main()
