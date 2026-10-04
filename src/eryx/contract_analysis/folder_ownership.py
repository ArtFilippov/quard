"""Compatibility imports for the folder ownership analyzer."""
import sys

from eryx.folder_analysis.folder_ownership import FolderOwnership, analyze, main, snake_case

__all__ = ["FolderOwnership", "analyze", "main", "snake_case"]


if __name__ == "__main__":
    sys.exit(main())
