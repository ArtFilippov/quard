"""Offline policy, architecture, installer and process regressions."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX / "src"))
from eryx import _common, _policy, init_architecture
from offline_fixtures import create_repo


class RepositoryTest(unittest.TestCase):
    def test_tool_checkout_can_be_a_consumer(self):
        self.assertEqual(_common.project_dir(ERYX), ERYX)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eryx-test with spaces-")
        self.root = Path(self.tmp.name)
        create_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_package_one_only(self):
        self.assertEqual(init_architecture.package_name(self.root), "demo")
        (self.root / "src/second").mkdir()
        (self.root / "src/second/a.py").write_text("pass")
        with self.assertRaises(_common.EryxError):
            init_architecture.package_name(self.root)

    def test_layer_discovery_and_prohibitions(self):
        (self.root / "src/demo/application").mkdir()
        (self.root / "src/demo/application/api.py").write_text("pass")
        draft, _ = init_architecture.draft_configuration(self.root)
        layers = {m["path"]: m["layer"] for m in draft["modules"]}
        self.assertEqual(layers["demo.application"], "UseCase")
        self.assertEqual(layers["demo.domain"], "Domain")
        domain = next(m for m in draft["modules"] if m["path"] == "demo.domain")
        self.assertIn("demo.application", domain["cannot_depend_on"])
        self.assertTrue(draft["exact"])
        self.assertFalse(draft["ignore_type_checking_imports"])

    def test_validate_native_architecture_config(self):
        data = init_architecture.validate_architecture_config(self.root)
        self.assertEqual(data["root_module"], "forbid")
        path = self.root / "tach.toml"
        path.write_text(path.read_text().replace('root_module = "forbid"', 'root_module = "ignore"'))
        with self.assertRaises(_common.EryxError):
            init_architecture.validate_architecture_config(self.root)

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
