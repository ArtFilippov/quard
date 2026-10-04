"""Map discovered test classes to local production classes and their folders."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
from typing import cast, override

from eryx.contract_analysis.contracts import module_name
from eryx.folder_analysis.folder_ownership import FolderOwnership, snake_case
from eryx.test_placement_analysis.test_placement_analysis import TestPlacementAnalysis


def top_level_tests(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Find module-level tests, including declarations under conditional suites."""
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test_"):
                    found.append(child)
            elif not isinstance(child, ast.ClassDef):
                visit(child)

    visit(tree)
    return found


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


def analyze(root: Path, production: list[str], tests: list[str],
            test_directories: list[str]) -> tuple[list[dict[str, object]], str | None]:
    trees: dict[str, ast.Module] = {}
    diagnostics: list[dict[str, object]] = []
    error = None
    for path in production + tests:
        if (Path(path).is_absolute() or ".." in Path(path).parts
                or Path(path).suffix not in {".py", ".pyi"}):
            raise ValueError(f"Invalid test placement source path: {path!r}")
        try:
            trees[path] = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            error = "TEST_PLACEMENT_PARSE_ERROR"
            diagnostics.append({"rule": error, "path": path, "line": getattr(exc, "lineno", None),
                                "column": getattr(exc, "offset", None), "message": str(exc),
                                "is_infrastructure_error": True})
    placement = TestPlacement(trees, set(production), set(tests), set(test_directories))
    diagnostics.extend(placement.violations())
    return diagnostics, error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        data: object = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Test placement manifest requires production, tests and test_directory_names lists")
        fields = cast(dict[str, object], data)
        if any(not isinstance(fields.get(key), list) or any(
                not isinstance(value, str) for value in cast(list[object], fields[key]))
                for key in ("production", "tests", "test_directory_names")):
            raise ValueError("Test placement manifest requires production, tests and test_directory_names lists")
        entries = cast(dict[str, list[str]], data)
        diagnostics, error = analyze(Path.cwd(), entries["production"], entries["tests"],
                                     entries["test_directory_names"])
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        error = "TEST_PLACEMENT_EXECUTION_ERROR"
        diagnostics = [{"rule": error, "message": str(exc), "is_infrastructure_error": True}]
    print(json.dumps({"eryx_test_placement_adapter": 1, "diagnostics": diagnostics,
                      "execution_error": error}, ensure_ascii=False))
    return 2 if error else (1 if diagnostics else 0)


if __name__ == "__main__":
    sys.exit(main())
