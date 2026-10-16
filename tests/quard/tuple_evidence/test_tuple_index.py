"""Autonomous regressions for the deferred tuple-signature check."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from typing import override

from jsonschema import validate

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from quard._policy import discover_files, read_checks, read_scope
from quard.check import execute_job, make_jobs
from quard.check_selection_utils import validate_checks
from quard.check_runner.native_check_runner import NativeCheckRunner
from quard.tuple_signature_utils import analyze
from offline_fixtures import create_repo, fake_tools


class TestTupleIndex(unittest.TestCase):
    """Exercise source evidence and the selected consumer job.

    Args:
        methodName (str, default="runTest"): Test case name.
    """

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path.cwd()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        create_repo(self.root)

    def put(self, path: str, code: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")

    def test_nested_tuple_aliases_across_source_and_stub(self):
        self.put("src/demo/types.py", "type Pair[T] = tuple[T, int]\n")
        self.put("src/demo/types.pyi", "type Pair[T] = tuple[T, int]\n")
        self.put("src/demo/service.py", '''\
from .types import Pair as Result
import typing as tp
from typing import Tuple as Legacy
class Service:
    def call(self, data: list[dict[str, Result[str]]]) -> tp.Tuple[int, str]: ...
async def check(value: Legacy[int, int]) -> "dict[str, tuple[int, ...]]": ...
''')
        self.put("src/demo/service.pyi", '''\
from .types import Pair
def stub(value: Pair[int]) -> None: ...
''')
        self.put("tests/test_tuple.py", "def check(value: tuple) -> None: ...\n")
        paths = ["src/demo/types.py", "src/demo/types.pyi", "src/demo/service.py",
                 "src/demo/service.pyi", "tests/test_tuple.py"]
        findings, error = analyze(self.root, paths)
        self.assertEqual((error, [(item["path"], item["symbol"]) for item in findings]),
                         (None, [("src/demo/service.py", "Service.call.data"),
                                 ("src/demo/service.py", "Service.call"),
                                 ("src/demo/service.py", "check.value"),
                                 ("src/demo/service.py", "check"),
                                 ("src/demo/service.pyi", "stub.value"),
                                 ("tests/test_tuple.py", "check.value")]))

    def test_named_values_collections_and_shadowed_names_pass(self):
        self.put("src/demo/types.py", '''\
from typing import NamedTuple, Literal
class Coordinates(NamedTuple):
    x: int
    y: int
def good(value: list[dict[str, Coordinates]]) -> Literal["tuple[int]"]: ...
tuple = Coordinates
def local(tuple: int) -> tuple: ...
''')
        findings, error = analyze(self.root, ["src/demo/types.py"])
        self.assertEqual((findings, error), ([], None))

    def test_source_and_stub_alias_evidence_stay_separate(self):
        self.put("src/demo/aliases.py", "type Result = list[int]\n")
        self.put("src/demo/aliases.pyi", "type Result = tuple[int, int]\n")
        self.put("src/demo/service.py", "from .aliases import Result\n"
                 + "def run() -> Result: ...\n")
        self.put("src/demo/service.pyi", "from .aliases import Result\n"
                 + "def run() -> Result: ...\n")
        findings, error = analyze(self.root, ["src/demo/aliases.py", "src/demo/aliases.pyi",
                                              "src/demo/service.py", "src/demo/service.pyi"])
        self.assertEqual((error, [item["path"] for item in findings]),
                         (None, ["src/demo/service.pyi"]))

    def test_named_tuple_factories_metadata_and_unknown_aliases_pass(self):
        self.put("src/demo/model.py", '''\
from typing import Annotated, NamedTuple, Literal
import typing as tp
Point = NamedTuple("Point", [("value", tuple[int, str])])
def check(value: Annotated[Point, "tuple[int]"]) -> Literal["tuple[int]"]: ...
def unknown(value: Missing[int]) -> tp.Annotated[dict[str, int], "tuple[int]"]: ...
''')
        findings, error = analyze(self.root, ["src/demo/model.py"])
        self.assertEqual((findings, error), ([], None))

    def test_imported_modules_and_alias_cycles(self):
        self.put("src/demo/aliases.py", "type First = Second\ntype Second = First\n"
                 + "type Pair = dict[str, tuple[int, int]]\n")
        self.put("src/demo/service.py", "import demo.aliases as shapes\n"
                 + "def check(value: shapes.First) -> shapes.Pair: ...\n")
        findings, error = analyze(self.root, ["src/demo/aliases.py", "src/demo/service.py"])
        self.assertEqual((error, [item["symbol"] for item in findings]), (None, ["check"]))

    def test_enabled_job_preserves_positioned_findings(self):
        self.put("src/demo/domain/api.py", "def f(value: tuple[int, str]) -> int: ...\n")
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / ".quard/reports/run"
        jobs = make_jobs(self.root, directory, files, True, [],
                         checks=validate_checks({"tuple_signatures": True}))
        job = next(item for item in jobs if item["name"] == "tuple-signatures")
        result = execute_job(job, self.root, directory, 30)
        self.assertEqual((result["status"], result["exit_code"],
                          [(item["rule"], item["line"], item["column"])
                           for item in result["diagnostics"]]),
                         ("violations", 1, [("RAW_TUPLE_SIGNATURE", 1, 14)]), result)

    def test_missing_consumer_switch_enables_check(self):
        self.assertTrue(validate_checks({})["tuple_signatures"])

    def test_checkout_explicitly_disables_check(self):
        self.assertFalse(read_checks(QUARD)["tuple_signatures"])

    def versions(self, root: Path) -> tuple[dict[str, str], list[dict[str, object]]]:
        return {}, []

    def test_disabled_job_has_no_manifest(self):
        fake, seen = fake_tools()
        with patch("quard.check_runner.native_check_runner.require_source_pin"), patch(
                "quard.check_runner.runner_utils.clone_engine_enabled", return_value=False):
            report = NativeCheckRunner().run(self.root, capture_fn=fake,
                                             versions_fn=self.versions)
        job = next(item for item in report["tools"] if item["name"] == "tuple-signatures")
        directory = (self.root / report["report_path"]).parent
        self.assertEqual((report["exit_code"], job["status"], job["exit_code"],
                          "tuple-signatures" in seen,
                          (directory / "tuple-signatures-files.json").exists()),
                         (0, "skipped", None, False, False))

    def test_full_report_enables_missing_switch(self):
        setting = self.root / ".quard/config.toml"
        setting.write_text(setting.read_text(encoding="utf-8").replace(
            "tuple_signatures = false\n", ""), encoding="utf-8")
        fake, seen = fake_tools()
        with patch("quard.check_runner.native_check_runner.require_source_pin"), patch(
                "quard.check_runner.runner_utils.clone_engine_enabled", return_value=False):
            report = NativeCheckRunner().run(self.root, capture_fn=fake,
                                             versions_fn=self.versions)
        validate(report, json.loads((QUARD / "report.schema.json").read_text(encoding="utf-8")))
        self.assertEqual((report["exit_code"], "tuple-signatures" in seen), (0, True))

    def test_parse_error_keeps_peer_findings(self):
        self.put("src/demo/good.py", "def f() -> tuple[int, str]: ...\n")
        self.put("src/demo/bad.py", "def f(:\n")
        findings, error = analyze(self.root, ["src/demo/good.py", "src/demo/bad.py"])
        self.assertEqual((error, {item["rule"] for item in findings}),
                         ("TUPLE_SIGNATURES_PARSE_ERROR", {"RAW_TUPLE_SIGNATURE",
                                                           "TUPLE_SIGNATURES_PARSE_ERROR"}))

    def test_invalid_manifest_is_infrastructure_failure(self):
        files = discover_files(self.root, read_scope(self.root))
        directory = self.root / ".quard/reports/run"
        jobs = make_jobs(self.root, directory, files, True, [],
                         checks=validate_checks({"tuple_signatures": True}))
        job = next(item for item in jobs if item["name"] == "tuple-signatures")
        (directory / "tuple-signatures-files.json").write_text(
            '["../outside.py"]', encoding="utf-8")
        result = execute_job(job, self.root, directory, 30)
        self.assertEqual((result["status"], result["exit_code"],
                          {(item["rule"], item["is_infrastructure_error"])
                           for item in result["diagnostics"]}),
                         ("error", 2, {("TUPLE_SIGNATURES_EXECUTION_ERROR", True)}))
