"""Compatibility entry point for outer-class analysis."""
import sys

from quard.outer_classes_worker_utils import ParsedOuterClasses, outer_classes, analyze, main

__all__ = ["ParsedOuterClasses", "outer_classes", "analyze", "main"]

if __name__ == "__main__":
    sys.exit(main())
