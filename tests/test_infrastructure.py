"""Offline tests: policy, adapters, orchestration and installer transactions.

Native analyzers are explicitly mocked; these tests do not prove their integration.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX / "src"))
from eryx import _common, _policy, _reports, check, init_architecture
spec = importlib.util.spec_from_file_location("eryx_install", ERYX / "src/eryx/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

CATALOG = {"message_ids": ["R0915", "R1260", "W9001", "W9003", "W9101", "W9102", "W9201", "W9301", "W9501"],
           "plugins": ["pylint.extensions.mccabe", "pylint.extensions.broad_try_clause", "clean_architecture_linter"]}


def create_repo(root: Path, configured: bool = True, ready: bool = True) -> None:
    shutil.copytree(ERYX / "defaults", root / ".eryx")
    (root / "src/demo/domain").mkdir(parents=True)
    (root / "src/demo/__init__.py").write_text("", encoding="utf-8")
    (root / "src/demo/domain/api.py").write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests/test_value.py").write_text("def test_value() -> None:\n    assert 1 == 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\nrequires-python = ">=3.12,<3.15"\ndependencies = []\n\n[tool.unrelated]\nvalue = "retain"\n', encoding="utf-8")
    _common.write_json(root / ".eryx/pylint-catalog.json", CATALOG)
    if configured:
        _policy.configure(root, initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)
    if ready:
        draft, _ = init_architecture.draft_configuration(root)
        import tomlkit
        _common.atomic_write(root / "tach.toml", tomlkit.dumps(draft))
        _common.write_json(root / ".eryx/architecture.ready.json", {"accepted": True})


def native_result(argv, *, stdout="", stderr="", code=0, error=None):
    return {"argv": argv, "stdout": stdout, "stderr": stderr, "exit_code": code, "error": error, "duration_seconds": 0.01}


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

    def test_installer_plan_no_changes(self):
        files = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = installer.install(self.root, ERYX, dry_run=True, progress=False)
        self.assertEqual(result["status"], "planned")
        self.assertIn("pyright[nodejs]==1.1.414", result["dependencies"])
        current = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(files, current)

    def test_installer_rollback_on_uv_failure(self):
        before = (self.root / "pyproject.toml").read_bytes()
        (self.root / ".eryx/checks.toml").unlink()
        fail = lambda argv, cwd, timeout: native_result(argv, stderr="intentional offline test failure", code=1)
        with patch.object(installer, "capture", fail):
            with self.assertRaises(_common.EryxError):
                installer.install(self.root, ERYX, progress=False)
        self.assertEqual((self.root / "pyproject.toml").read_bytes(), before)
        self.assertFalse((self.root / ".eryx/checks.toml").exists())
        error = _common.read_json(self.root / ".eryx/logs/install-error.json")
        self.assertTrue(error["managed_files_rolled_back"])

    def test_prepare_project_idempotent(self):
        installer.prepare_project(self.root)
        first = (self.root / "pyproject.toml").read_bytes()
        installer.prepare_project(self.root)
        self.assertEqual(first, (self.root / "pyproject.toml").read_bytes())
        data = _common.load_toml(self.root / "pyproject.toml")
        self.assertEqual(data["project"]["requires-python"], ">=3.12,<3.15")
        self.assertEqual(data["tool"]["uv"]["dependency-groups"]["dev"]["requires-python"], ">=3.12")

    def test_full_install_reinstall_mocked(self):
        def fake(argv, cwd, timeout):
            if str(ERYX / "_audit.py") in argv:
                _policy.configure(cwd, initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)
                _common.write_json(cwd / ".eryx/logs/install-probes/result.json", {"versions": {}, "smoke_tests": {"native": "mocked in this offline test"}})
            return native_result(argv)
        with patch.object(installer, "capture", fake):
            installer.install(self.root, ERYX, progress=False)
            first = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file() and "logs" not in p.parts and "__pycache__" not in p.parts}
            installer.install(self.root, ERYX, progress=False)
            second = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file() and "logs" not in p.parts and "__pycache__" not in p.parts}
            self.assertEqual(first, second)



class ParserTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/tmp/demo-repo")

    def test_pylint_json2(self):
        text = json.dumps({"messages": [{"messageId": "C0301", "message": "Line too long", "path": "/tmp/demo-repo/src/demo/a.py", "line": 4, "column": 0, "type": "convention", "symbol": "line-too-long"}]})
        items, err = _reports.parse_pylint(text, self.root)
        self.assertIsNone(err)
        self.assertEqual(items[0]["column"], 1)
        self.assertEqual(items[0]["path"], "src/demo/a.py")

    def test_pylint_bad_plugin_is_infra(self):
        text = json.dumps({"messages": [{"messageId": "E0013", "message": "Bad plugin", "path": "config", "line": 1, "column": 0}]})
        result = _reports.normalize("pylint", native_result([], stdout=text, code=2), self.root)
        self.assertEqual(result["status"], "error")

    def test_pyright_one_based_and_banner(self):
        text = "optional banner\n" + json.dumps({"generalDiagnostics": [{"file": "/tmp/demo-repo/src/a.py", "message": "Invalid type", "severity": "error", "rule": "reportReturnType", "range": {"start": {"line": 0, "character": 3}}}]})
        result = _reports.parse_pyright(text, self.root)[0]
        self.assertEqual((result["line"], result["column"]), (1, 4))

    def test_pydoclint_stderr(self):
        text = "\x1b[31msrc/demo/a.py:8: DOC101: Missing args\x1b[0m\n"
        result = _reports.normalize("pydoclint", native_result([], stderr=text, code=1), self.root)
        self.assertEqual(result["status"], "violations")
        self.assertEqual(result["diagnostics"][0]["rule"], "DOC101")

    def test_tach_positioned(self):
        result = _reports.normalize("tach", native_result([], stdout="❌ src/demo/a.py[L8]: Cannot import 'demo.b'\n", code=1), self.root)
        self.assertEqual(result["status"], "violations")
        item = result["diagnostics"][0]
        self.assertEqual(item["path"], "src/demo/a.py")
        self.assertEqual(item["line"], 8)
        self.assertTrue(item["rule_is_adapter_category"])

    def test_unparsed_failure_never_passes(self):
        result = _reports.normalize("tach", native_result([], stdout="new unknown error format", code=1), self.root)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["diagnostics"][-1]["is_infrastructure_error"])

    def test_timeout_is_error(self):
        result = _reports.normalize("pyright", native_result([], error="TOOL_TIMEOUT", code=-15), self.root)
        self.assertEqual(result["error"], "TOOL_TIMEOUT")

    def test_duplicate_same_native_only(self):
        first = _reports.diagnostic("pylint", "W0611", "Unused", path="a.py", line=3)
        second = _reports.diagnostic("pyright", "reportUnusedImport", "Unused", path="a.py", line=3)
        result = _reports.sorted_diagnostics([second, first, dict(first)])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["tool"], "pylint")

    def test_config_warning_zero_exit_is_error(self):
        result = _reports.normalize("pyright", native_result([], stdout='{"generalDiagnostics": []}', stderr='Config contains unrecognized setting "bad".'), self.root)
        self.assertEqual(result["status"], "error")

    def test_tach_native_adapter_exact_unused(self):
        payload = {"eryx_tach_adapter": 1, "execution_error": None, "diagnostics": [{"rule": "unused-dependency", "message": "Unused declaration", "path": "tach.toml", "line": None, "severity": "error", "rule_is_adapter_category": True}]}
        result = _reports.normalize("tach", native_result([], stdout=json.dumps(payload), code=1), self.root)
        self.assertEqual(result["status"], "violations")
        self.assertEqual(result["diagnostics"][0]["rule"], "unused-dependency")

    def test_pyright_message_about_config_not_execution_error(self):
        payload = {"generalDiagnostics": [{"file": "src/a.py", "message": "Function config returns error incorrectly", "severity": "error", "rule": "reportReturnType", "range": {"start": {"line": 1, "character": 0}}}]}
        result = _reports.normalize("pyright", native_result([], stdout=json.dumps(payload), code=1), self.root)
        self.assertEqual(result["status"], "violations")



class RunnerTests(unittest.TestCase):
    def setUp(self):
        RepositoryTest.setUp(self)

    def tearDown(self):
        RepositoryTest.tearDown(self)

    def fake_tools(self, failures=None):
        failures = failures or {}
        seen = []
        def fake(argv, cwd, timeout):
            if str(ERYX / "_lcom4_worker.py") in argv:
                name = "lcom4"
            elif str(ERYX / "_method_decorators_worker.py") in argv:
                name = "method-decorators"
            elif str(ERYX / "_instance_fields_worker.py") in argv:
                name = "instance-fields"
            elif str(ERYX / "_class_contracts_worker.py") in argv:
                name = "class-contracts"
            elif str(ERYX / "_contract_folders_worker.py") in argv:
                name = "contract-folders"
            elif str(ERYX / "_test_placement_worker.py") in argv:
                name = "test-placement"
            elif str(ERYX / "_outer_classes_worker.py") in argv:
                name = "outer-classes"
            elif str(ERYX / "_encapsulated_only_worker.py") in argv:
                name = "encapsulated-only"
            elif str(ERYX / "_constructor_purity_worker.py") in argv:
                name = "constructor-purity"
            elif "--manifest" in argv:
                name = "pylint-tests" if "tests" in argv[argv.index("--manifest") + 1] else "pylint-production"
                output = Path(argv[argv.index("--output") + 1])
                _common.write_json(output, {"messages": []})
            elif "pyright" in argv:
                name = "pyright"
            elif Path(argv[0]).name == "pydoclint":
                name = "pydoclint"
            else:
                name = "tach-external" if "check-external" in argv else "tach"
            seen.append(name)
            if name in failures:
                return native_result(argv, **failures[name])
            return native_result(argv, stdout=('{"generalDiagnostics": []}' if name == "pyright" else
                                  '{"eryx_lcom4_adapter": 1, "diagnostics": [], "execution_error": null}'
                                  if name == "lcom4" else
                                   '{"eryx_method_decorators_adapter": 1, "diagnostics": [], "execution_error": null}'
                                   if name == "method-decorators" else
                                   '{"eryx_instance_fields_adapter": 1, "diagnostics": [], "execution_error": null}'
                                   if name == "instance-fields" else
                                    '{"eryx_class_contracts_adapter": 1, "diagnostics": [], "execution_error": null}'
                                    if name == "class-contracts" else
                                    '{"eryx_contract_folders_adapter": 1, "diagnostics": [], "execution_error": null}'
                                    if name == "contract-folders" else
                                     '{"eryx_test_placement_adapter": 1, "diagnostics": [], "execution_error": null}'
                                     if name == "test-placement" else
                                     '{"eryx_outer_classes_adapter": 1, "diagnostics": [], "execution_error": null}'
                                     if name == "outer-classes" else
                                     '{"eryx_encapsulated_only_adapter": 1, "diagnostics": [], "execution_error": null, "declarations": [], "classes": [], "effective_targets": {}}'
                                     if name == "encapsulated-only" else
                                     '{"eryx_constructor_purity_adapter": 1, "diagnostics": [], "execution_error": null}'
                                     if name == "constructor-purity" else ""))
        return fake, seen

    def test_all_tools_pass_mocked(self):
        fake, seen = self.fake_tools()
        report = check.run_checks(self.root, capture_fn=fake, versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 0, report)
        self.assertEqual(len(seen), 14)
        self.assertNotIn("constructor-purity", seen)
        constructor = next(t for t in report["tools"] if t["name"] == "constructor-purity")
        self.assertEqual((constructor["status"], constructor["exit_code"]), ("skipped", None))
        self.assertFalse((self.root / report["report_path"]).parent.joinpath("constructor-purity-files.json").exists())
        self.assertEqual(report["coverage"]["lcom4"]["mode"], "full")
        self.assertEqual(report["coverage"]["lcom4"]["analyzed_files"],
                         report["coverage"]["lcom4"]["selected_files"])
        self.assertTrue((self.root / report["latest_report_path"]).is_file())
        import jsonschema
        jsonschema.validate(report, _common.read_json(ERYX / "report.schema.json"))

    def test_selected_lcom4_deduplicates_without_narrowing_other_jobs(self):
        fake, seen = self.fake_tools()
        report = check.run_checks(self.root, lcom_files="tests/test_value.py src/demo/domain/api.py tests/test_value.py",
                                  capture_fn=fake, versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 0, report)
        self.assertEqual(len(seen), 14)
        coverage = report["coverage"]["lcom4"]
        self.assertEqual(coverage["mode"], "selected")
        self.assertEqual(coverage["selected_files"], ["tests/test_value.py", "src/demo/domain/api.py"])
        self.assertEqual(coverage["analyzed_files"], coverage["selected_files"])
        self.assertEqual(next(t for t in report["tools"] if t["name"] == "lcom4")["status"], "passed")
        manifest = self.root / report["report_path"].replace("report.json", "lcom4-files.json")
        self.assertEqual(_common.read_json(manifest), coverage["selected_files"])

    def test_explicit_empty_lcom4_skips_only_lcom4(self):
        fake, seen = self.fake_tools()
        report = check.run_checks(self.root, lcom_files="", capture_fn=fake,
                                  versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 0, report)
        self.assertEqual(len(seen), 13)
        self.assertNotIn("lcom4", seen)
        self.assertIn("method-decorators", seen)
        self.assertEqual(report["coverage"]["lcom4"], {"mode": "skipped", "requested": "",
                         "selected_files": [], "analyzed_files": []})
        self.assertEqual(next(t for t in report["tools"] if t["name"] == "lcom4")["status"], "skipped")
        import jsonschema
        jsonschema.validate(report, _common.read_json(ERYX / "report.schema.json"))

    def test_method_decorator_violation_runs_with_skipped_lcom_and_native_results(self):
        source = self.root / "src/demo/domain/api.py"
        source.write_text("class Example:\n    @staticmethod\n    def make(): pass\n", encoding="utf-8")
        fake, seen = self.fake_tools({"pyright": {"stdout": json.dumps({
            "generalDiagnostics": [{"file": str(source), "message": "Native issue",
                                    "severity": "error", "rule": "reportReturnType",
                                    "range": {"start": {"line": 0, "character": 0}}}]}), "code": 1}})

        def capture(argv, cwd, timeout):
            if str(ERYX / "_method_decorators_worker.py") in argv:
                seen.append("method-decorators")
                return _common.capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        report = check.run_checks(self.root, lcom_files="", capture_fn=capture,
                                  versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 1, report)
        self.assertEqual({d["rule"] for d in report["diagnostics"]},
                         {"NON_INSTANCE_METHOD", "reportReturnType"})
        self.assertEqual(next(t for t in report["tools"] if t["name"] == "method-decorators")["status"],
                         "violations")
        self.assertEqual(report["coverage"]["lcom4"]["mode"], "skipped")
        self.assertIn("pyright", seen)

    def test_instance_field_violation_runs_with_skipped_lcom_and_native_results(self):
        source = self.root / "src/demo/domain/api.py"
        source.write_text("class Example:\n    def __init__(self): self.field = 1\n"
                          "def use(value: Example):\n    return value.field\n", encoding="utf-8")
        (self.root / "tests/test_value.py").write_text(
            "from demo.domain.api import Example\n"
            "def test_field():\n    return Example().field\n", encoding="utf-8")
        fake, seen = self.fake_tools({"pyright": {"stdout": json.dumps({
            "generalDiagnostics": [{"file": str(source), "message": "Native issue",
                                    "severity": "error", "rule": "reportReturnType",
                                    "range": {"start": {"line": 0, "character": 0}}}]}), "code": 1}})

        def capture(argv, cwd, timeout):
            if str(ERYX / "_instance_fields_worker.py") in argv:
                seen.append("instance-fields")
                return _common.capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        report = check.run_checks(self.root, lcom_files="", capture_fn=capture,
                                  versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 1, report)
        self.assertEqual([(d["path"], d["line"], d["rule"]) for d in report["diagnostics"]
                          if d["tool"] == "instance-fields"], [
            ("src/demo/domain/api.py", 4, "EXTERNAL_INSTANCE_FIELD"),
            ("tests/test_value.py", 3, "EXTERNAL_INSTANCE_FIELD")])
        self.assertIn("reportReturnType", {d["rule"] for d in report["diagnostics"]})
        self.assertEqual(next(t for t in report["tools"] if t["name"] == "instance-fields")
                         ["status"], "violations")
        self.assertEqual(report["coverage"]["lcom4"]["mode"], "skipped")

    def test_class_contract_violation_runs_with_skipped_lcom_and_native_results(self):
        source = self.root / "src/demo/domain/api.py"
        source.write_text("class Missing: pass\n", encoding="utf-8")
        (self.root / "tests/test_value.py").write_text("class TestFixture: pass\n", encoding="utf-8")
        stub = self.root / "src/demo/domain/stubs.pyi"
        stub.write_text("class Stub: ...\n", encoding="utf-8")
        fake, seen = self.fake_tools({"pyright": {"stdout": json.dumps({
            "generalDiagnostics": [{"file": str(source), "message": "Native issue",
                                    "severity": "error", "rule": "reportReturnType",
                                    "range": {"start": {"line": 0, "character": 0}}}]}), "code": 1}})

        def capture(argv, cwd, timeout):
            if str(ERYX / "_class_contracts_worker.py") in argv:
                seen.append("class-contracts")
                return _common.capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        report = check.run_checks(self.root, lcom_files="", capture_fn=capture,
                                  versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 1, report)
        self.assertEqual({d["path"] for d in report["diagnostics"]
                          if d["rule"] == "MISSING_CLASS_CONTRACT"},
                         {"src/demo/domain/api.py", "src/demo/domain/stubs.pyi",
                          "tests/test_value.py"})
        self.assertIn("reportReturnType", {d["rule"] for d in report["diagnostics"]})
        self.assertEqual(next(t for t in report["tools"] if t["name"] == "class-contracts")
                         ["status"], "violations")
        self.assertEqual(report["coverage"]["lcom4"]["mode"], "skipped")

    def test_missing_receiver_runs_with_narrowed_or_skipped_lcom_and_native_results(self):
        source = self.root / "src/demo/domain/api.py"
        source.write_text("class Example:\n    def missing(): pass\n", encoding="utf-8")
        stub = self.root / "src/demo/domain/stubs.pyi"
        stub.write_text("class Stub:\n    async def defaulted(self = ...) -> None: ...\n",
                        encoding="utf-8")
        fake, seen = self.fake_tools({"pyright": {"stdout": json.dumps({
            "generalDiagnostics": [{"file": str(source), "message": "Native issue",
                                    "severity": "error", "rule": "reportReturnType",
                                    "range": {"start": {"line": 0, "character": 0}}}]}), "code": 1}})

        def capture(argv, cwd, timeout):
            if str(ERYX / "_method_decorators_worker.py") in argv:
                seen.append("method-decorators")
                return _common.capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        for selection in ("", "tests/test_value.py"):
            with self.subTest(selection=selection):
                report = check.run_checks(self.root, lcom_files=selection, capture_fn=capture,
                                          versions_fn=lambda root: ({}, []))
                self.assertEqual(report["exit_code"], 1, report)
                self.assertEqual([(d["path"], d["symbol"]) for d in report["diagnostics"]
                                  if d["rule"] == "MISSING_INSTANCE_RECEIVER"], [
                    ("src/demo/domain/api.py", "Example.missing"),
                    ("src/demo/domain/stubs.pyi", "Stub.defaulted")])
                self.assertIn("reportReturnType", {d["rule"] for d in report["diagnostics"]})
                self.assertEqual(next(t for t in report["tools"] if t["name"] == "method-decorators")
                                 ["status"], "violations")
                self.assertIn("pyright", seen)

    def test_method_conversions_report_through_check_with_skipped_lcom(self):
        source = self.root / "src/demo/domain/api.py"
        source.write_text("class Example:\n    def make(self): pass\n"
                          "Example.make = staticmethod(Example.make)\n", encoding="utf-8")
        (self.root / "src/demo/domain/stubs.pyi").write_text(
            "class Stub:\n    def build(self): ...\n    build = classmethod(build)\n",
            encoding="utf-8")
        fake, seen = self.fake_tools()

        def capture(argv, cwd, timeout):
            if str(ERYX / "_method_decorators_worker.py") in argv:
                seen.append("method-decorators")
                return _common.capture(argv, cwd, timeout)
            return fake(argv, cwd, timeout)

        report = check.run_checks(self.root, lcom_files="", capture_fn=capture,
                                  versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 1, report)
        self.assertEqual([(d["path"], d["symbol"], d["rule"])
                          for d in report["diagnostics"]], [
            ("src/demo/domain/api.py", "Example.make", "NON_INSTANCE_METHOD"),
            ("src/demo/domain/stubs.pyi", "Stub.build", "NON_INSTANCE_METHOD")])
        self.assertTrue(all(not d["is_infrastructure_error"] for d in report["diagnostics"]))
        self.assertEqual(next(t for t in report["tools"] if t["name"] == "method-decorators")
                         ["status"], "violations")
        self.assertIn("pyright", seen)

    def test_failed_lcom4_job_does_not_claim_analysis(self):
        fake, _ = self.fake_tools({"lcom4": {"stdout": "not JSON", "code": 1}})
        report = check.run_checks(self.root, lcom_files="src/demo/domain/api.py",
                                  capture_fn=fake, versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["coverage"]["lcom4"]["selected_files"], ["src/demo/domain/api.py"])
        self.assertEqual(report["coverage"]["lcom4"]["analyzed_files"], [])
        self.assertEqual(next(t for t in report["tools"] if t["name"] == "lcom4")["status"], "error")

    def test_encapsulated_only_failure_keeps_native_diagnostics_and_full_job_status(self):
        native = {"stderr": "src/demo/domain/api.py:1: DOC101: Missing argument\n", "code": 1}
        failures = ({"stdout": "not JSON", "code": 0},
                    {"stdout": "", "code": None, "error": "TOOL_TIMEOUT"})
        for failure in failures:
            with self.subTest(failure=failure):
                fake, seen = self.fake_tools({"encapsulated-only": failure, "pydoclint": native})
                report = check.run_checks(self.root, lcom_files="", capture_fn=fake,
                                          versions_fn=lambda root: ({}, []))
                self.assertEqual((report["status"], report["exit_code"]), ("error", 2))
                self.assertIn("DOC101", {d["rule"] for d in report["diagnostics"]})
                self.assertTrue(any(d["is_infrastructure_error"] for d in report["diagnostics"]
                                    if d["tool"] == "encapsulated-only"))
                self.assertEqual(next(t for t in report["tools"] if t["name"] == "encapsulated-only")
                                  ["status"], "error")
                self.assertEqual(len(seen), 13)
                import jsonschema
                jsonschema.validate(report, _common.read_json(ERYX / "report.schema.json"))

    def test_invalid_lcom4_path_errors_while_other_jobs_run(self):
        for requested in ("src/missing.py", "src/demo/domain/api.pyi", "../outside.py",
                          "/tmp/outside.py", "src/demo/generated/skip.py", "src/demo/domain/../domain/api.py",
                          "tests/missing.py", " "):
            with self.subTest(requested=requested):
                fake, seen = self.fake_tools()
                report = check.run_checks(self.root, lcom_files=requested, capture_fn=fake,
                                          versions_fn=lambda root: ({}, []))
                self.assertEqual(report["exit_code"], 2, report)
                self.assertEqual(len(seen), 13)
                self.assertNotIn("lcom4", seen)
                self.assertEqual(next(t for t in report["tools"] if t["name"] == "lcom4")["status"], "error")
                self.assertIn("INVALID_LCOM_FILES", {d["rule"] for d in report["diagnostics"]})
                self.assertEqual(report["coverage"]["lcom4"]["analyzed_files"], [])

    def test_no_fail_fast_and_error_precedence(self):
        fake, seen = self.fake_tools({"pyright": {"stdout": "broken", "code": 2},
                                     "pydoclint": {"stderr": "src/demo/domain/api.py:1: DOC101: Missing argument\n", "code": 1}})
        report = check.run_checks(self.root, capture_fn=fake, versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(len(seen), 14)
        self.assertTrue(any(d["rule"] == "DOC101" for d in report["diagnostics"]))

    def test_violation_code_one(self):
        fake, seen = self.fake_tools({"pydoclint": {"stderr": "src/demo/domain/api.py:1: DOC101: Missing argument\n", "code": 1}})
        report = check.run_checks(self.root, capture_fn=fake, versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(len(seen), 14)

    def test_not_initialized_not_green(self):
        (self.root / ".eryx/architecture.ready.json").unlink()
        fake, seen = self.fake_tools()
        report = check.run_checks(self.root, capture_fn=fake, versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(len(seen), 12)
        self.assertEqual(sum(t["status"] == "blocked" for t in report["tools"]), 2)

    def test_unapplied_limits_still_run_tools(self):
        path = self.root / ".eryx/readability.toml"
        path.write_text(path.read_text().replace("max_module_lines = 400", "max_module_lines = 410"))
        fake, seen = self.fake_tools()
        report = check.run_checks(self.root, capture_fn=fake, versions_fn=lambda root: ({}, []))
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(len(seen), 14)
        self.assertTrue(any(d["rule"] == "READABILITY_NOT_APPLIED" for d in report["diagnostics"]))

    def test_short_output_does_not_truncate_json(self):
        fake, _ = self.fake_tools({"pydoclint": {"stderr": "src/demo/domain/api.py:1: DOC101: Missing argument\n", "code": 1}})
        report = check.run_checks(self.root, capture_fn=fake, versions_fn=lambda root: ({}, []))
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            check.display(report, False, 0)
        self.assertIn("1 more diagnostics", output.getvalue())
        self.assertEqual(len(report["diagnostics"]), 1)


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
