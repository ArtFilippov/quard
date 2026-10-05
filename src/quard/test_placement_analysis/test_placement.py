"""Map discovered test classes to local production classes and their folders."""
from __future__ import annotations

import ast
from pathlib import Path
import sys
from typing import override

from quard.contract_analysis.contracts import module_name
from quard.folder_analysis.folder_ownership import FolderOwnership, snake_case
from quard.test_placement_analysis.placement_utils import analyze, main, top_level_tests
from quard.test_placement_analysis.test_placement_analysis import TestPlacementAnalysis

__all__ = ["TestPlacement", "analyze", "main", "top_level_tests"]


class TestPlacement(TestPlacementAnalysis):
    """Check test names and mirrored locations against discovered production declarations."""

    def __init__(self, trees: dict[str, ast.Module], production: set[str], tests: set[str],
                 test_directories: set[str]):
        super().__init__()
        self.folders = FolderOwnership(trees)
        self.trees = trees
        self.production = production
        self.tests = tests
        self.test_directories = test_directories
        self.targets: dict[str, dict[str, str]] = {}
        for name, node, path in self.folders.all_declarations():
            if path not in production:
                continue
            # A companion stub is one declaration, but different modules are distinct.
            key = name
            if key not in self.targets or path.endswith(".py"):
                self.targets[key] = {"path": path, "class": node.name}

    @override
    def mirrored_folder(self, path: str) -> Path:
        """Return the production directory corresponding to a test directory."""
        parts = Path(path).parts
        if parts[0] == "tests":
            return Path("src", *parts[1:-1])
        # A test directory within src/ mirrors its surrounding production package.
        directory = list(parts[1:-1])
        for index, segment in enumerate(directory):
            if segment in self.test_directories:
                del directory[index]
                break
        return Path("src", *directory)

    @override
    def violations(self) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for path in sorted(self.tests):
            for item in top_level_tests(self.trees[path]):
                results.append({"rule": "TOP_LEVEL_TEST_FUNCTION", "path": path,
                                "line": item.lineno, "column": item.col_offset + 1,
                                "symbol": item.name, "message":
                                f"{path}: move {item.name} into a mapped Test<Class> class."})
        for name, node, path in self.folders.all_declarations():
            if path not in self.tests:
                continue
            symbol = name.removeprefix(module_name(path) + ".")
            location = {"path": path, "line": node.lineno,
                        "column": node.col_offset + 1, "symbol": symbol}
            if not node.name.startswith("Test") or len(node.name) == 4:
                results.append({**location, "rule": "TEST_CLASS_NAME", "message":
                                f"{name}: name the test class Test<Class> for a production class."})
                continue
            target_name = node.name[4:]
            expected_module = "test_" + snake_case(target_name)
            if Path(path).stem != expected_module:
                results.append({**location, "rule": "TEST_MODULE_NAME", "message":
                                f"{name}: declare Test{target_name} in {expected_module}{Path(path).suffix}."})
                continue
            matches = [(qualified, target["path"]) for qualified, target in self.targets.items()
                       if target["class"] == target_name]
            if not matches:
                results.append({**location, "rule": "MISSING_TEST_TARGET", "message":
                                f"{name}: no production class {target_name} was discovered."})
                continue
            folder = self.mirrored_folder(path)
            local = [(qualified, target) for qualified, target in matches
                     if Path(target).parent == folder]
            if not local:
                locations = ", ".join(sorted({str(Path(target).parent) for _, target in matches}))
                results.append({**location, "rule": "TEST_CLASS_PLACEMENT", "message":
                                f"{name}: mirror the production package and contract folder of "
                                + f"{target_name} under this test root (found in {locations})."})
                continue
            if len(local) != 1:
                results.append({**location, "rule": "AMBIGUOUS_TEST_TARGET", "message":
                                f"{name}: multiple production classes named {target_name} in {folder}; "
                                + "choose an unambiguous class name."})
                continue
            target, _ = local[0]
            owners, unknown = self.folders.owners(target)
            if not self.folders.is_contract(target) and (unknown or len(owners) != 1 or any(
                    self.folders.path_for(owner) not in self.production for owner in owners)):
                results.append({**location, "rule": "TEST_TARGET_CONTRACT", "message":
                                f"{name}: production class {target} needs one resolvable local contract."})
        return results


if __name__ == "__main__":
    sys.exit(main())
