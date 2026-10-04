"""Compatibility entry point for cohesion analysis."""
from eryx.cohesion_analysis.classes import (ABSTRACT_DECORATORS, METHOD, SPECIAL_METHODS,
                                             Classes, Declarations, MethodReferences, analyze,
                                             components, dotted, eligible, is_placeholder, main)

__all__ = ["ABSTRACT_DECORATORS", "METHOD", "SPECIAL_METHODS", "Classes", "Declarations",
           "MethodReferences", "analyze", "components", "dotted", "eligible",
           "is_placeholder", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
