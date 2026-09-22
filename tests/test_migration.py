"""Consumer migration invariants, including late-failure transactions."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common
import _policy
import install
from test_infrastructure import CATALOG, ERYX, create_repo, native_result


def managed_bytes(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and not any(part in {"logs", "cache", "reports", ".venv", ".git"}
                                          for part in path.relative_to(root).parts)}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="eryx-migration-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        create_repo(self.root)

    def fake_uv(self, argv, cwd, timeout):
        if str(ERYX / "_audit.py") in argv:
            _policy.configure(cwd, initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)
            _common.write_json(cwd / ".eryx/logs/install-probes/result.json",
                               {"versions": {}, "smoke_tests": {"native": "mocked"}})
        return native_result(argv)

    def test_migrate_legacy_policy_and_multi_package_architecture(self):
        path = self.root / "pyproject.toml"
        document = tomlkit.parse(path.read_text())
        document["tool"]["pylint"]["design"]["ignored-parents"].append("foreign.Framework")
        document["tool"]["pyright"]["stubPath"] = "src/stubs"
        document["tool"]["uv"] = {"sources": {"foreign": {"git": "https://example.org/repo", "rev": "abc"}},
                                  "index": [{"name": "custom", "url": "https://example.org/simple", "explicit": True}],
                                  "dependency-metadata": [{"name": "foreign", "requires-dist": []}]}
        path.write_text(tomlkit.dumps(document))
        _common.write_json(self.root / "pyrightconfig.json", document["tool"]["pyright"].unwrap())
        (self.root / "src/other").mkdir()
        (self.root / "src/other/__init__.py").write_text("")
        limits = self.root / ".eryx/readability.toml"
        limits.write_text(limits.read_text().replace("max_complexity = 8", "max_complexity = 6"))
        _policy.configure(self.root)
        original_tach = (self.root / "tach.toml").read_bytes()
        original_marker = (self.root / ".eryx/architecture.ready.json").read_bytes()
        (self.root / ".eryx").rename(self.root / "quality")
        with patch.object(install, "capture", self.fake_uv):
            install.install(self.root, ERYX, progress=False)
        actual = _common.load_toml(path)
        self.assertIn("foreign.Framework", actual["tool"]["pylint"]["design"]["ignored-parents"])
        self.assertEqual(actual["tool"]["pyright"]["stubPath"], "src/stubs")
        for key in ("sources", "index", "dependency-metadata"):
            self.assertEqual(actual["tool"]["uv"][key], document["tool"]["uv"][key])
        self.assertEqual(_policy.read_limits(self.root)["production"]["max_complexity"], 6)
        self.assertEqual((self.root / "tach.toml").read_bytes(), original_tach)
        self.assertEqual((self.root / ".eryx/architecture.ready.json").read_bytes(), original_marker)
        tests = _common.load_toml(self.root / ".eryx/pylint-tests.toml")
        self.assertIn("foreign.Framework", tests["tool"]["pylint"]["design"]["ignored-parents"])
        self.assertNotIn("garak.probes.base.Probe", _policy.IGNORED_FRAMEWORK_PARENTS)

    def test_existing_state_wins_without_installation_marker(self):
        legacy = self.root / "quality"
        legacy.mkdir()
        (legacy / "readability.toml").write_text("invalid legacy limits")
        before = (self.root / ".eryx/readability.toml").read_bytes()
        install.initialize_state(self.root, ERYX)
        self.assertEqual((self.root / ".eryx/readability.toml").read_bytes(), before)

    def test_late_failure_restores_all_managed_files_and_modes(self):
        original = managed_bytes(self.root)
        path = self.root / "pyproject.toml"
        path.chmod(0o640)

        def failing_audit(argv, cwd, timeout):
            self.fake_uv(argv, cwd, timeout)
            if str(ERYX / "_audit.py") in argv:
                path.write_text("partially written config")
                _common.write_json(cwd / ".eryx/policy-notes.json", {"partial": True})
                return native_result(argv, code=2, stderr="intentional probe failure")
            return native_result(argv)

        with patch.object(install, "capture", failing_audit), self.assertRaises(_common.EryxError):
            install.install(self.root, ERYX, progress=False)
        self.assertEqual(managed_bytes(self.root), original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_selected_extras_are_retained_on_reinstall(self):
        path = self.root / "pyproject.toml"
        doc = tomlkit.parse(path.read_text())
        doc["project"]["optional-dependencies"] = {"keep": ["six==1.17.0"]}
        path.write_text(tomlkit.dumps(doc))
        with patch.object(install, "capture", side_effect=self.fake_uv) as capture:
            install.install(self.root, ERYX, extras=["keep"], progress=False)
            install.install(self.root, ERYX, progress=False)
        commands = [call.args[0] for call in capture.call_args_list]
        for command in commands:
            if command[1] in {"sync", "run"}:
                self.assertEqual(command[command.index("--extra") + 1], "keep")
        self.assertEqual(_common.read_json(self.root / ".eryx/installation.json")["extras"], ["keep"])

    def test_unknown_extra_is_rejected_without_writes(self):
        before = managed_bytes(self.root)
        with self.assertRaisesRegex(_common.EryxError, "Unknown project extra"):
            install.install(self.root, ERYX, extras=["missing"], progress=False)
        self.assertEqual(managed_bytes(self.root), before)
