"""Installation plans and rollback through the public transaction contract."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

ERYX = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ERYX / "src"))

from eryx import install as entry
from eryx._common import EryxError, read_json
from eryx.installation.transactional_installation import TransactionalInstallation


class TestTransactionalInstallation(unittest.TestCase):
    def project(self, directory: str) -> Path:
        root = Path(directory)
        (root / "src/example").mkdir(parents=True)
        (root / "src/example/__init__.py").write_text("", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            '[project]\nname = "example"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n',
            encoding="utf-8",
        )
        return root

    def test_dry_run_does_not_write_consumer_files(self):
        with TemporaryDirectory(prefix="eryx-installation-") as directory:
            root = self.project(directory)
            before = (root / "pyproject.toml").read_bytes()
            result = TransactionalInstallation(entry).install(root, ERYX, dry_run=True)
            self.assertEqual((result["status"], result["exit_code"]), ("planned", 0))
            self.assertEqual((root / "pyproject.toml").read_bytes(), before)
            self.assertFalse((root / ".eryx").exists())

    def test_failed_tool_step_restores_managed_files_and_logs_failure(self):
        def fail(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            return {"argv": argv, "stdout": "", "stderr": "offline failure",
                    "exit_code": 1, "error": None}

        with TemporaryDirectory(prefix="eryx-installation-") as directory:
            root = self.project(directory)
            before = (root / "pyproject.toml").read_bytes()
            with patch.object(entry, "capture", fail):
                with self.assertRaises(EryxError):
                    TransactionalInstallation(entry).install(root, ERYX, progress=False)
            self.assertEqual((root / "pyproject.toml").read_bytes(), before)
            self.assertTrue(read_json(root / ".eryx/logs/install-error.json")
                            ["managed_files_rolled_back"])
