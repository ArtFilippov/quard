#!/usr/bin/env python3
"""Compatibility entry point for transactional installation."""
import sys

from quard.install_utils import (STATE_FILES, clone_source, dependency_specs, initialize_state,
                                validate_project, prepare_project, snapshot, rollback,
                                install, main)
from quard._common import capture, require_runtime, write_json
sys.dont_write_bytecode = True

__all__ = ["capture", "require_runtime", "write_json", "STATE_FILES", "clone_source", "dependency_specs",
           "initialize_state", "validate_project", "prepare_project", "snapshot", "rollback",
           "install", "main"]

if __name__ == "__main__":
    sys.exit(main())
