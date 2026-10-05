#!/usr/bin/env python3
"""Compatibility entry point for applying consumer policy."""
import sys

from quard.apply_utils import main
sys.dont_write_bytecode = True

if __name__ == "__main__":
    sys.exit(main())
