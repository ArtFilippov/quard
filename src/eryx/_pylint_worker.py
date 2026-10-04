"""Compatibility entry point for the pinned Pylint adapter."""
import sys

from eryx.pylint_worker_utils import main, patch_pathlib_alias_check
sys.dont_write_bytecode = True

__all__ = ["main", "patch_pathlib_alias_check"]

if __name__ == "__main__":
    sys.exit(main())
