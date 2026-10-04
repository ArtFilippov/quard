#!/usr/bin/env python3
"""Compatibility entry point for architecture initialization."""
import sys

from eryx.init_architecture_utils import (draft_configuration, validate_architecture_config,
                                          sync_clean_arch, main)
from eryx._common import emit_event, require_runtime
sys.dont_write_bytecode = True

__all__ = ["draft_configuration", "validate_architecture_config", "sync_clean_arch", "main",
           "emit_event", "require_runtime"]

if __name__ == "__main__":
    sys.exit(main())
