"""Offline policy, architecture, installer and process regressions."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX / "src"))
from eryx import _common, _policy


class RepositoryTest(unittest.TestCase):
    def test_tool_checkout_can_be_a_consumer(self):
        self.assertEqual(_common.project_dir(ERYX), ERYX)

    def test_clean_arch_mapping_correct_direction(self):
        mapping = _policy.clean_config()["layer_map"]
        self.assertIn("Domain", mapping)
        self.assertIn("domain", mapping["Domain"])

class PrimitiveTests(unittest.TestCase):
    def test_capture_timeout_real_process(self):
        result = _common.capture([sys.executable, "-c", "import time; time.sleep(10)"], Path(tempfile.gettempdir()), timeout=1)
        self.assertEqual(result["error"], "TOOL_TIMEOUT")
        self.assertNotEqual(result["exit_code"], 0)

    def test_capture_start_failure(self):
        result = _common.capture(["/definitely/nonexistent-tool"], Path(tempfile.gettempdir()))
        self.assertEqual(result["error"], "TOOL_START_FAILED")

    def test_lock_contention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with _common.repo_lock(root):
                with self.assertRaises(_common.EryxError):
                    with _common.repo_lock(root):
                        pass

    def test_reject_path_traversal(self):
        root = Path("/tmp/example")
        with self.assertRaises(_common.EryxError):
            _common.safe_target(root / "../escape", root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
