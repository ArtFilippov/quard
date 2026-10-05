"""Compatibility entry point for test-placement analysis."""
from quard.contract_analysis.test_placement import TestPlacement, analyze, main, top_level_tests

__all__ = ["TestPlacement", "analyze", "main", "top_level_tests"]


if __name__ == "__main__":
    raise SystemExit(main())
