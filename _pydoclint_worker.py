#!/usr/bin/env python3
"""Pass selected doc roots to pinned pydoclint without exposing them in argv."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
sys.dont_write_bytecode = True

from _common import read_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = read_json(args.manifest)
        if not isinstance(paths, list) or not paths or any(type(path) is not str for path in paths):
            return 2
        from click.exceptions import Exit
        from pydoclint.main import main as pydoclint

        try:
            pydoclint.main(args=["--config", str(args.config), "--quiet",
                                 "--show-filenames-in-every-violation-message=True", *paths],
                           standalone_mode=False)
        except Exit as exc:
            return exc.exit_code
        return 0
    except Exception:
        # A third-party traceback could contain source text or private paths.
        return 2


if __name__ == "__main__":
    sys.exit(main())
