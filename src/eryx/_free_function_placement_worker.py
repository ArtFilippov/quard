"""Compatibility entry point for free-function placement analysis."""

from eryx.free_function_placement_utils import analyze, declarations, main

__all__ = ["analyze", "declarations", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
