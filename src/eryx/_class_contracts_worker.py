"""Compatibility entry point for the class-contract analyzer."""
from eryx.contract_analysis.contracts import (CONTRACT_BASES, METHOD, Contracts, analyze,
                                              collect_nodes, dotted, main, module_name)

__all__ = ["CONTRACT_BASES", "METHOD", "Contracts", "analyze", "collect_nodes",
           "dotted", "main", "module_name"]


if __name__ == "__main__":
    raise SystemExit(main())
