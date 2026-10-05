"""Compatibility entry point for method-binding analysis."""
from quard.method_analysis.declarations import (BUILTINS, FUNCTIONS, IMPLICIT_CLASS_HOOKS,
                                                METHODS, Declarations, analyze, assigned,
                                                function_locals, main, needs_receiver)

__all__ = ["BUILTINS", "FUNCTIONS", "IMPLICIT_CLASS_HOOKS", "METHODS", "Declarations",
           "analyze", "assigned", "function_locals", "main", "needs_receiver"]


if __name__ == "__main__":
    raise SystemExit(main())
