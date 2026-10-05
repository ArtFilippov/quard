"""Compatibility imports for the mirrored test-placement analyzer."""
import sys

from quard.test_placement_analysis.test_placement import TestPlacement, analyze, main, top_level_tests

__all__ = ["TestPlacement", "analyze", "main", "top_level_tests"]


if __name__ == "__main__":
    sys.exit(main())
