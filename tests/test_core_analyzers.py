"""Include mirrored suites in Python 3.12 unittest discovery."""

from pathlib import Path
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
    root = Path(__file__).resolve().parent / "eryx"
    for group in ("contract_analysis", "method_analysis", "cohesion_analysis",
                  "field_ownership", "constructor_analysis", "restricted_calls",
                  "check_job", "check_runner", "clean_architecture_layers", "installation", "outer_classes",
                  "architecture_compatibility", "afk_issue_runner", "report_parsing"):
        directory = root / group
        suite.addTests(loader.discover(str(directory), pattern="test*.py",
                                       top_level_dir=str(directory)))
    return suite
