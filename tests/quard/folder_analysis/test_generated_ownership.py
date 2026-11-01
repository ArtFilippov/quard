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
from quard.architecture_draft.generated_selection_utils import IDENTITY
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
