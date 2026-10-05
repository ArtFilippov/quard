"""Compatibility entry point for restricted-call analysis."""
import sys

from quard.encapsulated_only_worker_utils import (Ownership, agreement, Calls, Restrictions,
                                                 analyze, main)

__all__ = ["Ownership", "agreement", "Calls", "Restrictions", "analyze", "main"]

if __name__ == "__main__":
    sys.exit(main())
