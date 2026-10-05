"""Compatibility entry point for contract-folder analysis."""
from quard.contract_analysis.folder_ownership import FolderOwnership, analyze, main, snake_case

__all__ = ["FolderOwnership", "analyze", "main", "snake_case"]


if __name__ == "__main__":
    raise SystemExit(main())
