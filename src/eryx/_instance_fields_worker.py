"""Compatibility entry point for instance-field analysis."""
import sys

from eryx.instance_fields_worker_utils import (Accesses, Binding, Ownership, PROTOCOL_BUILTINS,
                                               assigned_names, module_name, analyze, main)

__all__ = ["Accesses", "Binding", "Ownership", "PROTOCOL_BUILTINS", "assigned_names",
           "module_name", "analyze", "main"]

if __name__ == "__main__":
    sys.exit(main())
