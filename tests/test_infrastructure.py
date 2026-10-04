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

    def test_limits_exact(self):
        data = _policy.read_limits(self.root)
        self.assertEqual(len(data["production"]), 17)
        self.assertEqual(data["production"]["max_statements"], 30)
        self.assertEqual(data["tests"]["max_statements"], 60)
        self.assertEqual(data["tests"]["max_line_length"], 100)

    def test_apply_idempotent(self):
        self.assertEqual(_policy.configure(self.root), [])
        self.assertEqual(_policy.configure(self.root), [])
        self.assertEqual(_policy.limit_drift(self.root), [])

    def test_apply_new_limits(self):
        path = self.root / ".eryx/readability.toml"
        path.write_text(path.read_text().replace("max_module_lines = 400", "max_module_lines = 444"))
        self.assertIn("production.max_module_lines", _policy.limit_drift(self.root))
        _policy.configure(self.root)
        self.assertEqual(_common.load_toml(self.root / "pyproject.toml")["tool"]["pylint"]["format"]["max-module-lines"], 444)
        self.assertEqual(_policy.limit_drift(self.root), [])

    def test_apply_preserves_other_settings(self):
        _policy.configure(self.root)
        data = _common.load_toml(self.root / "pyproject.toml")
        self.assertEqual(data["tool"]["unrelated"]["value"], "retain")
        self.assertEqual(data["project"]["requires-python"], ">=3.12,<3.15")

    def test_invalid_limit_no_writes(self):
        original = (self.root / "pyproject.toml").read_bytes()
        path = self.root / ".eryx/readability.toml"
        path.write_text(path.read_text().replace("max_complexity = 8", "max_complexity = true"))
        with self.assertRaises(_common.EryxError):
            _policy.configure(self.root)
        self.assertEqual((self.root / "pyproject.toml").read_bytes(), original)

    def test_unknown_limit_rejected(self):
        path = self.root / ".eryx/readability.toml"
        path.write_text(path.read_text() + "\nmax_comlexity = 8\n")
        with self.assertRaises(_common.EryxError):
            _policy.read_limits(self.root)

    def test_inconsistent_limits_rejected(self):
        path = self.root / ".eryx/readability.toml"
        path.write_text(path.read_text().replace("max_positional_arguments = 4", "max_positional_arguments = 6"))
        with self.assertRaises(_common.EryxError):
            _policy.read_limits(self.root)

    def test_missing_catalog_no_partial_apply(self):
        original = (self.root / "pyproject.toml").read_bytes()
        (self.root / ".eryx/pylint-catalog.json").unlink()
        path = self.root / ".eryx/readability.toml"
        path.write_text(path.read_text().replace("max_module_lines = 400", "max_module_lines = 450"))
        with self.assertRaises(_common.EryxError):
            _policy.configure(self.root)
        self.assertEqual((self.root / "pyproject.toml").read_bytes(), original)

    def test_invalid_tach_no_partial_apply(self):
        original = (self.root / "pyproject.toml").read_bytes()
        (self.root / "tach.toml").write_text("[[[invalid")
        with self.assertRaises(Exception):
            _policy.configure(self.root)
        self.assertEqual((self.root / "pyproject.toml").read_bytes(), original)

    def test_scope_and_nested_tests(self):
        generated = self.root / "src/demo/generated"
        generated.mkdir()
        (generated / "x.py").write_text("bad syntax !")
        tests = self.root / "src/demo/tests"
        tests.mkdir()
        (tests / "test_nested.py").write_text("pass")
        files = _policy.discover_files(self.root, _policy.read_scope(self.root))
        self.assertIn("src/demo/generated/", files["excluded_directories"])
        self.assertIn("src/demo/tests/test_nested.py", files["tests"])
        self.assertNotIn("src/demo/generated/x.py", files["production"])

    def test_scope_basename_not_prefix(self):
        path = self.root / "src/demo/generated_models"
        path.mkdir()
        (path / "a.py").write_text("pass")
        files = _policy.discover_files(self.root, _policy.read_scope(self.root))
        self.assertIn("src/demo/generated_models/a.py", files["production"])

    def test_optional_docs_and_core_checks(self):
        pylint = _common.load_toml(self.root / "pyproject.toml")["tool"]["pylint"]
        self.assertEqual(pylint["messages_control"]["enable"], ["all"])
        self.assertIn("missing-function-docstring", pylint["messages_control"]["disable"])
        self.assertNotIn("protected-access", pylint["messages_control"]["disable"])
        self.assertNotIn("unused-variable", pylint["messages_control"]["disable"])

    def test_test_whitebox_no_conflict(self):
        tests = _common.load_toml(self.root / ".eryx/pylint-tests.toml")["tool"]["pylint"]
        disabled = tests["messages_control"]["disable"]
        self.assertIn("W9102", disabled)
        self.assertNotIn("W9101", disabled)
        self.assertIn("protected-access", disabled)

    def test_symlink_managed_target_rejected(self):
        target = self.root / "elsewhere.txt"
        target.write_text("original")
        path = self.root / "pyrightconfig.json"
        path.unlink()
        path.symlink_to(target)
        before = (self.root / "pyproject.toml").read_bytes()
        with self.assertRaises(_common.EryxError):
            _policy.configure(self.root)
        self.assertEqual(target.read_text(), "original")
        self.assertEqual((self.root / "pyproject.toml").read_bytes(), before)

    def test_symlink_source_rejected(self):
        (self.root / "src/demo/link.py").symlink_to(self.root / "src/demo/domain/api.py")
        with self.assertRaises(_common.EryxError):
            _policy.discover_files(self.root, _policy.read_scope(self.root))

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
