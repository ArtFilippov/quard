"""Generated-v2 contract ownership in nested consumer directories."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

QUARD = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(QUARD / "src"))
sys.path.insert(0, str(QUARD / "tests"))

from offline_fixtures import CATALOG, create_repo
from quard._common import write_json
from quard.architecture_draft.generated_selection_utils import IDENTITY, LEGACY_IDENTITY
from quard.check_runner import native_check_runner
from quard.policy_utils import configure


class TestGeneratedOwnership(unittest.TestCase):
    """Exercise generated ownership and test placement through selected report jobs."""

    def consumer(self) -> AbstractContextManager[Path]:
        root = Path(self.enterContext(TemporaryDirectory(prefix="quard-nested-groups-")))
        create_repo(root, ready=False)
        self.put(root, "src/demo/domain/api.py", "")
        self.put(root, "tests/test_value.py", "")
        self.put(root, "src/demo/records/service.py",
                 "from typing import Protocol\nclass Service(Protocol): pass\n")
        self.put(root, "src/demo/records/sender.py",
                 "from demo.records.service import Service\nclass Sender(Service): pass\n")
        write_json(root / ".quard/architecture.generated.json", IDENTITY)
        configure(root, initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)
        self.enterContext(patch.object(native_check_runner, "require_source_pin"))
        self.enterContext(patch("quard.check_runner.runner_utils.require_source_pin"))
        self.enterContext(patch("quard.check_runner.runner_utils.clone_engine_enabled",
                                return_value=False))
        return nullcontext(root)

    def report(self, root: Path) -> dict[str, Any]:
        requested = {"contract-folders", "test-placement"}
        original = native_check_runner.make_jobs

        def select(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [job for job in original(*args, **kwargs) if job["name"] in requested]

        with patch.object(native_check_runner, "make_jobs", side_effect=select):
            report = native_check_runner.NativeCheckRunner().run(root)
        self.assertEqual(report["summary"]["infrastructure_errors"], 0, report["tools"])
        self.assertEqual({job["name"] for job in report["tools"] if job["status"] != "skipped"},
                         requested, report["tools"])
        return report

    def put(self, root: Path, path: str, source: str) -> None:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)

    def contracts(self, root: Path) -> None:
        self.put(root, "src/demo/records/deep/other.py",
                 "from typing import Protocol\nclass Other(Protocol): pass\n")
        self.put(root, "src/gateway/combined/combined.py",
                 "from typing import Protocol\n"
                 "from demo.records.service import Service\n"
                 "from demo.records.deep.other import Other\n"
                 "class Combined(Service, Other, Protocol): pass\n")

    def test_enclosing_and_combined_contracts_own_deep_implementations(self) -> None:
        with self.consumer() as root:
            self.contracts(root)
            for path, name in (
                    ("src/demo/records/neighbor.py", "Neighbor"),
                    ("src/demo/records/deep/child.py", "Child"),
                    ("src/demo/records/deep/other_branch/more/leaf.py", "Leaf")):
                self.put(root, path, "from demo.records.service import Service\n"
                         f"class {name}(Service): pass\n")
            self.put(root, "src/demo/records/deep/other_branch/more/leaf.pyi",
                     "from demo.records.service import Service\nclass Leaf(Service): ...\n")
            for path in ("src/gateway/combined/near.py",
                         "src/gateway/combined/plain/deeper/far.py"):
                name = "Near" if path.endswith("near.py") else "Far"
                self.put(root, path, "from gateway.combined.combined import Combined\n"
                         f"class {name}(Combined): pass\n")
            self.put(root, "tests/demo/records/deep/other_branch/more/test_leaf.py",
                     "class TestLeaf: pass\n")
            report = self.report(root)
            self.assertEqual((report["exit_code"],
                              {job["name"]: job["status"] for job in report["tools"]
                               if job["name"] in {"contract-folders", "test-placement"}}),
                             (0, {"contract-folders": "passed", "test-placement": "passed"}),
                             "An enclosing contract did not own a nested implementation")

    def test_foreign_contracts_stubs_and_misplaced_tests_stay_invalid(self) -> None:
        with self.consumer() as root:
            self.contracts(root)
            self.put(root, "src/gateway/client/foreign.py",
                     "from demo.records.service import Service\nclass Foreign(Service): pass\n")
            self.put(root, "src/demo/records/deep/other_branch/outsider.py",
                     "from gateway.combined.combined import Combined\n"
                     "class Outsider(Combined): pass\n")
            self.put(root, "src/gateway/combined/own.py",
                     "from gateway.combined.combined import Combined\n"
                     "class Own(Combined): pass\n")
            self.put(root, "src/gateway/combined/own.pyi",
                     "from demo.records.service import Service\nclass Own(Service): ...\n")
            self.put(root, "src/demo/records/deep/unknown.py",
                     "from demo.records.service import Service\n"
                     "class Unknown(Service, Ghost): pass\n")
            self.put(root, "src/demo/records/deep/other_branch/more/leaf.py",
                     "from demo.records.service import Service\nclass Leaf(Service): pass\n")
            self.put(root, "tests/demo/records/deep/test_leaf.py", "class TestLeaf: pass\n")
            self.put(root, "src/demo/records/deep/ordinary.py",
                     "from demo.records.sender import Sender\n")
            report = self.report(root)
            self.assertEqual(
                {(item["path"], item["rule"], item["line"], item["column"])
                 for item in report["diagnostics"]},
                {("src/gateway/client/foreign.py", "CROSS_GROUP_CONTRACT", 2, 1),
                 ("src/demo/records/deep/other_branch/outsider.py", "CROSS_GROUP_CONTRACT", 2, 1),
                 ("src/gateway/combined/own.pyi", "CROSS_GROUP_CONTRACT", 2, 1),
                 ("src/demo/records/deep/unknown.py", "UNRESOLVED_GROUP_OWNER", 2, 1),
                 ("src/demo/records/deep/unknown.py", "UNRESOLVED_EXTERNAL_CONTRACT", 2, 1),
                 ("src/demo/records/deep/ordinary.py", "CONCRETE_IMPLEMENTATION_IMPORT", 1, 1),
                 ("tests/demo/records/deep/test_leaf.py", "TEST_CLASS_PLACEMENT", 1, 1)},
                "A foreign contract, stub, ordinary import or misplaced test passed")

    def test_multiple_enclosing_and_same_directory_contracts_have_mirrored_tests(self) -> None:
        with self.consumer() as root:
            self.put(root, "src/demo/records/deep/other.py",
                     "from typing import Protocol\nclass Other(Protocol): pass\n")
            self.put(root, "src/demo/records/deep/another.py",
                     "from typing import Protocol\nclass Another(Protocol): pass\n")
            self.put(root, "src/demo/records/deep/same.py",
                     "from demo.records.deep.other import Other\n"
                     "from demo.records.deep.another import Another\n"
                     "class Same(Other, Another): pass\n")
            self.put(root, "src/demo/records/deep/branch/leaf/local.py",
                     "from typing import Protocol\nclass Local(Protocol): pass\n")
            self.put(root, "src/demo/records/deep/branch/leaf/leaf.py",
                     "from demo.records.service import Service\n"
                     "from demo.records.deep.other import Other\n"
                     "from demo.records.deep.branch.leaf.local import Local\n"
                     "class Leaf(Service, Other, Local): pass\n")
            self.put(root, "tests/demo/records/deep/test_same.py", "class TestSame: pass\n")
            self.put(root, "tests/demo/records/deep/branch/leaf/test_leaf.py",
                     "class TestLeaf: pass\n")
            report = self.report(root)
            self.assertEqual((report["exit_code"],
                              {job["name"]: job["status"] for job in report["tools"]
                               if job["name"] in {"contract-folders", "test-placement"}}),
                             (0, {"contract-folders": "passed", "test-placement": "passed"}),
                             "Multiple local contracts did not allow mirrored targets")

    def test_foreign_sibling_stubs_and_invalid_test_targets_remain_diagnostic(self) -> None:
        with self.consumer() as root:
            self.put(root, "src/demo/sibling/foreign.py",
                     "from typing import Protocol\nclass Foreign(Protocol): pass\n")
            self.put(root, "src/demo/records/deep/branch/implementation.py",
                     "from demo.records.service import Service\n"
                     "from demo.sibling.foreign import Foreign\n"
                     "class Implementation(Service, Foreign): pass\n")
            self.put(root, "src/demo/records/deep/branch/implementation.pyi",
                     "from demo.records.service import Service\n"
                     "from demo.sibling.foreign import Foreign\n"
                     "class Implementation(Service, Foreign): ...\n")
            self.put(root, "src/demo/records/deep/branch/unknown.py",
                     "from demo.records.service import Service\n"
                     "class Unknown(Service, Ghost): pass\n")
            self.put(root, "src/demo/records/deep/branch/loose.py",
                     "class Loose(object): pass\n")
            self.put(root, "tests/demo/records/deep/test_implementation.py",
                     "class TestImplementation: pass\n")
            self.put(root, "tests/demo/records/deep/branch/test_unknown.py",
                     "class TestUnknown: pass\n")
            self.put(root, "tests/demo/records/deep/branch/test_loose.py",
                     "class TestLoose: pass\n")
            report = self.report(root)
            self.assertEqual({(item["path"], item["rule"]) for item in report["diagnostics"]},
                             {("src/demo/records/deep/branch/implementation.py",
                               "CROSS_GROUP_CONTRACT"),
                              ("src/demo/records/deep/branch/implementation.pyi",
                               "CROSS_GROUP_CONTRACT"),
                              ("src/demo/records/deep/branch/unknown.py",
                               "UNRESOLVED_GROUP_OWNER"),
                              ("src/demo/records/deep/branch/unknown.py",
                               "UNRESOLVED_EXTERNAL_CONTRACT"),
                              ("tests/demo/records/deep/test_implementation.py",
                               "TEST_CLASS_PLACEMENT"),
                              ("tests/demo/records/deep/branch/test_unknown.py",
                               "TEST_TARGET_CONTRACT"),
                              ("tests/demo/records/deep/branch/test_loose.py",
                               "TEST_TARGET_CONTRACT")},
                             "Foreign contracts or invalid test targets passed")

    def test_generated_v1_keeps_competing_bases_and_single_test_owner(self) -> None:
        with self.consumer() as root:
            write_json(root / ".quard/architecture.generated.json", LEGACY_IDENTITY)
            self.put(root, "src/demo/first/first.py",
                     "from typing import Protocol\nclass First(Protocol): pass\n")
            self.put(root, "src/demo/second/second.py",
                     "from typing import Protocol\nclass Second(Protocol): pass\n")
            self.put(root, "src/demo/first/double.py",
                     "from demo.first.first import First\n"
                     "from demo.second.second import Second\n"
                     "class Double(First, Second): pass\n")
            self.put(root, "tests/demo/first/test_double.py", "class TestDouble: pass\n")
            report = self.report(root)
            self.assertEqual({(item["path"], item["rule"]) for item in report["diagnostics"]
                             if item["path"] in {"src/demo/first/double.py",
                                                 "tests/demo/first/test_double.py"}},
                             {("src/demo/first/double.py", "COMPETING_LOCAL_BASES"),
                              ("tests/demo/first/test_double.py", "TEST_TARGET_CONTRACT")},
                             "Generated v1 accepted competing bases or their test target")
