"""Discover mirrored unittest suites without shadowing the production package."""

from pathlib import Path
import sys
import unittest


def load_tests(loader: unittest.TestLoader, suite: unittest.TestSuite,
               pattern: str | None) -> unittest.TestSuite:
    """Discover namespace test directories without shadowing the production package.

    Args:
        loader (unittest.TestLoader): The active unittest loader.
        suite (unittest.TestSuite): The suite receiving the mirrored tests.
        pattern (str | None): The caller's pattern, which selects this entry point only.

    Returns:
        unittest.TestSuite: The suite with all mirrored tests included.
    """
    del pattern
    root = Path(__file__).resolve().parent / "quard"
    sys.path.insert(0, str(root.parents[1] / "src"))
    for group in ("contract_analysis", "folder_analysis", "test_placement_analysis",
                  "method_analysis", "cohesion_analysis", "field_ownership",
                  "constructor_analysis", "restricted_calls", "marker_analysis",
                  "restricted_targets", "consumer_policy", "dispatch_site_model",
                  "check_job", "check_runner", "clean_architecture_layers",
                  "installation", "outer_classes", "architecture_compatibility",
                  "architecture_draft", "report_parsing",
                  "free_function_placement"):
        directory = root / group
        suite.addTests(loader.discover(str(directory), pattern="test*.py",
                                       top_level_dir=str(directory)))
    return suite
