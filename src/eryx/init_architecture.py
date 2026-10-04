#!/usr/bin/env python3
"""Initialize native Tach boundaries separately from installation; review before acceptance."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
sys.dont_write_bytecode = True
from typing import Any

from eryx._common import error_exit, emit_event, project_dir, repo_lock, require_runtime
from eryx.architecture_draft.configured_architecture_draft import ConfiguredArchitectureDraft
from eryx.clean_architecture_layers.configured_clean_architecture_layers import ConfiguredCleanArchitectureLayers


def draft_configuration(root: Path) -> tuple[dict[str, Any], list[str]]:
    """Return a convention-based, unapproved draft for offline fixtures.

    Args:
        root (Path): Selected consumer project.

    Returns:
        tuple[dict[str, Any], list[str]]: Tach configuration and review notes.
    """
    return ConfiguredArchitectureDraft(root).draft()


def validate_architecture_config(root: Path) -> dict[str, Any]:
    """Check that a consumer has explicit, reviewable native boundaries."""
    return ConfiguredArchitectureDraft(root).validate()


def sync_clean_arch(root: Path, tach: dict[str, Any]) -> None:
    """Keep the separate native clean-architecture layer map in sync."""
    ConfiguredCleanArchitectureLayers().sync(root, tach)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--accept", action="store_true", help="Accept the reviewed native tach.toml (does not suppress any violations)")
    parser.add_argument("--force", action="store_true", help="Replace an existing Tach configuration with a fresh draft")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        root = project_dir(args.project)
        require_runtime()
        with repo_lock(root):
            event, details = ConfiguredArchitectureDraft(root).initialize(accept=args.accept,
                                                                          force=args.force)
            emit_event(event, json_output=args.json, **details, exit_code=0)
            return 0
    except Exception as exc:
        return error_exit(exc, args.json)


if __name__ == "__main__":
    sys.exit(main())
