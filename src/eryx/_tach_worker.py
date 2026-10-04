#!/usr/bin/env python3
"""Compatibility entry point for the pinned Tach adapter."""
import sys

from eryx.tach_worker_utils import run, main

__all__ = ["run", "main"]

if __name__ == "__main__":
    sys.exit(main())
