"""Real wheel, CLI and whole-run integration over an isolated public fixture."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, override
import unittest
from unittest.mock import patch

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from offline_fixtures import create_repo, fake_tools
from quard._common import capture, read_json
from quard.check_runner.native_check_runner import NativeCheckRunner
from quard.check_runner.runner_utils import binary


class TestRustCloneEngine(unittest.TestCase):
    @staticmethod
    def versions(_root: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
        return {}, []

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cargo = shutil.which("cargo") or "/tmp/opencode/cargo/bin/cargo"
        if not Path(cargo).is_file():
            raise unittest.SkipTest("Rust developer toolchain is required to build the real wheel")
        cls.tmp = TemporaryDirectory(prefix="quard-real-wheel-", dir="/tmp/opencode")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.base = Path(cls.tmp.name)
        env = os.environ.copy()
        env["PATH"] = str(Path(cargo).parent) + os.pathsep + env.get("PATH", "")
        if cargo.startswith("/tmp/opencode/"):
            env["CARGO_HOME"] = "/tmp/opencode/cargo"
            env["RUSTUP_HOME"] = "/tmp/opencode/rustup"
        env["CARGO_TARGET_DIR"] = str(cls.base / "target")
        subprocess.run(["uvx", "--from", "maturin==1.9.6", "maturin", "build",
                        "--locked", "--release", "--manifest-path",
                        str(QUARD / "rust/quard-clones/Cargo.toml"), "--interpreter",
                        sys.executable, "--out", str(cls.base)], env=env, check=True,
                       capture_output=True, text=True, timeout=300)
        wheels = list(cls.base.glob("quard_clones-*.whl"))
        if len(wheels) != 1:
            raise AssertionError(f"Expected one real wheel, got {wheels}")
        cls.python = cls.base / "venv/bin/python"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(cls.base / "venv")],
                       check=True, capture_output=True, text=True)
        subprocess.run(["uv", "pip", "install", "--python", str(cls.python), str(wheels[0])],
                       check=True, capture_output=True, text=True)
        installed = subprocess.run([str(cls.python), "-c",
                                    "import quard_clones; print(dict(quard_clones.versions()))"],
                                   check=True, capture_output=True, text=True)
        assert "cpd-core" in installed.stdout

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path(self.enterContext(TemporaryDirectory(
            prefix="quard-wheel-consumer-", dir="/tmp/opencode")))
        create_repo(self.root)

    def run_report(self) -> dict[str, Any]:
        fake, _ = fake_tools()

        def dispatch(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
            if Path(argv[1]).name == "_clone_engine_worker.py":
                return capture([str(self.python), *argv[1:]], cwd, timeout)
            return fake(argv, cwd, timeout)

        with patch.dict(os.environ, {"QUARD_CLONE_ENGINE": "rust"}):
            return NativeCheckRunner().run(self.root, lcom_files="", serial=True,
                                           timeout=60, capture_fn=dispatch,
                                           versions_fn=self.versions)

    def test_cli_parity_original_scope_and_locations(self) -> None:
        text = ("def repeat(value):\n    first = value + 17\n    second = first * 3\n"
                "    third = second - 4\n    fourth = third / 2\n    return fourth + 10\n")
        (self.root / "src/demo/first.py").write_text(text, encoding="utf-8")
        (self.root / "tests/second.py").write_text(text, encoding="utf-8")
        (self.root / "src/demo/companion.pyi").write_text(text, encoding="utf-8")
        excluded = self.root / "src/demo/generated"
        excluded.mkdir()
        (excluded / "hidden.py").write_text(text, encoding="utf-8")
        for clean in (False, True):
            with self.subTest(clean=clean):
                if clean:
                    (self.root / "tests/second.py").unlink()
                    (self.root / "src/demo/first.py").write_text("unique = object()\n",
                                                                encoding="utf-8")
                report = self.run_report()
                exact = next(job for job in report["tools"] if job["name"] == "jscpd")
                self.assertEqual((report["exit_code"], exact["status"], exact["exit_code"]),
                                 (0, "passed", 0) if clean else (1, "violations", 1),
                                 report["diagnostics"])
                native = read_json(self.root / exact["raw_output"]["native_json"])
                paths = read_json(self.root / report["report_path"].replace("/report.json", "") /
                                  "clone-engine-files.json")["paths"]
                output = self.root / ".quard/cli"
                output.mkdir(exist_ok=True)
                command = [binary("jscpd"), "--config", str(self.root / report["report_path"].replace(
                    "/report.json", "") / "jscpd-config.json"), "--no-gitignore", "--absolute",
                    "--kind", "exact", "--mode", "strict", "--min-lines", "3", "--min-tokens",
                    "1", "--fail-on-empty", "--reporters", "json", "--output", str(output),
                    "--exit-code", "1", *paths]
                cli = capture(command, self.root, 60)
                self.assertEqual(cli["exit_code"], exact["exit_code"], cli)
                original = read_json(output / "jscpd-report.json")
                for key in ("sources", "clones", "lines", "tokens"):
                    self.assertEqual(native["statistics"]["total"][key],
                                     original["statistics"]["total"][key])
                for key in ("kind", "lines", "tokens", "fragment", "firstFile", "secondFile"):
                    self.assertEqual([item[key] for item in native["duplicates"]],
                                     [item[key] for item in original["duplicates"]], key)
                self.assertEqual({d["path"] for d in report["diagnostics"] if d["tool"] == "jscpd"},
                                 set() if clean else {"src/demo/first.py", "tests/second.py"})

    def test_malformed_native_output_preserves_independent_results(self) -> None:
        text = "def repeat(value):\n    first = value + 17\n    second = first * 3\n    third = second - 4\n"
        (self.root / "src/demo/first.py").write_text(text, encoding="utf-8")
        (self.root / "tests/second.py").write_text(text, encoding="utf-8")
        fake, _ = fake_tools({"pydoclint": {"stderr": "src/demo/first.py:1: DOC101: Native issue\n",
                                           "code": 1}})
        with patch.dict(os.environ, {"QUARD_CLONE_ENGINE": "rust"}):
            report = NativeCheckRunner().run(self.root, lcom_files="", serial=True,
                                              timeout=30, capture_fn=fake,
                                              versions_fn=self.versions)
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(next(j for j in report["tools"] if j["name"] == "jscpd")["status"], "error")
        self.assertIn("DOC101", {d["rule"] for d in report["diagnostics"]})

    def test_missing_wheel_is_an_infrastructure_error(self) -> None:
        clean = self.base / "without-wheel"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(clean)],
                       check=True, capture_output=True, text=True)
        fixture = self.root / "src/demo/first.py"
        fixture.write_text("one = 1\ntwo = one + 2\nthree = two * 3\n", encoding="utf-8")
        manifest = self.root / ".quard/clone-manifest.json"
        manifest.write_text(json.dumps({"paths": ["src/demo/first.py"], "min_lines": 3}),
                            encoding="utf-8")
        result = capture([str(clean / "bin/python"), str(QUARD / "_clone_engine_worker.py"),
                          "--manifest", str(manifest), "--output",
                          str(self.root / ".quard/native.json")], self.root, 30)
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("CLONE_ENGINE_ERROR", result["stderr"])
        self.assertFalse((self.root / ".quard/native.json").exists())

    def test_same_file_clone_and_native_mapping_corruption(self) -> None:
        text = ("def repeat(value):\n    first = value + 17\n    second = first * 3\n"
                "    third = second - 4\n    fourth = third / 2\n    return fourth + 10\n")
        (self.root / "src/demo/first.py").write_text(text + "\n# separation\n" + text,
                                                      encoding="utf-8")
        report = self.run_report()
        exact = next(job for job in report["tools"] if job["name"] == "jscpd")
        self.assertEqual((exact["status"], exact["exit_code"]), ("violations", 1),
                         report["diagnostics"])
        self.assertEqual({d["path"] for d in report["diagnostics"] if d["tool"] == "jscpd"},
                         {"src/demo/first.py"})
        native_path = self.root / exact["raw_output"]["native_json"]
        raw = read_json(native_path)
        self.assertEqual(raw["statistics"]["total"]["clones"], len(raw["duplicates"]))
        output = self.root / ".quard/same-file-cli"
        output.mkdir()
        run_dir = self.root / report["report_path"].replace("/report.json", "")
        paths = read_json(run_dir / "clone-engine-files.json")["paths"]
        cli = capture([binary("jscpd"), "--config", str(run_dir / "jscpd-config.json"),
                       "--no-gitignore", "--absolute", "--kind", "exact", "--mode", "strict",
                       "--min-lines", "3", "--min-tokens", "1", "--fail-on-empty", "--reporters",
                       "json", "--output", str(output), "--exit-code", "1", *paths], self.root, 30)
        self.assertEqual(cli["exit_code"], 1)
        expected = read_json(output / "jscpd-report.json")
        for key in ("kind", "lines", "tokens", "fragment", "firstFile", "secondFile"):
            self.assertEqual([item[key] for item in raw["duplicates"]],
                             [item[key] for item in expected["duplicates"]], key)
        raw["duplicates"][0]["fragment"] = "invented source"
        from quard.report_utils import normalize
        bad = self.root / "altered.json"
        bad.write_text(json.dumps(raw), encoding="utf-8")
        invalid = normalize("jscpd", {"exit_code": 1, "stdout": "", "stderr": "", "error": None},
                            self.root, bad, paths, True)
        self.assertEqual(invalid["status"], "error")
        self.assertTrue(any(d["is_infrastructure_error"] for d in invalid["diagnostics"]))
