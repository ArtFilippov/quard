"""Include mirrored analyzer suites in Python 3.12 unittest discovery."""

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
        unittest.TestSuite: The suite with all mirrored analyzer tests included.
    """
    del pattern
    root = Path(__file__).resolve().parent / "eryx"
    for group in ("contract_analysis", "method_analysis", "cohesion_analysis"):
        directory = root / group
        suite.addTests(loader.discover(str(directory), pattern="test*.py",
                                       top_level_dir=str(directory)))
    return suite
