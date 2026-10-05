"""Consumer policy regressions on an isolated offline project."""
from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ERYX = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ERYX / "src"))
sys.path.insert(0, str(ERYX / "tests"))

from eryx._common import EryxError, load_toml
from eryx import _policy
from eryx.consumer_policy.configured_consumer_policy import ConfiguredConsumerPolicy
from eryx.consumer_policy.consumer_policy import ConsumerPolicy
from eryx.check_selection_utils import CHECK_DEFAULTS
from offline_fixtures import create_repo, managed_bytes


class TestConfiguredConsumerPolicy(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        directory = TemporaryDirectory(prefix="eryx-test with spaces-")
        self.root: Path = Path(self.enterContext(directory))
        create_repo(self.root)

    def policy(self) -> ConsumerPolicy:
        return ConfiguredConsumerPolicy(self.root)

    def test_limits_exact(self):
        data = self.policy().limits()
        self.assertEqual(len(data["production"]), 17)
        self.assertEqual(data["production"]["max_statements"], 30)
        self.assertEqual(data["tests"]["max_statements"], 60)
        self.assertEqual(data["tests"]["max_line_length"], 100)
        mapping = _policy.clean_config()["layer_map"]
        self.assertIn("Domain", mapping)
        self.assertIn("domain", mapping["Domain"])

    def test_apply_idempotent(self):
        self.assertEqual(self.policy().apply(), [])
        self.assertEqual(self.policy().apply(), [])
        self.assertEqual(self.policy().limit_drift(), [])

    def test_apply_new_limits(self):
        path = self.root / ".eryx/config.toml"
        path.write_text(path.read_text().replace("max_module_lines = 400",
                                                 "max_module_lines = 444"))
        self.assertIn("production.max_module_lines", self.policy().limit_drift())
        self.policy().apply()
        config = load_toml(self.root / "pyproject.toml")
        self.assertEqual(config["tool"]["pylint"]["format"]["max-module-lines"], 444)
        self.assertEqual(self.policy().limit_drift(), [])

    def test_apply_scope_restores_managed_fields_and_preserves_native_options(self):
        path = self.root / ".eryx/config.toml"
        path.write_text(path.read_text().replace('"generated", "_generated"',
                                                  '"generated", "custom", "_generated"'))
        pyproject = self.root / "pyproject.toml"
        pyproject.write_text(pyproject.read_text() + '\n[tool.pylint.design.extra-policy]\nvalue = "owned"\n')
        drift = self.policy().scope_drift()
        self.assertIn("production.main.ignore-paths", drift)
        self.assertIn("pyrightconfig.json.exclude", drift)
        self.policy().apply()
        self.assertEqual(self.policy().scope_drift(), [])
        self.assertEqual(load_toml(pyproject)["tool"]["pylint"]["design"]["extra-policy"],
                         {"value": "owned"})

    def test_config_validation_rejects_unknown_sections_and_check_names(self):
        path = self.root / ".eryx/config.toml"
        original = path.read_text()
        for text in (original + "\n[unrecognized]\nvalue = true\n",
                     original.replace("constructor_purity = false",
                                      "constructor_purity = false\nother_check = true"),
                     original.replace("constructor_purity = false",
                                      "constructor_purity = 1"),
                     original.replace("max_complexity = 8", "max_complexity = 1000001")):
            with self.subTest(text=text[-60:]):
                path.write_text(text)
                before = managed_bytes(self.root)
                with self.assertRaises(EryxError):
                    self.policy().apply()
                self.assertEqual(managed_bytes(self.root), before)

    def test_each_new_switch_requires_a_boolean_and_unknown_jobs_fail(self):
        path = self.root / ".eryx/config.toml"
        original = path.read_text(encoding="utf-8")
        for key in CHECK_DEFAULTS:
            if key == "constructor_purity":
                continue
            with self.subTest(key=key):
                path.write_text(original.replace(f"{key} = true", f"{key} = 1"),
                                encoding="utf-8")
                with self.assertRaises(EryxError) as failure:
                    self.policy().checks()
                self.assertEqual(failure.exception.code, "INVALID_CHECKS")
        path.write_text(original.replace("lcom4 = true",
                                         "lcom4 = true\ndependency_types = false"),
                        encoding="utf-8")
        with self.assertRaises(EryxError) as failure:
            self.policy().checks()
        self.assertEqual(failure.exception.code, "INVALID_CHECKS")

    def test_apply_preserves_other_settings(self):
        self.policy().apply()
        data = load_toml(self.root / "pyproject.toml")
        self.assertEqual(data["tool"]["unrelated"]["value"], "retain")
        self.assertEqual(data["project"]["requires-python"], ">=3.12,<3.15")

    def test_invalid_limit_no_writes(self):
        path = self.root / ".eryx/config.toml"
        path.write_text(path.read_text().replace("max_complexity = 8", "max_complexity = true"))
        original = managed_bytes(self.root)
        with self.assertRaises(EryxError):
            self.policy().apply()
        self.assertEqual(managed_bytes(self.root), original)

    def test_unknown_limit_rejected(self):
        path = self.root / ".eryx/config.toml"
        path.write_text(path.read_text().replace("max_complexity = 8",
                                                  "max_complexity = 8\nmax_comlexity = 8"))
        with self.assertRaises(EryxError):
            self.policy().limits()

    def test_inconsistent_limits_rejected(self):
        path = self.root / ".eryx/config.toml"
        path.write_text(path.read_text().replace("max_positional_arguments = 4",
                                                 "max_positional_arguments = 6"))
        with self.assertRaises(EryxError):
            self.policy().limits()

    def test_missing_catalog_no_partial_apply(self):
        (self.root / ".eryx/pylint-catalog.json").unlink()
        path = self.root / ".eryx/config.toml"
        path.write_text(path.read_text().replace("max_module_lines = 400",
                                                 "max_module_lines = 450"))
        original = managed_bytes(self.root)
        with self.assertRaises(EryxError):
            self.policy().apply()
        self.assertEqual(managed_bytes(self.root), original)

    def test_invalid_tach_no_partial_apply(self):
        (self.root / "tach.toml").write_text("[[[invalid")
        original = managed_bytes(self.root)
        with self.assertRaises(Exception):
            self.policy().apply()
        self.assertEqual(managed_bytes(self.root), original)

    def test_scope_and_nested_tests(self):
        generated = self.root / "src/demo/generated"
        generated.mkdir()
        (generated / "x.py").write_text("bad syntax !")
        tests = self.root / "src/demo/tests"
        tests.mkdir()
        (tests / "test_nested.py").write_text("pass")
        files = self.policy().files()
        self.assertIn("src/demo/generated/", files["excluded_directories"])
        self.assertIn("src/demo/tests/test_nested.py", files["tests"])
        self.assertNotIn("src/demo/generated/x.py", files["production"])

    def test_scope_basename_not_prefix(self):
        path = self.root / "src/demo/generated_models"
        path.mkdir()
        (path / "a.py").write_text("pass")
        files = self.policy().files()
        self.assertIn("src/demo/generated_models/a.py", files["production"])

    def test_optional_docs_and_core_checks(self):
        pylint = load_toml(self.root / "pyproject.toml")["tool"]["pylint"]
        self.assertEqual(pylint["messages_control"]["enable"], ["all"])
        self.assertIn("missing-function-docstring", pylint["messages_control"]["disable"])
        self.assertNotIn("protected-access", pylint["messages_control"]["disable"])
        self.assertNotIn("unused-variable", pylint["messages_control"]["disable"])

    def test_test_whitebox_no_conflict(self):
        tests = load_toml(self.root / ".eryx/pylint-tests.toml")["tool"]["pylint"]
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
        with self.assertRaises(EryxError):
            self.policy().apply()
        self.assertEqual(target.read_text(), "original")
        self.assertEqual((self.root / "pyproject.toml").read_bytes(), before)

    def test_symlink_source_rejected(self):
        (self.root / "src/demo/link.py").symlink_to(self.root / "src/demo/domain/api.py")
        with self.assertRaises(EryxError):
            self.policy().files()

    def test_invalid_scope_rejected_without_writes(self):
        path = self.root / ".eryx/config.toml"
        path.write_text(path.read_text().replace('"generated"', '"../generated"'))
        original = managed_bytes(self.root)
        with self.assertRaises(EryxError):
            self.policy().apply()
        self.assertEqual(managed_bytes(self.root), original)


if __name__ == "__main__":
    unittest.main()
