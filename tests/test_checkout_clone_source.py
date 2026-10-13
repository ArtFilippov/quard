"""Keep checkout-local clone builds separate from consumer Git pins."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quard.clone_engine_utils import enabled, installed_engine, require_source_pin
from quard.check_runner.runner_utils import installed_versions, make_jobs


class TestCheckoutCloneSource(unittest.TestCase):
    def test_locked_local_build_and_independent_consumer_pin(self) -> None:
        checkout = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
        self.assertEqual(checkout["tool"]["uv"]["sources"]["quard-clones"],
                         {"path": "rust/quard-clones"})
        package = next(item for item in lock["package"] if item["name"] == "quard-clones")
        self.assertEqual(package["source"], {"directory": "rust/quard-clones"})
        self.assertEqual(installed_engine()["quard-clones"], "0.1.0")
        versions, problems = installed_versions(ROOT)
        self.assertFalse(problems, problems)
        self.assertEqual(versions["quard-clones"], "0.1.0")

        with TemporaryDirectory(prefix="quard-clone-selection-", dir="/tmp/opencode") as temp:
            consumer = Path(temp)
            (consumer / ".quard").mkdir()
            (consumer / ".quard/config.toml").write_text("", encoding="utf-8")
            (consumer / "pyproject.toml").write_text(
                '[tool.uv.sources]\nquard-clones = { git = "https://example.org/quard.git", '
                'rev = "' + 'a' * 40 + '", subdirectory = "rust/quard-clones" }\n',
                encoding="utf-8")
            self.assertTrue(enabled(ROOT))
            self.assertTrue(enabled(consumer))
            require_source_pin(ROOT)
            require_source_pin(consumer)
            self.assertEqual(tomllib.loads((consumer / "pyproject.toml").read_text(
                encoding="utf-8"))["tool"]["uv"]["sources"]["quard-clones"]["rev"],
                "a" * 40)
            for selected in (ROOT, consumer):
                run_dir = selected / ".quard/cache/clone-selection-test"
                jobs = make_jobs(selected, run_dir,
                                 {"production": ["src/example.py"], "tests": []}, True)
                clones = [job for job in jobs if job["tool"] == "jscpd"]
                self.assertEqual({job["kind"] for job in clones},
                                 {"exact", "renamed", "similar"})
                self.assertTrue(all(job["engine"] and
                                    Path(job["argv"][1]).name == "_clone_engine_worker.py"
                                    for job in clones))
                self.assertEqual(json.loads((run_dir / "clone-engine-files.json").read_text(
                    encoding="utf-8"))["paths"], ["src/example.py"])


if __name__ == "__main__":
    unittest.main()
