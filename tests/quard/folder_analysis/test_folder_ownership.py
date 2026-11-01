"""End-to-end folder ownership checks across production, tests and stubs."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import override
import unittest

import tomlkit

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))

from quard._common import write_json
from quard._policy import discover_files, read_scope
from quard.policy_utils import base_pylint, pyright_config
from quard.architecture_draft.generated_selection_utils import IDENTITY, LEGACY_IDENTITY
from quard.check import execute_job, make_jobs
from quard._class_contracts_worker import Contracts as LegacyContracts
from quard._contract_folders_worker import FolderOwnership as LegacyFolderOwnership
from quard._lcom4_worker import Classes as LegacyClasses
from quard._lcom4_worker import Declarations as LegacyCohesionDeclarations
from quard._lcom4_worker import MethodReferences as LegacyMethodReferences
from quard._method_decorators_worker import Declarations as LegacyMethodDeclarations
from quard._test_placement_worker import TestPlacement as LegacyTestPlacement
from quard.contract_analysis.contracts import Contracts
from quard.contract_analysis.folder_ownership import FolderOwnership
from quard.contract_analysis.test_placement import TestPlacement
from quard.cohesion_analysis.classes import Classes
from quard.cohesion_analysis.declarations import Declarations as CohesionDeclarations
from quard.cohesion_references.method_references import MethodReferences
from quard.method_analysis.declarations import Declarations


class TestFolderOwnership(unittest.TestCase):
    def test_delegate_super_override_and_invalid_forwarding(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def run(self, value: int) -> int: ...\n")
        for label, constructor, decorator, result in (
                ("plain", "", "", "value"),
                ("super", "super().__init__()\n        ", "@alias\n    ", "value"),
                ("changed", "", "", "value + 1"),
                ("args", "super().__init__(value)\n        ", "", "value"),
                ("unrelated", "", "@property\n    ", "value")):
            self.put(f"src/pkg/port/envelopes/{label}.py",
                     "from pkg.port.port import Port\n"
                     + ("from typing import override as alias\n" if decorator else "")
                     + f"class {label.title()}(Port):\n"
                     + "    def __init__(self, delegate: Port) -> None:\n        "
                     + constructor + "self.delegate = delegate\n"
                     + f"    {decorator}def run(self, value: int) -> int:\n"
                     + f"        return self.delegate.run({result})\n")
        result, raw = self.run_worker()
        self.assertEqual({item["symbol"] for item in raw["diagnostics"]
                          if item["rule"] == "ENVELOPE_DELEGATION"},
                         {"Changed", "Args", "Unrelated"}, "valid forwarding was rejected")

    def test_override_property_accessors_with_matching_stub(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    @property\n    def value(self) -> int: ...\n"
                 "    @value.setter\n    def value(self, value: int) -> None: ...\n"
                 "    @value.deleter\n    def value(self) -> None: ...\n")
        self.put("src/pkg/port/envelopes/value.py",
                 "from typing_extensions import override as alias\n"
                 "from pkg.port.port import Port\nclass Value(Port):\n"
                 "    def __init__(self, delegate: Port) -> None:\n"
                 "        super().__init__()\n        self.delegate = delegate\n"
                 "    @alias\n    @property\n    def value(self) -> int:\n"
                 "        return self.delegate.value\n"
                 "    @value.setter\n    @alias\n    def value(self, value: int) -> None:\n"
                 "        self.delegate.value = value\n"
                 "    @alias\n    @value.deleter\n    def value(self) -> None:\n"
                 "        del self.delegate.value\n")
        self.put("src/pkg/port/envelopes/value.pyi",
                 "from typing_extensions import override as alias\n"
                 "from pkg.port.port import Port\nclass Value(Port):\n"
                 "    @alias\n    @property\n    def value(self) -> int: ...\n"
                 "    @value.setter\n    @alias\n    def value(self, value: int) -> None: ...\n"
                 "    @alias\n    @value.deleter\n    def value(self) -> None: ...\n")
        result, raw = self.run_worker()
        self.assertEqual(raw["diagnostics"], [], "override accessors lost proof")

    def test_checkpoints_are_excluded_without_hiding_other_directories(self):
        for path in ("src/pkg/envelopes/nested/good.py", "src/pkg/.hidden/good.py",
                     "src/pkg/.ipynb_checkpoints/bad.py", "tests/.ipynb_checkpoints/bad.py"):
            self.put(path, "VALUE = 1\n")
        files = discover_files(self.root, read_scope(self.root))
        self.assertEqual((files["production"], files["excluded_directories"]),
                         (["src/pkg/.hidden/good.py", "src/pkg/envelopes/nested/good.py"],
                          ["src/pkg/.ipynb_checkpoints/", "tests/.ipynb_checkpoints/"]),
                         "checkpoint files entered the discovered scope")

    def test_native_pylint_exempts_only_proven_envelopes(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def run(self, value: int) -> int: ...\n")
        self.put("src/pkg/port/envelopes/proven.py", "from pkg.port.port import Port\n"
                 "class Proven(Port):\n    def __init__(self, delegate: Port) -> None:\n"
                 "        self.delegate = delegate\n    def run(self, value: int) -> int:\n"
                 "        return self.delegate.run(value)\n")
        self.put("src/pkg/port/envelopes/assembly.py",
                 "from pkg.port.envelopes.proven import Proven\nclass Assembly(Proven):\n"
                 "    def __init__(self, delegate: Proven) -> None:\n"
                 "        super().__init__(delegate)\n")
        self.put("src/pkg/port/envelopes/invalid.py", "from pkg.port.port import Port\n"
                 "class Invalid(Port):\n    def run(self, value: int) -> int:\n"
                 "        return value\n")
        self.put("src/pkg/port/ordinary.py", "class Ordinary:\n"
                 "    def run(self) -> int:\n        return 1\n")
        files = discover_files(self.root, read_scope(self.root))["production"]
        config = self.root / "pylint.toml"
        config.write_text(tomlkit.dumps({"tool": {"pylint": base_pylint([], read_scope(self.root))}}))
        manifest = self.root / "files.json"
        manifest.write_text(json.dumps(files))
        output = self.root / "output.json"
        result = subprocess.run([sys.executable, str(QUARD / "_pylint_worker.py"),
                                 "--manifest", str(manifest), "--envelopes-manifest", str(manifest),
                                 "--config", str(config), "--output", str(output), "--generated"],
                                cwd=self.root, capture_output=True, text=True, check=False)
        self.assertTrue(output.exists(), result.stderr)
        findings = json.loads(output.read_text())["messages"]
        self.assertEqual({item["obj"] for item in findings if item["messageId"] == "R0903"},
                         {"Invalid", "Ordinary"}, "R0903 exempted an unproved class")

    def test_native_pylint_reuses_folder_envelope_evidence(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def run(self) -> int: ...\n")
        self.put("src/pkg/port/envelopes/proven.py", "from pkg.port.port import Port\n"
                 "class Proven(Port):\n    def __init__(self, delegate: Port) -> None:\n"
                 "        self.delegate = delegate\n    def run(self) -> int:\n"
                 "        return self.delegate.run()\n")
        self.put("src/pkg/port/envelopes/invalid.py", "from pkg.port.port import Port\n"
                 "class Invalid(Port):\n    def run(self) -> int:\n        return 1\n")
        files = discover_files(self.root, read_scope(self.root))["production"]
        evidence = self.run_dir / "envelope-evidence.json"
        from quard.folder_analysis.folder_utils import analyze
        analyze(self.root, files, evidence=evidence)
        config = self.root / "pylint.toml"
        config.write_text(tomlkit.dumps({"tool": {"pylint": base_pylint([], read_scope(self.root))}}))
        manifest = self.run_dir / "files.json"
        manifest.write_text(json.dumps(files))
        findings = []
        for shared in (False, True):
            output = self.run_dir / f"pylint-{shared}.json"
            subprocess.run([sys.executable, str(QUARD / "_pylint_worker.py"),
                            "--manifest", str(manifest), "--envelopes-manifest", str(manifest),
                            "--config", str(config), "--output", str(output), "--generated",
                            *(["--envelope-evidence", str(evidence)] if shared else [])],
                           cwd=self.root, capture_output=True, text=True, check=False)
            findings.append(json.loads(output.read_text())["messages"])
        self.assertEqual((findings[0] == findings[1], json.loads(evidence.read_text())),
                         (True, [["src/pkg/port/envelopes/proven.py", 2]]),
                         "shared proof changed Pylint diagnostics or lost a valid Envelope")

    def test_native_pylint_restores_r0903_without_contract_folders(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def run(self, value: int) -> int: ...\n")
        self.put("src/pkg/port/envelopes/proven.py", "from pkg.port.port import Port\n"
                 "class Proven(Port):\n    def __init__(self, delegate: Port) -> None:\n"
                 "        self.delegate = delegate\n    def run(self, value: int) -> int:\n"
                 "        return self.delegate.run(value)\n")
        files = discover_files(self.root, read_scope(self.root))["production"]
        config = self.root / "pylint.toml"
        config.write_text(tomlkit.dumps({"tool": {"pylint": base_pylint([], read_scope(self.root))}}))
        manifest = self.root / "files.json"
        manifest.write_text(json.dumps(files))
        output = self.root / "output.json"
        subprocess.run([sys.executable, str(QUARD / "_pylint_worker.py"), "--manifest",
                        str(manifest), "--config", str(config), "--output", str(output),
                        "--generated"], cwd=self.root, capture_output=True, text=True, check=False)
        self.assertIn("Proven", {item["obj"] for item in json.loads(output.read_text())["messages"]
                                 if item["messageId"] == "R0903"},
                      "disabled contract folders retained the R0903 exception")

    def test_native_pyright_rules_keep_outside_scope(self):
        self.put("src/pkg/parent.py", "class Parent:\n"
                 "    def __init__(self) -> None: super().__init__()\n"
                 "    def run(self) -> int: return 1\n")
        for path in ("src/pkg/ordinary.py", "src/pkg/envelopes/nested/inside.py"):
            self.put(path, "from pkg.parent import Parent\nclass Child(Parent):\n"
                     "    def __init__(self) -> None: pass\n"
                     "    def run(self) -> int: return 2\n")
        files = discover_files(self.root, read_scope(self.root))
        (self.root / "pyrightconfig.json").write_text(json.dumps(pyright_config(
            read_scope(self.root), files, True)))
        result = subprocess.run([sys.executable, "-m", "pyright", "--project",
                                 str(self.root / "pyrightconfig.json"), "--pythonpath", sys.executable,
                                 "--outputjson"], cwd=self.root, capture_output=True, text=True,
                                check=False)
        findings = json.loads(result.stdout)["generalDiagnostics"]
        self.assertEqual({(Path(item["file"]).name, item["rule"]) for item in findings
                          if item.get("rule") in {"reportMissingSuperCall", "reportImplicitOverride"}},
                         {("ordinary.py", "reportMissingSuperCall"),
                         ("ordinary.py", "reportImplicitOverride")},
                         "Pyright's native exceptions escaped the Envelope subtree")

    def test_native_pyright_restores_rules_without_contract_folders(self):
        self.put("src/pkg/parent.py", "class Parent:\n"
                 "    def __init__(self) -> None: super().__init__()\n"
                 "    def run(self) -> int: return 1\n")
        self.put("src/pkg/envelopes/nested/inside.py", "from pkg.parent import Parent\n"
                 "class Child(Parent):\n    def __init__(self) -> None: pass\n"
                 "    def run(self) -> int: return 2\n")
        files = discover_files(self.root, read_scope(self.root))
        (self.root / "pyrightconfig.json").write_text(json.dumps(pyright_config(
            read_scope(self.root), files, False)))
        result = subprocess.run([sys.executable, "-m", "pyright", "--project",
                                 str(self.root / "pyrightconfig.json"), "--pythonpath", sys.executable,
                                 "--outputjson"], cwd=self.root, capture_output=True, text=True,
                                check=False)
        findings = json.loads(result.stdout)["generalDiagnostics"]
        self.assertIn("reportImplicitOverride", {item.get("rule") for item in findings},
                      "disabled contract folders retained the Pyright exception")

    def test_migrated_production_classes_and_compatibility_imports(self):
        self.assertEqual(self.run_worker("class-contracts")[0]["exit_code"], 0)
        self.assertIs(LegacyContracts, Contracts)
        self.assertIs(LegacyFolderOwnership, FolderOwnership)
        self.assertIs(LegacyTestPlacement, TestPlacement)
        self.assertIs(LegacyClasses, Classes)
        self.assertIs(LegacyCohesionDeclarations, CohesionDeclarations)
        self.assertIs(LegacyMethodReferences, MethodReferences)
        self.assertIs(LegacyMethodDeclarations, Declarations)

        from quard._class_contracts_worker import analyze as contracts_analyze
        from quard._contract_folders_worker import analyze as folders_analyze
        from quard._outer_classes_worker import analyze as outer_analyze

        paths = sorted(str(path.relative_to(QUARD)) for folder in (
            "contract_analysis", "folder_analysis", "test_placement_analysis",
            "method_analysis", "cohesion_analysis", "cohesion_references")
            for path in (QUARD / "src/quard" / folder).glob("*.py"))
        paths.extend(f"src/quard/{worker}.py" for worker in (
            "_class_contracts_worker", "_contract_folders_worker", "_lcom4_worker",
            "_method_decorators_worker", "_test_placement_worker"))
        for analyze in (contracts_analyze, folders_analyze, outer_analyze):
            diagnostics, error = analyze(QUARD, paths)
            self.assertIsNone(error)
            self.assertEqual([], diagnostics)

    def test_migrated_helpers_keep_worker_interfaces_and_placement(self):
        from quard._class_contracts_worker import analyze as contracts_analyze
        from quard._contract_folders_worker import analyze as folders_analyze
        from quard._lcom4_worker import analyze as cohesion_analyze
        from quard._test_placement_worker import analyze as placement_analyze
        from quard.cohesion_analysis.cohesion_worker_utils import analyze as cohesion_helper
        from quard.contract_analysis.contract_worker_utils import analyze as contract_helper
        from quard.folder_analysis.folder_utils import analyze as folder_helper
        from quard.free_function_placement_utils import analyze as free_functions
        from quard.test_placement_analysis.placement_utils import analyze as placement_helper

        self.assertIs(contracts_analyze, contract_helper)
        self.assertIs(folders_analyze, folder_helper)
        self.assertIs(cohesion_analyze, cohesion_helper)
        self.assertIs(placement_analyze, placement_helper)

        paths = sorted(str(path.relative_to(QUARD)) for folder in (
            "contract_analysis", "folder_analysis", "test_placement_analysis",
            "cohesion_analysis", "cohesion_references")
            for path in (QUARD / "src/quard" / folder).glob("*.py"))
        diagnostics, error = free_functions(QUARD, paths)
        self.assertIsNone(error)
        self.assertEqual([], diagnostics)

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.temp: TemporaryDirectory[str] = TemporaryDirectory(prefix="quard-folders-")
        self.addCleanup(self.temp.cleanup)
        self.root: Path = Path(self.temp.name)
        self.run_dir: Path = self.root / ".quard/reports/run"

    @override
    def setUp(self):
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / ".quard").mkdir()
        (self.root / ".quard/config.toml").write_text(
            (QUARD / "defaults/config.toml").read_text().split("[scope]")[0]
            + '[scope]\nexclude_directory_names = ["generated"]\n'
            'test_directory_names = ["tests"]\n', encoding="utf-8")
        self.run_dir.mkdir(parents=True)

    def put(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def run_worker(self, name="contract-folders"):
        files = discover_files(self.root, read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False, [])
                   if job["name"] == name)
        result = execute_job(job, self.root, self.run_dir, 30)
        text = (self.root / result["raw_output"]["stdout"]).read_text(encoding="utf-8")
        self.assertTrue(text, (result, (self.root / result["raw_output"]["stderr"])
                               .read_text(encoding="utf-8")))
        raw = json.loads(text)
        return result, raw

    def test_valid_combined_contract_helpers_stubs_and_external_bases(self):
        self.put("src/pkg/http_port/http_port.py", "from typing import Protocol\n"
                 "class HTTPPort(Protocol):\n    def fetch(self): ...\n")
        self.put("src/pkg/write_port/write_port.pyi", "from typing import Protocol\n"
                 "class WritePort(Protocol):\n    def write(self): ...\n")
        self.put("src/pkg/combined_port/combined_port.py", "from typing import Protocol\n"
                 "from pkg.http_port.http_port import HTTPPort\n"
                 "from pkg.write_port.write_port import WritePort\n"
                 "class CombinedPort(HTTPPort, WritePort, Protocol): pass\n")
        self.put("src/pkg/http_port/impl.py", "from .http_port import HTTPPort\n"
                 "class Impl(HTTPPort):\n    def fetch(self): pass\n")
        self.put("src/pkg/http_port/impl.pyi", "from .http_port import HTTPPort\n"
                 "class Impl(HTTPPort):\n    def fetch(self): ...\n")
        self.put("src/pkg/http_port/helpers.py", "def assist(): return 1\n")
        self.put("src/pkg/http_port/nested/aux.py", "from pkg.http_port.impl import Impl\n"
                 "class Auxiliary(Impl): pass\n")
        self.put("src/pkg/http_port/external.py", "from pathlib import Path\n"
                 "from .impl import Impl\nclass Extended(Impl, Path): pass\n")
        self.put("src/pkg/combined_port/impl.py", "from .combined_port import CombinedPort\n"
                 "from pkg.http_port.impl import Impl\n"
                 "class Both(CombinedPort, Impl):\n    def write(self): pass\n")
        self.put("src/pkg/combined_port/child.py", "from .impl import Both\n"
                 "class Child(Both): pass\n")
        self.put("tests/pkg/combined_port/test_child.py", "class TestChild: pass\n")
        self.put("tests/pkg/http_port/test_impl.py", "from pkg.http_port.impl import Impl\n"
                 "class TestImpl(Impl): pass\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("passed", 0, None), result["diagnostics"])
        self.assertEqual(result["diagnostics"], [])
        placement, _ = self.run_worker("test-placement")
        self.assertEqual(placement["diagnostics"], [])
        self.assertIn("tests/pkg/http_port/test_impl.py", json.loads(
            (self.run_dir / "contract-folders-files.json").read_text(encoding="utf-8")))

    def test_misplaced_contract_implementations_and_competing_bases(self):
        self.put("src/pkg/incorrect.py", "from typing import Protocol\n"
                 "class BadPort(Protocol): pass\n")
        self.put("src/pkg/read_port/read_port.py", "from typing import Protocol\n"
                 "class ReadPort(Protocol): pass\n")
        self.put("src/pkg/write_port/write_port.py", "from typing import Protocol\n"
                 "class WritePort(Protocol): pass\n")
        self.put("src/pkg/read_port/reader.py", "from .read_port import ReadPort\n"
                 "class Reader(ReadPort): pass\n")
        self.put("src/pkg/write_port/writer.py", "from .write_port import WritePort\n"
                 "class Writer(WritePort): pass\n")
        self.put("src/pkg/wrong.py", "from pkg.read_port.reader import Reader\n"
                 "class Wrong(Reader): pass\n")
        self.put("src/pkg/bad_impl.py", "from pkg.incorrect import BadPort\n"
                 "class BadImpl(BadPort): pass\n")
        self.put("src/pkg/ambiguous.py", "from pkg.read_port.reader import Reader\n"
                 "from pkg.write_port.writer import Writer\n"
                 "class Ambiguous(Reader, Writer): pass\n")
        self.put("tests/test_wrong.py", "from pkg.read_port.reader import Reader\n"
                 "class TestWrong(Reader): pass\n")
        self.put("src/pkg/unknown.py", "from pkg.missing import Missing\n"
                 "class Unknown(Missing): pass\n")
        self.put("src/pkg/generated/excluded.py", "class NotScanned: pass\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("violations", 1, None))
        self.assertEqual({(d["path"], d["rule"]) for d in result["diagnostics"]}, {
            ("src/pkg/incorrect.py", "CONTRACT_FOLDER"),
            ("src/pkg/bad_impl.py", "IMPLEMENTATION_FOLDER"),
            ("src/pkg/wrong.py", "IMPLEMENTATION_FOLDER"),
            ("src/pkg/ambiguous.py", "COMPETING_LOCAL_BASES"),
            ("tests/test_wrong.py", "IMPLEMENTATION_FOLDER"),
            ("src/pkg/unknown.py", "UNRESOLVED_FOLDER_OWNER")})
        self.assertTrue(all(d["line"] > 0 and d["column"] > 0 and
                            not d["is_infrastructure_error"] for d in result["diagnostics"]))

    def test_reviewed_direct_contracts_keep_competing_owner_and_test_diagnostic(self):
        self.put("src/pkg/first/first.py", "from typing import Protocol\n"
                 "class First(Protocol): pass\n")
        self.put("src/pkg/second/second.py", "from typing import Protocol\n"
                 "class Second(Protocol): pass\n")
        self.put("src/pkg/first/double.py", "from pkg.first.first import First\n"
                 "from pkg.second.second import Second\n"
                 "class Double(First, Second): pass\n")
        self.put("tests/pkg/first/test_double.py", "class TestDouble: pass\n")
        folders, _ = self.run_worker()
        placement, _ = self.run_worker("test-placement")
        self.assertEqual(({(item["path"], item["rule"]) for item in folders["diagnostics"]},
                          {(item["path"], item["rule"]) for item in placement["diagnostics"]}),
                         ({("src/pkg/first/double.py", "COMPETING_LOCAL_BASES")},
                          {("tests/pkg/first/test_double.py", "TEST_TARGET_CONTRACT")}),
                         "Reviewed multiple contracts lost their competing-owner diagnostics")

    def test_source_stub_pairs_and_existing_contract_diagnostics_unchanged(self):
        self.put("src/pkg/service_port/service_port.py", "from typing import Protocol\n"
                 "class ServicePort(Protocol):\n    def serve(self): ...\n")
        self.put("src/pkg/service_port/service_port.pyi", "from typing import Protocol\n"
                 "class ServicePort(Protocol):\n    def serve(self): ...\n")
        self.put("src/pkg/service_port/impl.py", "from .service_port import ServicePort\n"
                 "class Impl(ServicePort):\n    def serve(self): pass\n")
        self.put("src/pkg/service_port/impl.pyi", "from .service_port import ServicePort\n"
                 "class Impl(ServicePort):\n    def serve(self): ...\n")
        folders, _ = self.run_worker()
        contracts, _ = self.run_worker("class-contracts")
        self.assertEqual(folders["diagnostics"], [])
        self.assertEqual(contracts["diagnostics"], [])

    def test_aliases_unresolved_ancestry_and_unrelated_combined_contract(self):
        self.put("src/pkg/first_port/first_port.pyi", "from typing import Protocol as P\n"
                 "class FirstPort(P): pass\n")
        self.put("src/pkg/second_port/second_port.py", "from typing import Protocol\n"
                 "class SecondPort(Protocol): pass\n")
        self.put("src/pkg/first_port/relay.py", "from .first_port import FirstPort as Public\n")
        self.put("src/pkg/first_port/base.py", "from .relay import Public as Parent\n"
                 "class Base(Parent): pass\n")
        self.put("src/pkg/first_port/good.py", "from .base import Base as Ancestor\n"
                 "class Good(Ancestor): pass\n")
        self.put("src/pkg/first_port/unknown.py", "from .base import Base\n"
                 "from pkg.absent import Unknown\nclass Unproven(Base, Unknown): pass\n")
        self.put("src/pkg/second_port/bad.py", "from pkg.first_port.base import Base\n"
                 "from .second_port import SecondPort\n"
                 "class Unrelated(SecondPort, Base): pass\n")
        result, _ = self.run_worker()
        self.assertEqual({(d["symbol"], d["rule"]) for d in result["diagnostics"]}, {
            ("Unproven", "UNRESOLVED_FOLDER_OWNER"),
            ("Unrelated", "COMPETING_LOCAL_BASES")})

    def test_envelopes_source_stubs_aliases_and_independent_jobs(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def send(self): ...\n")
        self.put("src/pkg/port/envelopes/port_envelope.py",
                  "from pkg.port.port import Port as Parent\n"
                  "class PortEnvelope(Parent):\n"
                  "    def __init__(self, delegate): self.delegate = delegate\n"
                  "    def send(self): return self.delegate.send()\n")
        self.put("src/pkg/port/envelopes/port_envelope.pyi",
                 "from pkg.port.port import Port\n"
                 "class PortEnvelope(Port):\n    def send(self): ...\n")
        self.put("src/pkg/port/envelopes/relay.py",
                 "from .port_envelope import PortEnvelope as Base\n"
                 "class Relay(Base):\n    def __init__(self, delegate): super().__init__(delegate)\n")
        self.put("src/pkg/port/envelopes/logging_port_envelope.py",
                 "from .relay import Relay as Decorator\n"
                 "class LoggingPortEnvelope(Decorator):\n"
                 "    def __init__(self, delegate): super().__init__(delegate)\n")
        self.put("src/pkg/port/envelopes/logging_port_envelope.pyi",
                 "from .relay import Relay\n"
                 "class LoggingPortEnvelope(Relay):\n"
                 "    def __init__(self, delegate): ...\n")
        self.put("tests/pkg/port/envelopes/test_port_envelope.py",
                 "from pkg.port.envelopes.port_envelope import PortEnvelope\n"
                 "class TestPortEnvelope(PortEnvelope): pass\n")
        folders, _ = self.run_worker()
        contracts, _ = self.run_worker("class-contracts")
        outer, _ = self.run_worker("outer-classes")
        self.assertEqual(folders["diagnostics"], [])
        self.assertEqual(contracts["diagnostics"], [])
        self.assertEqual(outer["diagnostics"], [])

    def nested_envelopes(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def send(self): ...\n")
        self.put("src/pkg/port/impl.py", "from pkg.port.port import Port\n"
                 "class Impl(Port):\n    def send(self): return 1\n")
        self.put("src/pkg/port/envelopes/nested/__init__.py", "")
        self.put("src/pkg/port/envelopes/nested/base.py",
                 "from pkg.port.port import Port\nfrom pkg.port.impl import Impl\n"
                 "class Base(Port):\n"
                 "    def __init__(self, delegate: Impl): self.delegate = delegate\n"
                 "    def send(self): return self.delegate.send()\n")
        self.put("src/pkg/port/envelopes/nested/base.pyi",
                 "from pkg.port.port import Port\nfrom pkg.port.impl import Impl\n"
                 "class Base(Port):\n"
                 "    def __init__(self, delegate: Impl): ...\n"
                 "    def send(self): ...\n")
        self.put("src/pkg/port/envelopes/nested/deeper/relay.py",
                 "from pkg.port.envelopes.nested.base import Base\n"
                 "from pkg.port.impl import Impl\n"
                 "class Relay(Base):\n"
                 "    def __init__(self, delegate: Impl): super().__init__(delegate)\n")
        self.put("src/pkg/port/envelopes/nested/deeper/relay.pyi",
                 "from pkg.port.envelopes.nested.base import Base\n"
                 "from pkg.port.impl import Impl\n"
                 "class Relay(Base):\n    def __init__(self, delegate: Impl): ...\n")
        self.put("tests/pkg/port/envelopes/nested/deeper/test_relay.py",
                 "class TestRelay: pass\n")

    def test_nested_envelopes_are_proved_in_every_mode(self):
        self.nested_envelopes()
        for marker in (None, LEGACY_IDENTITY, IDENTITY):
            with self.subTest(marker=marker):
                if marker:
                    write_json(self.root / ".quard/architecture.generated.json", marker)
                result, raw = self.run_worker()
                self.assertEqual((result["status"], result["exit_code"],
                                  raw["execution_error"], result["diagnostics"]),
                                 ("passed", 0, None, []))

    def test_nested_envelope_failures_keep_positions_in_every_mode(self):
        self.nested_envelopes()
        self.put("src/pkg/port/envelopes/nested/base.py",
                 "from pkg.port.port import Port\nfrom pkg.port.impl import Impl\n"
                 "class Base(Port):\n"
                 "    def __init__(self, delegate: Impl): self.delegate = delegate\n"
                 "    def send(self): return 0\n")
        self.put("src/pkg/port/envelopes/nested/deeper/relay.py",
                 "from pkg.port.envelopes.nested.base import Base\n"
                 "class Relay(Base):\n"
                 "    def __init__(self, delegate):\n"
                 "        super().__init__(delegate)\n"
                 "        super().__init__(delegate)\n")
        self.put("src/pkg/port/envelopes/nested/deeper/unrelated.py",
                 "class Unrelated(object): pass\n")
        self.put("src/pkg/port/envelopes/nested/deeper/wire.py", "value = 1\n")
        for marker in (None, LEGACY_IDENTITY, IDENTITY):
            with self.subTest(marker=marker):
                if marker:
                    write_json(self.root / ".quard/architecture.generated.json", marker)
                result, raw = self.run_worker()
                self.assertTrue({("src/pkg/port/envelopes/nested/base.py",
                                  "ENVELOPE_DELEGATION"),
                                 ("src/pkg/port/envelopes/nested/deeper/relay.py",
                                  "ENVELOPE_INHERITANCE"),
                                 ("src/pkg/port/envelopes/nested/deeper/unrelated.py",
                                  "ENVELOPE_INHERITANCE"),
                                 ("src/pkg/port/envelopes/nested/deeper/wire.py",
                                  "ENVELOPE_CLASS")} <= {
                                     (d["path"], d["rule"]) for d in result["diagnostics"]
                                 } and result["status"] == "violations" and
                                result["exit_code"] == 1 and raw["execution_error"] is None
                                and all(d["line"] > 0 and d["column"] > 0 and
                                        not d["is_infrastructure_error"]
                                        for d in result["diagnostics"]), result)

    def test_invalid_envelopes_and_misplaced_subclasses_are_positioned(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def send(self): ...\n")
        self.put("src/pkg/port/envelopes/port_envelope.py",
                  "from pkg.port.port import Port\n"
                  "class PortEnvelope(Port):\n"
                  "    def __init__(self, delegate): self.delegate = delegate\n"
                  "    def send(self): return self.delegate.send()\n")
        self.put("src/pkg/port/envelopes/bad.py", "from .port_envelope import PortEnvelope\n"
                 "class WrongFileEnvelope(PortEnvelope): pass\n")
        self.put("src/pkg/port/envelopes/random.py", "class Random(object): pass\n")
        self.put("src/pkg/port/envelopes/pretender.py", "from pkg.port.port import Port\n"
                 "class Pretender(Port): pass\n")
        self.put("src/pkg/port/envelopes/fake_port_envelope.py",
                 "from pkg.port.port import Port\nclass FakePortEnvelope(Port): pass\n")
        self.put("src/pkg/port/envelopes/unrelated_port_envelope.py",
                 "from pathlib import Path\nclass UnrelatedPortEnvelope(Path): pass\n")
        self.put("src/pkg/port/other_port_envelope.py",
                 "from .envelopes.port_envelope import PortEnvelope\n"
                 "class OtherPortEnvelope(PortEnvelope): pass\n")
        self.put("src/pkg/port/envelopes/deep/deep_port_envelope.py",
                 "from pkg.port.envelopes.port_envelope import PortEnvelope\n"
                 "class DeepPortEnvelope(PortEnvelope): pass\n")
        folders, _ = self.run_worker()
        self.assertEqual({(d["symbol"], d["rule"]) for d in folders["diagnostics"]}, {
            ("WrongFileEnvelope", "ENVELOPE_INHERITANCE"),
            ("Random", "ENVELOPE_INHERITANCE"),
            ("Pretender", "ENVELOPE_DELEGATION"),
            ("FakePortEnvelope", "ENVELOPE_DELEGATION"),
            ("DeepPortEnvelope", "ENVELOPE_INHERITANCE"),
            ("UnrelatedPortEnvelope", "ENVELOPE_DELEGATION")})
        self.assertTrue(all(d["line"] > 0 and d["column"] > 0 and
                            not d["is_infrastructure_error"] for d in folders["diagnostics"]))

    def test_envelopes_do_not_hide_contract_or_outer_class_diagnostics(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def send(self): ...\n")
        self.put("src/pkg/port/envelopes/port_envelope.py",
                  "from pkg.port.port import Port\n"
                  "class PortEnvelope(Port):\n"
                  "    def __init__(self, delegate): self.delegate = delegate\n"
                  "    def send(self): return self.delegate.send()\n"
                  "class ExtraEnvelope(PortEnvelope):\n    def extra(self): pass\n")
        folders, _ = self.run_worker()
        contracts, _ = self.run_worker("class-contracts")
        outer, _ = self.run_worker("outer-classes")
        self.assertEqual({d["rule"] for d in folders["diagnostics"]}, {"ENVELOPE_INHERITANCE"})
        self.assertEqual(contracts["diagnostics"], [])
        self.assertEqual([d["rule"] for d in outer["diagnostics"]],
                         ["MULTIPLE_OUTER_CLASSES", "MULTIPLE_OUTER_CLASSES"])

    def test_source_ancestry_wins_for_envelope_descendants_but_stubs_are_checked(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol): pass\n")
        self.put("src/pkg/port/envelopes/port_envelope.py",
                  "from pkg.port.port import Port\nclass PortEnvelope(Port):\n"
                  "    def __init__(self, delegate): self.delegate = delegate\n"
                  "    def send(self): return self.delegate.send()\n")
        self.put("src/pkg/port/envelopes/port_envelope.pyi",
                 "class PortEnvelope(object): ...\n")
        self.put("src/pkg/port/envelopes/child_port_envelope.py",
                 "from .port_envelope import PortEnvelope\n"
                 "class ChildPortEnvelope(PortEnvelope):\n"
                 "    def __init__(self, delegate): super().__init__(delegate)\n")
        result, _ = self.run_worker()
        self.assertEqual([(d["path"], d["rule"]) for d in result["diagnostics"]],
                          [("src/pkg/port/envelopes/port_envelope.pyi", "ENVELOPE_INHERITANCE")])

    def test_base_delegates_in_each_architecture_mode(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n" +
                 "class Port(Protocol):\n    def send(self, value): ...\n")
        self.put("src/pkg/port/envelopes/port_envelope.py",
                 "from pkg.port.port import Port\n" +
                 "class PortEnvelope(Port):\n" +
                 "    def __init__(self, delegate): self.delegate = delegate\n" +
                 "    def send(self, value): return self.delegate.send(value)\n")
        self.put("src/pkg/port/envelopes/port_envelope.pyi",
                 "from pkg.port.port import Port\n" +
                 "class PortEnvelope(Port):\n    def send(self, value): ...\n")
        results = []
        for version in (None, 1, 2):
            if version is not None:
                self.put(".quard/architecture.generated.json",
                         json.dumps({"policy": "contract-only", "version": version}))
            result, _ = self.run_worker()
            results.append((result["status"], result["exit_code"], result["diagnostics"]))
        self.assertEqual(results, [("passed", 0, [])] * 3, "valid base did not pass")

    def test_named_chained_assembly_and_mirrored_tests_in_each_mode(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def send(self, value): ...\n")
        self.put("src/pkg/port/envelopes/anything.py", "from pkg.port.port import Port\n"
                 "class Core(Port):\n"
                 "    def __init__(self, target): self.target = target\n"
                 "    def send(self, value): return self.target.send(value)\n")
        self.put("src/pkg/port/envelopes/anything.pyi", "from pkg.port.port import Port\n"
                 "class Core(Port):\n    def send(self, value): ...\n")
        self.put("src/pkg/port/envelopes/arbitrary.py", "from .anything import Core\n"
                 "class Compose(Core):\n"
                 "    def __init__(self, target):\n"
                 "        value = target\n        super().__init__(value)\n")
        self.put("src/pkg/port/envelopes/arbitrary.pyi", "from .anything import Core\n"
                 "class Compose(Core):\n    def __init__(self, target): ...\n")
        self.put("src/pkg/port/envelopes/chain.py", "from .arbitrary import Compose\n"
                 "class Final(Compose):\n    def __init__(self, target): super().__init__(target)\n")
        self.put("src/pkg/port/envelopes/__init__.py", "\n")
        self.put("tests/pkg/port/envelopes/test_final.py", "from pkg.port.envelopes.chain import Final\n"
                 "class TestFinal:\n    def test_forwarding(self):\n        assert Final(object())\n")
        results = []
        for version in (None, 1, 2):
            if version is not None:
                self.put(".quard/architecture.generated.json",
                         json.dumps({"policy": "contract-only", "version": version}))
            result, _ = self.run_worker()
            results.append((result["status"], result["exit_code"], result["diagnostics"]))
        self.assertEqual(results, [("passed", 0, [])] * 3, "named assembly did not pass")

    def test_assembly_failures_are_positioned_in_all_modes(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def send(self): ...\n")
        self.put("src/pkg/port/envelopes/core.py", "from pkg.port.port import Port\n"
                 "class Core(Port):\n    def __init__(self, target): self.target = target\n"
                 "    def send(self): return self.target.send()\n")
        cases = {
            "missing": "class Missing(Core):\n    def __init__(self, target): pass\n",
            "twice": "class Twice(Core):\n    def __init__(self, target):\n"
                     "        super().__init__(target)\n        super().__init__(target)\n",
            "branch": "class Branch(Core):\n    def __init__(self, target):\n"
                      "        if target: super().__init__(target)\n",
            "shadow": "class Shadow(Core):\n    def __init__(self, target):\n"
                      "        super = target\n        super().__init__(target)\n",
            "extra": "class Extra(Core):\n    def __init__(self, target): super().__init__(target)\n"
                     "    def send(self): return 42\n",
            "unrelated": "class Unrelated(object): pass\n",
        }
        for stem, text in cases.items():
            self.put(f"src/pkg/port/envelopes/{stem}.py",
                     "from .core import Core\n" + text)
        self.put("src/pkg/port/envelopes/helpers.py", "def dispatch(): return 1\n")
        results = []
        for version in (None, 1, 2):
            if version is not None:
                self.put(".quard/architecture.generated.json",
                         json.dumps({"policy": "contract-only", "version": version}))
            result, _ = self.run_worker()
            results.append((result["status"], result["exit_code"],
                            {(item["path"], item["line"], item["column"],
                              item["is_infrastructure_error"]) for item in result["diagnostics"]}))
        expected = {(f"src/pkg/port/envelopes/{stem}.py", 2, 1, False) for stem in cases}
        expected.add(("src/pkg/port/envelopes/helpers.py", 1, 1, False))
        self.assertEqual(results, [("violations", 1, expected)] * 3,
                         "invalid assembly was accepted")

    def test_stub_ancestry_and_source_proof_cannot_be_bypassed(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def send(self): ...\n")
        self.put("src/pkg/port/envelopes/core.py", "from pkg.port.port import Port\n"
                 "class Core(Port):\n    def __init__(self, target): self.target = target\n"
                 "    def send(self): return self.target.send()\n")
        self.put("src/pkg/port/envelopes/chain.py", "from .core import Core\n"
                 "class Chain(Core):\n    def __init__(self, target): super().__init__(target)\n")
        self.put("src/pkg/port/envelopes/chain.pyi", "from pkg.port.port import Port\n"
                 "class Chain(Port):\n    def send(self): ...\n")
        self.put("src/pkg/port/envelopes/orphan.pyi", "from .core import Core\n"
                 "class Orphan(Core):\n    def __init__(self, target): ...\n")
        self.put("src/pkg/port/envelopes/empty.py", "from .core import Core\n"
                 "class Empty(Core):\n    def __init__(self, target): super().__init__(target)\n")
        self.put("src/pkg/port/envelopes/empty.pyi", "from .core import Core\n"
                 "class Empty(Core): ...\n")
        result, _ = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"],
                          {(item["path"], item["line"], item["column"], item["rule"],
                            item["is_infrastructure_error"]) for item in result["diagnostics"]}),
                         ("violations", 1, {
                             ("src/pkg/port/envelopes/chain.pyi", 2, 1,
                              "ENVELOPE_DELEGATION", False),
                             ("src/pkg/port/envelopes/orphan.pyi", 2, 1,
                              "ENVELOPE_INHERITANCE", False),
                             ("src/pkg/port/envelopes/empty.pyi", 2, 1,
                              "ENVELOPE_INHERITANCE", False)}),
                         "stub-only or conflicting Envelope passed")

    def test_unproved_parent_blocks_descendant_and_keeps_ownership(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def send(self): ...\n")
        self.put("src/pkg/port/envelopes/core.py", "from pkg.port.port import Port\n"
                 "class Core(Port):\n    def send(self): return 0\n")
        self.put("src/pkg/port/envelopes/chain.py", "from .core import Core\n"
                 "class Chain(Core):\n    def __init__(self, target): super().__init__(target)\n")
        self.put("src/pkg/foreign/envelopes/wrong.py", "from pkg.port.port import Port\n"
                 "class Wrong(Port):\n    def __init__(self, target): self.target = target\n"
                 "    def send(self): return self.target.send()\n")
        result, _ = self.run_worker()
        self.assertEqual({(item["path"], item["rule"]) for item in result["diagnostics"]},
                         {("src/pkg/port/envelopes/core.py", "ENVELOPE_DELEGATION"),
                          ("src/pkg/port/envelopes/chain.py", "ENVELOPE_INHERITANCE"),
                          ("src/pkg/foreign/envelopes/wrong.py", "IMPLEMENTATION_FOLDER")},
                         "unproved ancestor or foreign ownership was accepted")

    def test_non_forwarding_and_invalid_constructor_are_positioned_in_every_mode(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n" +
                 "class Port(Protocol):\n    def send(self, value): ...\n")
        self.put("src/pkg/port/envelopes/port_envelope.py",
                 "from pkg.port.port import Port\n" +
                 "class PortEnvelope(Port):\n" +
                 "    def __init__(self, delegate): self.delegate = delegate\n" +
                 "    def send(self, value): return value\n")
        results = []
        for version in (None, 1, 2):
            if version is not None:
                self.put(".quard/architecture.generated.json",
                         json.dumps({"policy": "contract-only", "version": version}))
            result, _ = self.run_worker()
            results.append((result["status"], result["exit_code"],
                            [(item["rule"], item["line"], item["column"],
                              item["is_infrastructure_error"]) for item in result["diagnostics"]]))
        self.assertEqual(results, [("violations", 1, [("ENVELOPE_DELEGATION", 2, 1, False)])] * 3,
                         "non-forwarding base was not reported")

    def test_extra_constructor_work_rejected(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n" +
                 "class Port(Protocol): pass\n")
        self.put("src/pkg/port/envelopes/port_envelope.py",
                 "from pkg.port.port import Port\nclass PortEnvelope(Port):\n" +
                 "    def __init__(self, delegate):\n" +
                 "        self.delegate = delegate\n        delegate.send()\n" +
                 "    def send(self): return self.delegate.send()\n")
        result, _ = self.run_worker()
        self.assertEqual([(item["rule"], item["line"]) for item in result["diagnostics"]],
                         [("ENVELOPE_DELEGATION", 2)], "constructor work was not reported")

    def test_async_forwarding_accepts_same_arguments(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n" +
                 "class Port(Protocol): pass\n")
        self.put("src/pkg/port/envelopes/port_envelope.py",
                 "from pkg.port.port import Port\nclass PortEnvelope(Port):\n" +
                 "    def __init__(self, delegate): self.delegate = delegate\n" +
                 "    async def send(self, value, *, flag=False):\n" +
                 "        return await self.delegate.send(value, flag=flag)\n")
        result, _ = self.run_worker()
        self.assertEqual(result["diagnostics"], [], "async forwarding was rejected")

    def test_changed_async_forwarding_is_positioned_in_every_mode(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\nclass Port(Protocol): ...\n")
        cases = (
            "return await self.delegate.other(value)",
            "return await self.delegate.send(value + 1)",
            "return await self.other.send(value)",
            "return (await self.delegate.send(value)) + 1",
            "result = await self.delegate.send(value)\n        return result",
            "return await getattr(self.delegate, 'send')(value)",
        )
        results = []
        for body in cases:
            self.put("src/pkg/port/envelopes/core.py", "from pkg.port.port import Port\n"
                     "class Core(Port):\n    def __init__(self, target): self.target = target\n"
                     "    async def send(self, value):\n        "
                     + body.replace("delegate", "target") + "\n")
            for version in (None, 1, 2):
                if version is not None:
                    self.put(".quard/architecture.generated.json",
                             json.dumps({"policy": "contract-only", "version": version}))
                result, _ = self.run_worker()
                results.append((result["status"], result["exit_code"],
                                [(item["rule"], item["line"], item["column"])
                                 for item in result["diagnostics"]]))
        self.assertEqual(results, [("violations", 1, [("ENVELOPE_DELEGATION", 2, 1)])]
                         * (len(cases) * 3), "changed async forwarding passed")

    def test_properties_forward_in_every_mode_without_enumerating_external_members(self):
        self.put("src/pkg/port/envelopes/port_envelope.py",
                 "from external_api import Port\nclass PortEnvelope(Port):\n"
                 "    def __init__(self, delegate): self.delegate = delegate\n"
                 "    @property\n    def value(self): return self.delegate.value\n"
                 "    @value.setter\n    def value(self, value): self.delegate.value = value\n"
                 "    @value.deleter\n    def value(self): del self.delegate.value\n")
        self.put("src/pkg/port/envelopes/port_envelope.pyi",
                 "from external_api import Port\nclass PortEnvelope(Port):\n"
                 "    @property\n    def value(self): ...\n"
                 "    @value.setter\n    def value(self, value): ...\n"
                 "    @value.deleter\n    def value(self): ...\n")
        results = []
        for version in (None, 1, 2):
            if version is not None:
                self.put(".quard/architecture.generated.json",
                         json.dumps({"policy": "contract-only", "version": version}))
            result, _ = self.run_worker()
            results.append((result["status"], result["exit_code"], result["diagnostics"]))
        self.assertEqual(results, [("passed", 0, [])] * 3, "property forwarding failed")

    def test_property_mutations_and_indirection_are_positioned(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\nclass Port(Protocol): ...\n")
        cases = {
            "read": "@property\n    def value(self): return self.delegate.other\n",
            "set": "@property\n    def value(self): return self.delegate.value\n"
                   "    @value.setter\n    def value(self, value): self.delegate.value = value + 1\n",
            "delete": "@property\n    def value(self): return self.delegate.value\n"
                      "    @value.deleter\n    def value(self): del self.other.value\n",
            "extra": "@property\n    def value(self):\n"
                     "        self.delegate.tick()\n        return self.delegate.value\n",
            "indirect": "@property\n    def value(self): return getattr(self.delegate, 'value')\n",
            "wrong": "@property\n    def value(self): return self.delegate.value()\n",
        }
        results = []
        for stem, body in cases.items():
            self.put(f"src/pkg/port/envelopes/{stem}.py",
                     "from pkg.port.port import Port\nclass Wrapper(Port):\n"
                     "    def __init__(self, delegate): self.delegate = delegate\n"
                     "    " + body)
            result, _ = self.run_worker()
            results.append(any(item["path"] == f"src/pkg/port/envelopes/{stem}.py"
                               and item["rule"] == "ENVELOPE_DELEGATION"
                               and (item["line"], item["column"]) == (2, 1)
                               for item in result["diagnostics"]))
        self.assertEqual(results, [True] * len(cases), "unproved property passed")

    def test_outside_source_ancestor_proves_assembly_but_unknown_does_not(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\nclass Port(Protocol): ...\n")
        self.put("src/pkg/port/base.py", "from .port import Port\nclass Base(Port):\n"
                 "    def __init__(self, delegate): self.delegate = delegate\n"
                 "    def send(self): return self.delegate.send()\n")
        self.put("src/pkg/port/envelopes/known.py", "from pkg.port.base import Base\n"
                 "class Known(Base):\n    def __init__(self, delegate): super().__init__(delegate)\n")
        self.put("src/pkg/port/envelopes/unknown.py", "from pkg.port.missing import Missing\n"
                 "class Unknown(Missing):\n    def __init__(self, delegate): super().__init__(delegate)\n")
        result, _ = self.run_worker()
        self.assertEqual({(item["path"], item["rule"], item["line"])
                          for item in result["diagnostics"]},
                         {("src/pkg/port/envelopes/unknown.py", "ENVELOPE_INHERITANCE", 2),
                          ("src/pkg/port/envelopes/unknown.py", "UNRESOLVED_FOLDER_OWNER", 2)},
                         "outside source or unknown ancestry was misclassified")

    def test_stub_property_accessors_must_agree_with_source(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\nclass Port(Protocol): ...\n")
        self.put("src/pkg/port/envelopes/core.py", "from pkg.port.port import Port\n"
                 "class Core(Port):\n    def __init__(self, delegate): self.delegate = delegate\n"
                 "    @property\n    def value(self): return self.delegate.value\n"
                 "    @value.setter\n    def value(self, value): self.delegate.value = value\n")
        self.put("src/pkg/port/envelopes/core.pyi", "from pkg.port.port import Port\n"
                 "class Core(Port):\n    @property\n    def value(self): ...\n"
                 "    @value.deleter\n    def value(self): ...\n")
        result, _ = self.run_worker()
        self.assertEqual([(item["path"], item["rule"], item["line"])
                          for item in result["diagnostics"]],
                         [("src/pkg/port/envelopes/core.pyi", "ENVELOPE_DELEGATION", 2)],
                         "conflicting stub accessor was accepted")

    def test_stub_async_method_cannot_claim_sync_source(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\nclass Port(Protocol): ...\n")
        self.put("src/pkg/port/envelopes/core.py", "from pkg.port.port import Port\n"
                 "class Core(Port):\n    def __init__(self, target): self.target = target\n"
                 "    def send(self): return self.target.send()\n")
        self.put("src/pkg/port/envelopes/core.pyi", "from pkg.port.port import Port\n"
                 "class Core(Port):\n    async def send(self): ...\n")
        result, _ = self.run_worker()
        self.assertEqual([(item["rule"], item["line"]) for item in result["diagnostics"]],
                         [("ENVELOPE_DELEGATION", 2)], "stub changed sync kind")

    def envelope_pair(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n" +
                 "class Port(Protocol):\n    def send(self): ...\n")
        self.put("src/pkg/port/envelopes/core.py", "from pkg.port.port import Port\n" +
                 "class Core(Port):\n    def __init__(self, target): self.target = target\n" +
                 "    def send(self): return self.target.send()\n")

    def test_assembly_return_before_parent_call_is_not_proof(self):
        self.envelope_pair()
        self.put("src/pkg/port/envelopes/relay.py", "from .core import Core\n" +
                 "class Relay(Core):\n    def __init__(self, target):\n" +
                 "        if target is None: return\n" +
                 "        super().__init__(target)\n")
        result, _ = self.run_worker()
        self.assertEqual({(item["path"], item["rule"]) for item in result["diagnostics"]},
                         {("src/pkg/port/envelopes/relay.py", "ENVELOPE_INHERITANCE")},
                         "an early return passed as a parent call")

    def test_assembly_unconditional_return_before_parent_call_is_not_proof(self):
        self.envelope_pair()
        self.put("src/pkg/port/envelopes/relay.py", "from .core import Core\n" +
                 "class Relay(Core):\n    def __init__(self, target):\n" +
                 "        return\n        super().__init__(target)\n")
        result, _ = self.run_worker()
        self.assertEqual({item["rule"] for item in result["diagnostics"]},
                         {"ENVELOPE_INHERITANCE"}, "an unreachable parent call passed")

    def test_branch_without_early_return_preserves_parent_call(self):
        self.envelope_pair()
        self.put("src/pkg/port/envelopes/relay.py", "from .core import Core\n" +
                 "class Relay(Core):\n    def __init__(self, target):\n" +
                 "        if target is None: target = object()\n" +
                 "        super().__init__(target)\n")
        result, _ = self.run_worker()
        self.assertEqual(result["diagnostics"], [], "a reachable parent call was rejected")

    def test_nested_constructor_scope_does_not_count_as_parent_call(self):
        self.envelope_pair()
        self.put("src/pkg/port/envelopes/relay.py", "from .core import Core\n" +
                 "class Relay(Core):\n    def __init__(self, target):\n" +
                 "        def later(): super().__init__(target)\n" +
                 "        return\n")
        result, _ = self.run_worker()
        self.assertEqual({item["rule"] for item in result["diagnostics"]},
                         {"ENVELOPE_INHERITANCE"}, "a deferred parent call passed")

    def test_local_super_definition_cannot_prove_parent_call(self):
        self.envelope_pair()
        self.put("src/pkg/port/envelopes/relay.py", "from .core import Core\n" +
                 "class Relay(Core):\n    def __init__(self, target):\n" +
                 "        def super(): return target\n" +
                 "        super().__init__(target)\n")
        result, _ = self.run_worker()
        self.assertEqual({item["rule"] for item in result["diagnostics"]},
                         {"ENVELOPE_INHERITANCE"}, "a shadowed super passed")

    def test_envelope_module_rejects_unrelated_free_function(self):
        self.envelope_pair()
        self.put("src/pkg/port/envelopes/core.py", (self.root / "src/pkg/port/envelopes/core.py")
                 .read_text() + "def unrelated(): return 42\n")
        result, _ = self.run_worker()
        self.assertEqual({(item["path"], item["line"], item["rule"])
                          for item in result["diagnostics"]},
                         {("src/pkg/port/envelopes/core.py", 5, "ENVELOPE_CLASS")},
                         "unrelated module behavior passed")

    def test_envelope_initializer_rejects_value(self):
        self.envelope_pair()
        self.put("src/pkg/port/envelopes/__init__.py", "value = 42\n")
        result, _ = self.run_worker()
        self.assertEqual({(item["path"], item["line"], item["rule"])
                          for item in result["diagnostics"]},
                         {("src/pkg/port/envelopes/__init__.py", 1, "ENVELOPE_CLASS")},
                         "initializer code passed")

    def test_envelope_initializer_rejects_unrelated_class(self):
        self.envelope_pair()
        self.put("src/pkg/port/envelopes/__init__.py", "class Unrelated: pass\n")
        result, _ = self.run_worker()
        self.assertEqual({(item["path"], item["line"], item["rule"])
                          for item in result["diagnostics"]},
                         {("src/pkg/port/envelopes/__init__.py", 1, "ENVELOPE_CLASS")},
                         "an initializer class passed")

    def test_technical_envelope_initializer_is_allowed(self):
        self.envelope_pair()
        self.put("src/pkg/port/envelopes/__init__.py", '"""Envelope exports."""\n' +
                 "from .core import Core\n__all__ = ['Core']\n")
        result, _ = self.run_worker()
        self.assertEqual(result["diagnostics"], [], "technical initialization was rejected")

    def test_configured_source_test_directory_is_not_an_envelope(self):
        self.envelope_pair()
        self.put(".quard/config.toml", (self.root / ".quard/config.toml").read_text()
                 .replace('test_directory_names = ["tests"]',
                          'test_directory_names = ["tests", "specs"]'))
        self.put("src/pkg/port/envelopes/specs/test_core.py", "class TestCore: pass\n")
        result, _ = self.run_worker()
        self.assertEqual(result["diagnostics"], [], "a configured test was checked as an Envelope")

    def test_root_tests_remain_tests_with_other_configured_names(self):
        self.envelope_pair()
        self.put(".quard/config.toml", (self.root / ".quard/config.toml").read_text()
                 .replace('test_directory_names = ["tests"]',
                          'test_directory_names = ["specs"]'))
        self.put("tests/pkg/port/envelopes/test_core.py", "class TestCore: pass\n")
        result, _ = self.run_worker()
        self.assertEqual(result["diagnostics"], [], "root tests were checked as Envelopes")

    def test_paired_stub_constructor_signature_must_match_source(self):
        self.envelope_pair()
        self.put("src/pkg/port/envelopes/core.pyi", "from pkg.port.port import Port\n" +
                 "class Core(Port):\n    def __init__(self, target, extra): ...\n" +
                 "    def send(self): ...\n")
        result, _ = self.run_worker()
        self.assertEqual({(item["path"], item["rule"]) for item in result["diagnostics"]},
                         {("src/pkg/port/envelopes/core.pyi", "ENVELOPE_DELEGATION")},
                         "an incompatible stub constructor passed")

    def test_paired_stub_may_omit_base_constructor(self):
        self.envelope_pair()
        self.put("src/pkg/port/envelopes/core.pyi", "from pkg.port.port import Port\n" +
                 "class Core(Port):\n    def send(self): ...\n")
        result, _ = self.run_worker()
        self.assertEqual(result["diagnostics"], [], "an omitted stub constructor was rejected")

    def test_paired_stub_constructor_allows_annotations_and_placeholder(self):
        self.envelope_pair()
        self.put("src/pkg/port/envelopes/core.pyi", "from pkg.port.port import Port\n" +
                 "class Core(Port):\n    def __init__(self, target: Port) -> None: ...\n" +
                 "    def send(self): ...\n")
        result, _ = self.run_worker()
        self.assertEqual(result["diagnostics"], [], "a stub annotation was rejected")

    def test_paired_assembly_stub_constructor_shape_must_match(self):
        self.envelope_pair()
        self.put("src/pkg/port/envelopes/relay.py", "from .core import Core\n" +
                 "class Relay(Core):\n    def __init__(self, target): super().__init__(target)\n")
        self.put("src/pkg/port/envelopes/relay.pyi", "from .core import Core\n" +
                 "class Relay(Core):\n    def __init__(self, target, extra): ...\n")
        result, _ = self.run_worker()
        self.assertEqual({(item["path"], item["rule"]) for item in result["diagnostics"]},
                         {("src/pkg/port/envelopes/relay.pyi", "ENVELOPE_INHERITANCE")},
                         "an incompatible assembly stub passed")

    def test_external_contract_base_can_delegate(self):
        self.put("src/pkg/port/envelopes/port_envelope.py",
                 "from external_api import Port\nclass PortEnvelope(Port):\n" +
                 "    def __init__(self, delegate): self.delegate = delegate\n" +
                 "    def send(self, value): return self.delegate.send(value)\n")
        results = []
        for version in (None, 1, 2):
            if version is not None:
                self.put(".quard/architecture.generated.json",
                         json.dumps({"policy": "contract-only", "version": version}))
            result, _ = self.run_worker()
            results.append(result["diagnostics"])
        self.assertEqual(results, [[], [], []], "external contract base was rejected")

    def test_stub_without_source_cannot_prove_delegation(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n" +
                 "class Port(Protocol): pass\n")
        self.put("src/pkg/port/envelopes/port_envelope.pyi",
                 "from pkg.port.port import Port\nclass PortEnvelope(Port):\n" +
                 "    def send(self): ...\n")
        result, _ = self.run_worker()
        self.assertEqual([(item["rule"], item["line"]) for item in result["diagnostics"]],
                         [("ENVELOPE_DELEGATION", 2)], "stub-only base was accepted")

    def test_base_rejects_unproved_shapes(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n" +
                 "class Port(Protocol): pass\n")
        self.put("src/pkg/port/impl.py", "from pkg.port.port import Port\nclass Impl(Port): pass\n")
        self.put("src/pkg/port/envelopes/port_envelope.py",
                 "from pkg.port.port import Port\nclass PortEnvelope(Port): pass\n")
        shapes = (
            "class PortEnvelope:\n    def __init__(self, delegate): self.delegate = delegate\n" +
            "    def send(self, value): return self.delegate.send(value)\n",
            "class PortEnvelope(Port, object):\n" +
            "    def __init__(self, delegate): self.delegate = delegate\n" +
            "    def send(self, value): return self.delegate.send(value)\n",
            "class PortEnvelope(Port, Impl):\n" +
            "    def __init__(self, delegate): self.delegate = delegate\n" +
            "    def send(self, value): return self.delegate.send(value)\n",
            "class PortEnvelope(Port):\n" +
            "    def send(self, value): return value\n",
            "class PortEnvelope(Port):\n" +
            "    def __init__(self, delegate): self.delegate = delegate\n",
            "class PortEnvelope(Port):\n" +
            "    def __init__(self, delegate): self.delegate = delegate\n" +
            "    def send(self, value): return self.delegate.send(value + 1)\n",
            "class PortEnvelope(Port):\n" +
            "    def __init__(self, delegate): self.delegate = delegate\n" +
            "    def send(self, value): return self.other.send(value)\n",
        )
        results = []
        for shape in shapes:
            self.put("src/pkg/port/envelopes/port_envelope.py",
                     "from pkg.port.port import Port\nfrom pkg.port.impl import Impl\n" + shape)
            result, _ = self.run_worker()
            results.append((result["status"], result["exit_code"],
                            any(item["rule"] in {"ENVELOPE_DELEGATION", "ENVELOPE_INHERITANCE"}
                                and item["line"] == 3
                                and not item["is_infrastructure_error"]
                                for item in result["diagnostics"])))
        self.assertEqual(results, [("violations", 1, True)] * len(shapes),
                         "unproved base shape was accepted")

    def test_generated_malformed_source_stays_an_error_with_base_violation(self):
        self.put(".quard/architecture.generated.json",
                 json.dumps({"policy": "contract-only", "version": 2}))
        self.put("src/pkg/port/port.py", "from typing import Protocol\n" +
                 "class Port(Protocol): pass\n")
        self.put("src/pkg/port/envelopes/port_envelope.py",
                 "from pkg.port.port import Port\nclass PortEnvelope(Port): pass\n")
        self.put("src/pkg/broken.py", "class Broken(\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"],
                          {item["rule"] for item in result["diagnostics"]}),
                         ("error", 2, "CONTRACT_FOLDERS_PARSE_ERROR",
                          {"CONTRACT_FOLDERS_PARSE_ERROR", "ENVELOPE_DELEGATION"}),
                         "malformed source was treated as a passing check")

    def test_envelope_parse_and_worker_failures_keep_status_in_every_mode(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\nclass Port(Protocol): ...\n")
        self.put("src/pkg/port/envelopes/core.py", "from pkg.port.port import Port\n"
                 "class Core(Port): pass\n")
        self.put("src/pkg/broken.py", "class Broken(\n")
        results = []
        for version in (None, 1, 2):
            if version is not None:
                self.put(".quard/architecture.generated.json",
                         json.dumps({"policy": "contract-only", "version": version}))
            result, raw = self.run_worker()
            files = discover_files(self.root, read_scope(self.root))
            job = next(job for job in make_jobs(self.root, self.run_dir, files, False, [])
                       if job["name"] == "contract-folders")
            broken = execute_job(job, self.root, self.run_dir, 30, capture_fn=lambda *args: {
                "stdout": "not json", "stderr": "worker crashed", "exit_code": 2,
                "duration_seconds": 0, "error": None})
            results.append((result["status"], result["exit_code"], raw["execution_error"],
                            {(item["rule"], item["is_infrastructure_error"])
                             for item in result["diagnostics"]},
                            broken["status"], broken["exit_code"], broken["error"]))
        self.assertEqual(results, [("error", 2, "CONTRACT_FOLDERS_PARSE_ERROR",
                                    {("ENVELOPE_DELEGATION", False),
                                     ("CONTRACT_FOLDERS_PARSE_ERROR", True)},
                                    "error", 2, "UNREADABLE_TOOL_OUTPUT")] * 3,
                         "envelope errors were reported as success")

    def test_parse_failure_retains_violations_and_malformed_report_fails(self):
        self.put("src/bad.py", "class Broken(\n")
        self.put("tests/wrong.py", "from typing import Protocol\nclass Port(Protocol): pass\n")
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol): pass\n")
        self.put("src/pkg/port/envelopes/wrong.py", "from pkg.port.port import Port\n"
                  "class PortEnvelope(Port):\n"
                  "    def __init__(self, delegate): self.delegate = delegate\n"
                  "    def send(self): return self.delegate.send()\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("error", 2, "CONTRACT_FOLDERS_PARSE_ERROR"))
        self.assertEqual({d["rule"] for d in result["diagnostics"]},
                         {"CONTRACT_FOLDER", "CONTRACT_FOLDERS_PARSE_ERROR"})
        files = discover_files(self.root, read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False, [])
                   if job["name"] == "contract-folders")
        broken = execute_job(job, self.root, self.run_dir, 30, capture_fn=lambda *args: {
            "stdout": '{"quard_contract_folders_adapter": 1, "diagnostics": [], "execution_error": null}',
            "stderr": "", "exit_code": 1, "duration_seconds": 0, "error": None})
        self.assertEqual((broken["status"], broken["error"]),
                         ("error", "CONTRACT_FOLDERS_EXECUTION_ERROR"))


if __name__ == "__main__":
    unittest.main()
