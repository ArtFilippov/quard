"""End-to-end folder ownership checks across production, tests and stubs."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ERYX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ERYX / "src"))

from eryx._policy import discover_files, read_scope
from eryx.check import execute_job, make_jobs
from eryx._class_contracts_worker import Contracts as LegacyContracts
from eryx._contract_folders_worker import FolderOwnership as LegacyFolderOwnership
from eryx._lcom4_worker import Classes as LegacyClasses
from eryx._lcom4_worker import Declarations as LegacyCohesionDeclarations
from eryx._lcom4_worker import MethodReferences as LegacyMethodReferences
from eryx._method_decorators_worker import Declarations as LegacyMethodDeclarations
from eryx._test_placement_worker import TestPlacement as LegacyTestPlacement
from eryx.contract_analysis.contracts import Contracts
from eryx.contract_analysis.folder_ownership import FolderOwnership
from eryx.contract_analysis.test_placement import TestPlacement
from eryx.cohesion_analysis.classes import Classes
from eryx.cohesion_analysis.declarations import Declarations as CohesionDeclarations
from eryx.cohesion_references.method_references import MethodReferences
from eryx.method_analysis.declarations import Declarations


class ContractFolderTests(unittest.TestCase):
    def test_migrated_state_and_marker_classes_keep_entry_points_and_placement(self):
        from eryx._constructor_origins import Origins as LegacyOrigins
        from eryx._constructor_purity_worker import ConstructorChecker as LegacyChecker
        from eryx._field_operations import FieldOperation as LegacyOperation
        from eryx._field_types import TypeEvidence as LegacyEvidence
        from eryx._markers import MarkerBindings as LegacyBindings, MarkerIndex as LegacyIndex
        from eryx._restricted_calls import DispatchSite as LegacySite
        from eryx.constructor_analysis.constructor_checker import ConstructorChecker
        from eryx.constructor_origins.origins import Origins
        from eryx.dispatch_site_model.dispatch_site import DispatchSite
        from eryx.field_operation_model.field_operation import FieldOperation
        from eryx.field_type_analysis.type_evidence import TypeEvidence
        from eryx.marker_analysis.marker_index import MarkerIndex
        from eryx.marker_binding_analysis.marker_bindings import MarkerBindings
        from eryx._class_contracts_worker import analyze as contracts_analyze
        from eryx._contract_folders_worker import analyze as folders_analyze
        from eryx._outer_classes_worker import analyze as outer_analyze

        for old, current in ((LegacyOrigins, Origins), (LegacyChecker, ConstructorChecker),
                             (LegacyOperation, FieldOperation), (LegacyEvidence, TypeEvidence),
                             (LegacyIndex, MarkerIndex), (LegacyBindings, MarkerBindings),
                             (LegacySite, DispatchSite)):
            self.assertIs(old, current)

        paths = sorted(str(path.relative_to(ERYX)) for folder in (
            "constructor_analysis", "constructor_origins", "dispatch_site_model", "field_operation_model",
            "field_type_analysis", "marker_analysis", "marker_binding_analysis")
            for path in (ERYX / "src/eryx" / folder).glob("*.py"))
        paths.extend(f"src/eryx/{module}.py" for module in (
            "_constructor_origins", "_constructor_purity_worker", "_field_operations",
            "_field_types", "_markers"))
        for analyze in (contracts_analyze, folders_analyze, outer_analyze):
            diagnostics, error = analyze(ERYX, paths)
            self.assertIsNone(error)
            self.assertEqual([], diagnostics)

    def test_migrated_production_classes_and_compatibility_imports(self):
        self.assertEqual(self.run_worker("class-contracts")[0]["exit_code"], 0)
        self.assertIs(LegacyContracts, Contracts)
        self.assertIs(LegacyFolderOwnership, FolderOwnership)
        self.assertIs(LegacyTestPlacement, TestPlacement)
        self.assertIs(LegacyClasses, Classes)
        self.assertIs(LegacyCohesionDeclarations, CohesionDeclarations)
        self.assertIs(LegacyMethodReferences, MethodReferences)
        self.assertIs(LegacyMethodDeclarations, Declarations)

        from eryx._class_contracts_worker import analyze as contracts_analyze
        from eryx._contract_folders_worker import analyze as folders_analyze
        from eryx._outer_classes_worker import analyze as outer_analyze

        paths = sorted(str(path.relative_to(ERYX)) for folder in (
            "contract_analysis", "method_analysis", "cohesion_analysis", "cohesion_references")
            for path in (ERYX / "src/eryx" / folder).glob("*.py"))
        paths.extend(f"src/eryx/{worker}.py" for worker in (
            "_class_contracts_worker", "_contract_folders_worker", "_lcom4_worker",
            "_method_decorators_worker", "_test_placement_worker"))
        for analyze in (contracts_analyze, folders_analyze, outer_analyze):
            diagnostics, error = analyze(ERYX, paths)
            self.assertIsNone(error)
            self.assertEqual([], diagnostics)

    def setUp(self):
        self.temp = TemporaryDirectory(prefix="eryx-folders-")
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / ".eryx").mkdir()
        (self.root / ".eryx/scope.toml").write_text(
            'exclude_directory_names = ["generated"]\n'
            'test_directory_names = ["tests"]\n', encoding="utf-8")
        self.run_dir = self.root / ".eryx/reports/run"
        self.run_dir.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

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
        self.put("tests/pkg/http_port/test_impl.py", "from pkg.http_port.impl import Impl\n"
                 "class TestImpl(Impl): pass\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("passed", 0, None), result["diagnostics"])
        self.assertEqual(result["diagnostics"], [])
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
                 "class PortEnvelope(Parent):\n    def send(self): pass\n")
        self.put("src/pkg/port/envelopes/port_envelope.pyi",
                 "from pkg.port.port import Port\n"
                 "class PortEnvelope(Port):\n    def send(self): ...\n")
        self.put("src/pkg/port/envelopes/relay.py",
                 "from .port_envelope import PortEnvelope as Base\n")
        self.put("src/pkg/port/envelopes/logging_port_envelope.py",
                 "from .relay import Base as Decorator\n"
                 "class LoggingPortEnvelope(Decorator): pass\n")
        self.put("src/pkg/port/envelopes/logging_port_envelope.pyi",
                 "from .port_envelope import PortEnvelope\n"
                 "class LoggingPortEnvelope(PortEnvelope): ...\n")
        self.put("src/pkg/port/envelopes/stub_port_envelope.pyi",
                 "from .port_envelope import PortEnvelope as Base\n"
                 "class StubPortEnvelope(Base): ...\n")
        self.put("tests/pkg/port/envelopes/test_port_envelope.py",
                 "from pkg.port.envelopes.port_envelope import PortEnvelope\n"
                 "class TestPortEnvelope(PortEnvelope): pass\n")
        folders, _ = self.run_worker()
        contracts, _ = self.run_worker("class-contracts")
        outer, _ = self.run_worker("outer-classes")
        self.assertEqual(folders["diagnostics"], [])
        self.assertEqual(contracts["diagnostics"], [])
        self.assertEqual(outer["diagnostics"], [])

    def test_invalid_envelopes_and_misplaced_subclasses_are_positioned(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def send(self): ...\n")
        self.put("src/pkg/port/envelopes/port_envelope.py",
                 "from pkg.port.port import Port\n"
                 "class PortEnvelope(Port):\n    def send(self): pass\n")
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
            ("WrongFileEnvelope", "ENVELOPE_FILE"),
            ("Random", "ENVELOPE_CLASS"),
            ("Pretender", "ENVELOPE_CLASS"),
            ("FakePortEnvelope", "ENVELOPE_INHERITANCE"),
            ("UnrelatedPortEnvelope", "ENVELOPE_INHERITANCE"),
            ("OtherPortEnvelope", "ENVELOPE_FOLDER"),
            ("DeepPortEnvelope", "ENVELOPE_FOLDER")})
        self.assertTrue(all(d["line"] > 0 and d["column"] > 0 and
                            not d["is_infrastructure_error"] for d in folders["diagnostics"]))

    def test_envelopes_do_not_hide_contract_or_outer_class_diagnostics(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol):\n    def send(self): ...\n")
        self.put("src/pkg/port/envelopes/port_envelope.py",
                 "from pkg.port.port import Port\n"
                 "class PortEnvelope(Port):\n    def send(self): pass\n"
                 "class ExtraEnvelope(PortEnvelope):\n    def extra(self): pass\n")
        folders, _ = self.run_worker()
        contracts, _ = self.run_worker("class-contracts")
        outer, _ = self.run_worker("outer-classes")
        self.assertEqual({d["rule"] for d in folders["diagnostics"]}, {"ENVELOPE_FILE"})
        self.assertEqual({d["rule"] for d in contracts["diagnostics"]},
                         {"UNCOVERED_CLASS_MEMBER"})
        self.assertEqual([d["rule"] for d in outer["diagnostics"]],
                         ["MULTIPLE_OUTER_CLASSES", "MULTIPLE_OUTER_CLASSES"])

    def test_source_ancestry_wins_for_envelope_descendants_but_stubs_are_checked(self):
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol): pass\n")
        self.put("src/pkg/port/envelopes/port_envelope.py",
                 "from pkg.port.port import Port\nclass PortEnvelope(Port): pass\n")
        self.put("src/pkg/port/envelopes/port_envelope.pyi",
                 "class PortEnvelope(object): ...\n")
        self.put("src/pkg/port/envelopes/child_port_envelope.py",
                 "from .port_envelope import PortEnvelope\n"
                 "class ChildPortEnvelope(PortEnvelope): pass\n")
        result, _ = self.run_worker()
        self.assertEqual([(d["path"], d["rule"]) for d in result["diagnostics"]],
                         [("src/pkg/port/envelopes/port_envelope.pyi", "ENVELOPE_INHERITANCE")])

    def test_parse_failure_retains_violations_and_malformed_report_fails(self):
        self.put("src/bad.py", "class Broken(\n")
        self.put("tests/wrong.py", "from typing import Protocol\nclass Port(Protocol): pass\n")
        self.put("src/pkg/port/port.py", "from typing import Protocol\n"
                 "class Port(Protocol): pass\n")
        self.put("src/pkg/port/envelopes/wrong.py", "from pkg.port.port import Port\n"
                 "class PortEnvelope(Port): pass\n")
        result, raw = self.run_worker()
        self.assertEqual((result["status"], result["exit_code"], raw["execution_error"]),
                         ("error", 2, "CONTRACT_FOLDERS_PARSE_ERROR"))
        self.assertEqual({d["rule"] for d in result["diagnostics"]},
                         {"CONTRACT_FOLDER", "ENVELOPE_FILE",
                          "CONTRACT_FOLDERS_PARSE_ERROR"})
        files = discover_files(self.root, read_scope(self.root))
        job = next(job for job in make_jobs(self.root, self.run_dir, files, False, [])
                   if job["name"] == "contract-folders")
        broken = execute_job(job, self.root, self.run_dir, 30, capture_fn=lambda *args: {
            "stdout": '{"eryx_contract_folders_adapter": 1, "diagnostics": [], "execution_error": null}',
            "stderr": "", "exit_code": 1, "duration_seconds": 0, "error": None})
        self.assertEqual((broken["status"], broken["error"]),
                         ("error", "CONTRACT_FOLDERS_EXECUTION_ERROR"))


if __name__ == "__main__":
    unittest.main()
