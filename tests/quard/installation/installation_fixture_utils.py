"""Mock uv and run subprocesses against temporary installation consumers."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any

from offline_fixtures import CATALOG, QUARD, native_result
from quard import _policy
from quard._common import write_json


def fake_uv(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    if str(QUARD / "_audit.py") in argv:
        _policy.configure(cwd, initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)
        write_json(cwd / ".quard/logs/install-probes/result.json",
                   {"versions": {}, "smoke_tests": {"native": "mocked"}})
    return native_result(argv)


def run_command(argv: list[str], cwd: Path, expected: int = 0) -> str:
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "UV_NO_PROGRESS": "1",
                "GIT_AUTHOR_NAME": "Quard test", "GIT_AUTHOR_EMAIL": "test@example.org",
                "GIT_COMMITTER_NAME": "Quard test", "GIT_COMMITTER_EMAIL": "test@example.org"})
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                            timeout=240, check=False)
    if result.returncode != expected:
        root = Path(argv[argv.index("--project") + 1]) if "--project" in argv else cwd
        details = "\n".join(f"{path.relative_to(root)}:\n{path.read_text()[-8000:]}"
                            for path in (root / ".quard/logs").rglob("*.log"))
        raise AssertionError(
            f"{argv}\nexit={result.returncode}\n{result.stdout}\n{result.stderr}\n{details}")
    return result.stdout
