#!/usr/bin/env python3
"""Compatibility entry point for installation-time native probes."""
import sys

from eryx.audit_utils import (probe, discover_plugins, smoke_tests, audit, main)
from eryx._common import tool_binary
sys.dont_write_bytecode = True

__all__ = ["probe", "discover_plugins", "smoke_tests", "audit", "main", "tool_binary"]

if __name__ == "__main__":
    sys.exit(main())
