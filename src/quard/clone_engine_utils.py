"""Run the opt-in in-memory exact pass with the first-party native wheel."""
from __future__ import annotations

import argparse
from importlib import metadata
import importlib
import json
from pathlib import Path
import sys
from typing import cast

from quard._common import read_json, tool_dir

EXPECTED = read_json(tool_dir() / "tools.json")["native_components"]
PARENT = ".."


def main() -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--manifest", type=Path, required=True)
    _ = parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    native = importlib.import_module("quard_clones")
    versions = native.versions
    source_session = native.SourceSession

    if metadata.version("quard-clones") != EXPECTED["quard-clones"] or dict(versions()) != EXPECTED:
        raise RuntimeError("quard-clones wheel or linked engine library version mismatch")
    loaded: object = read_json(args.manifest)
    if not isinstance(loaded, dict):
        raise RuntimeError("invalid clone source manifest")
    manifest = cast(dict[str, object], loaded)
    minimum = manifest.get("min_lines")
    source_paths = manifest.get("paths")
    if (not isinstance(source_paths, list) or not isinstance(minimum, int)
            or isinstance(minimum, bool) or minimum < 1):
        raise RuntimeError("invalid clone source manifest")
    paths = cast(list[object], source_paths)
    if (not paths or any(not isinstance(path, str) or Path(path).is_absolute()
                         or PARENT in Path(path).parts or not path.endswith(".py")
                         for path in paths) or len(set(paths)) != len(paths)):
        raise RuntimeError("invalid clone source paths")
    paths = cast(list[str], paths)
    sources = [(str((Path.cwd() / path).resolve(strict=True)), (Path.cwd() / path).read_text(
        encoding="utf-8")) for path in paths]
    session = source_session(sources, minimum)
    if session.source_count() != len(paths):
        raise RuntimeError("incomplete in-memory clone scan")
    report = json.loads(session.exact())
    if report["statistics"]["total"]["sources"] != len(paths):
        raise RuntimeError("native source count mismatch")
    report["engine"] = dict(versions())
    report["sourcePaths"] = [path for path, _ in sources]
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return int(bool(report["duplicates"]))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"CLONE_ENGINE_ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
