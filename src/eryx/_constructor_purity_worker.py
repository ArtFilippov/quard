"""Compatibility entry point for the constructor-purity analyzer."""
from eryx.constructor_analysis.constructor_checker import (ConstructorChecker, EXECUTION_ERROR,
                                                            FUNCTIONS, PARSE_ERROR, RULE, analyze,
                                                            bindings, main)


if __name__ == "__main__":
    raise SystemExit(main())
