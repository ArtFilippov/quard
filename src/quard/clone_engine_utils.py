"""Run the opt-in in-memory exact and renamed passes with the native wheel."""
from __future__ import annotations

import argparse
from importlib import metadata
import importlib
import json
from pathlib import Path
import sys
from typing import cast

from quard._common import QuardError, read_json, tool_dir

EXPECTED = read_json(tool_dir() / "tools.json")["native_components"]
PARENT = ".."


def load_sources(manifest_path: Path) -> tuple[list[tuple[str, str]], int]:
    """Read the discovered source scope without importing consumer modules.

    Args:
        manifest_path (Path): Path to the job's discovered source manifest.

    Returns:
        tuple[list[tuple[str, str]], int]: Absolute paths, original text and minimum.

    Raises:
        RuntimeError: The manifest is malformed or has no eligible paths.
    """
    loaded: object = read_json(manifest_path)
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
    return sources, minimum


def main() -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--manifest", type=Path, required=True)
    _ = parser.add_argument("--output", type=Path, required=True)
    _ = parser.add_argument("--kind", choices=("exact", "renamed"), default="exact")
    args = parser.parse_args()
    native = importlib.import_module("quard_clones")
    versions = native.versions
    if metadata.version("quard-clones") != EXPECTED["quard-clones"] or dict(versions()) != EXPECTED:
        raise RuntimeError("quard-clones wheel or linked engine library version mismatch")
    sources, minimum = load_sources(args.manifest)
    session = native.SourceSession(sources, minimum, renamed=args.kind == "renamed")
    if session.source_count() != len(sources):
        raise RuntimeError("incomplete in-memory clone scan")
    report = json.loads(session.renamed() if args.kind == "renamed" else session.exact())
    if report["statistics"]["total"]["sources"] != len(sources):
        raise RuntimeError("native source count mismatch")
    report["engine"] = dict(versions())
    report["sourcePaths"] = [path for path, _ in sources]
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return int(bool(report["duplicates"]))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ImportError, OSError, ValueError, KeyError, TypeError, RuntimeError,
            QuardError) as exc:
        print(f"CLONE_ENGINE_ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
