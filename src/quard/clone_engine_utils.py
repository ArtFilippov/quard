"""Run the pinned in-memory native Python clone passes."""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import importlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, cast

from packaging.tags import Tag, parse_tag, sys_tags

from quard._common import QuardError, load_toml, read_json, tool_dir
from quard.duplication_utils import read_duplication_min_units

EXPECTED = read_json(tool_dir() / "tools.json")["native_components"]
PARENT = ".."
KINDS = ("exact", "renamed", "similar")
BATCH_KIND = "all"
RENAMED_KIND = "renamed"
SKIP_REASON = "No prepared unit meets the effective minimum"


def enabled(root: Path | None = None) -> bool:
    """Select the source build for consumers; retain CLI in the tool checkout.

    Returns:
        bool: Whether the Rust engine is selected.
    """
    manifest = read_json(tool_dir() / "tools.json")
    if os.environ.get("QUARD_CLONE_ENGINE") == "rust" or manifest.get("native_wheel"):
        return True
    return bool(manifest.get("native_source") and (root is None or root.resolve() != tool_dir().resolve()))


def require_source_pin(root: Path) -> None:
    """Reject consumer checks that could otherwise analyze without a pinned engine."""
    if root.resolve() == tool_dir().resolve() or not read_json(tool_dir() / "tools.json").get("native_source"):
        return
    sources = load_toml(root / "pyproject.toml").get("tool", {}).get("uv", {}).get("sources", {})
    source = sources.get("quard-clones") if isinstance(sources, dict) else None
    if (not isinstance(source, dict) or not isinstance(source.get("git"), str)
            or not source["git"] or source.get("subdirectory") != "rust/quard-clones"
            or not isinstance(source.get("rev"), str)
            or re.fullmatch(r"[0-9a-f]{40}", source["rev"]) is None):
        raise QuardError("CLONE_SOURCE_PIN_REQUIRED",
                         "quard-clones needs a pinned uv Git source (git, rev, subdirectory). "
                         "Reinstall Quard before checking; see docs/clone-cli-migration.md "
                         "(https://github.com/ArtFilippov/quard/blob/main/docs/clone-cli-migration.md).")


def installed_engine() -> dict[str, str]:
    """Load the installed binary and verify its distribution, ABI and linked crates.

    Returns:
        dict[str, str]: The wheel and actual linked crate versions.

    Raises:
        RuntimeError: The wheel, ABI or linked libraries do not match the pin.
    """
    distribution = metadata.distribution("quard-clones")
    if distribution.version != EXPECTED["quard-clones"]:
        raise RuntimeError("quard-clones distribution version mismatch")
    wheel_metadata = distribution.read_text("WHEEL")
    tags: set[Tag] = set()
    for line in (wheel_metadata or "").splitlines():
        if line.startswith("Tag: "):
            tags.update(parse_tag(line[5:].strip()))
    if not tags or not tags.intersection(sys_tags()):
        raise RuntimeError("quard-clones wheel ABI/platform does not match this interpreter")
    native = importlib.import_module("quard_clones")
    if not str(importlib.import_module("quard_clones._native").__file__).endswith(".so"):
        raise RuntimeError("quard-clones native extension is missing")
    versions = dict(native.versions())
    if versions != EXPECTED:
        raise RuntimeError("quard-clones linked engine library version mismatch")
    return versions


def validate_source_evidence(report: dict[str, object], root: Path,
                             paths: list[str], *, skipped: bool,
                             minimum: int | None = None) -> None:
    """Verify each pass's prepared units and the unchanged original source set."""
    originals = [str(root / path) for path in paths]
    counts = report.get("eligibleUnits")
    units = report.get("unitSizes")
    scanned = report.get("scannedUnitSizes")
    unit_counts = report.get("unitCount")
    scanned_counts = report.get("scannedUnitCount")
    digests = report.get("sourceDigests")
    minimum = read_duplication_min_units(root) if minimum is None else minimum
    if (report.get("sourcePaths") != originals or report.get("minimumUnits") != minimum
             or not isinstance(counts, dict) or not isinstance(units, dict)
             or not isinstance(scanned, dict) or not isinstance(unit_counts, dict)
             or not isinstance(scanned_counts, dict)
             or not isinstance(digests, dict)
             or any(set(evidence) != set(originals)
                    for evidence in (counts, units, scanned, unit_counts, scanned_counts, digests))):
        raise ValueError("Incomplete native source evidence")
    candidates = report.get("candidateUnitSizes")
    if candidates is not None and (not isinstance(candidates, dict)
                                    or set(candidates) != set(originals)
                                    or any(not isinstance(lengths, list)
                                           or any(type(length) is not int or length < minimum
                                                  for length in lengths)
                                           for lengths in candidates.values())):
        raise ValueError("Invalid prepared call candidate accounting")
    if report.get("pass") not in KINDS or (report["pass"] == "renamed") != (candidates is not None):
        raise ValueError("Invalid native pass accounting")
    scannable = False
    for original in originals:
        count, lengths, digest = counts[original], units[original], digests[original]
        if (type(count) is not int or count < 0 or not isinstance(lengths, list)
                 or any(type(length) is not int or length < 1 for length in lengths)
                 or sum(lengths) != count
                 or type(unit_counts[original]) is not int or unit_counts[original] != len(lengths)
                 or type(scanned_counts[original]) is not int
                 or scanned_counts[original] != len(scanned[original])
                 or scanned[original] != [length for length in lengths if length >= minimum]
                 or not isinstance(digest, str)
                 or hashlib.sha256(Path(original).read_bytes()).hexdigest() != digest):
            raise ValueError("Invalid prepared source accounting or changed original")
        scannable |= any(length >= minimum for length in lengths)
    if scannable == skipped:
        raise ValueError("Prepared scan status disagrees with eligible units")


def skipped_clone_job(job: dict[str, Any], root: Path, result: dict[str, Any],
                      logs: dict[str, str]) -> dict[str, Any] | None:
    """Return an intentional Rust skip only when original evidence proves it."""
    if not job.get("engine") or result.get("exit_code") != 3 or not job.get("json_file"):
        return None
    try:
        payload = json.loads(job["json_file"].read_text(encoding="utf-8"))
        validate_source_evidence(payload, root, job["expected_paths"], skipped=True)
        if (payload["engine"] != EXPECTED or payload["skipped"] != SKIP_REASON
                or payload["pass"] != job["kind"]):
            return None
    except (ValueError, KeyError, TypeError, OSError, QuardError):
        return None
    return {"name": job["name"], "tool": job["tool"], "status": "skipped",
            "exit_code": None, "duration_seconds": result["duration_seconds"],
            "argv": job["argv"], "raw_output": logs, "diagnostics": [],
            "error": payload["skipped"]}


def load_sources(manifest_path: Path) -> tuple[list[tuple[str, str]], int]:
    """Read the discovered source scope without importing consumer modules.

    Args:
        manifest_path (Path): Path to the job's discovered source manifest.

    Returns:
        tuple[list[tuple[str, str]], int]: Absolute paths, original text and minimum.

    Raises:
        RuntimeError: The manifest is malformed.
    """
    loaded: object = read_json(manifest_path)
    if not isinstance(loaded, dict):
        raise RuntimeError("invalid clone source manifest")
    manifest = cast(dict[str, object], loaded)
    minimum = manifest.get("min_units")
    source_paths = manifest.get("paths")
    if (set(manifest) != {"paths", "min_units"} or not isinstance(source_paths, list)
            or not isinstance(minimum, int)
            or isinstance(minimum, bool) or minimum < 1):
        raise RuntimeError("invalid clone source manifest")
    paths = cast(list[object], source_paths)
    if (any(not isinstance(path, str) or Path(path).is_absolute()
                         or PARENT in Path(path).parts or not path.endswith(".py")
                         for path in paths) or len(set(paths)) != len(paths)):
        raise RuntimeError("invalid clone source paths")
    paths = cast(list[str], paths)
    sources = [(str((Path.cwd() / path).resolve(strict=True)), (Path.cwd() / path).read_text(
        encoding="utf-8")) for path in paths]
    return sources, minimum


def run_pass(session: Any, kind: str, output: Path, count: int,
             evidence: dict[str, Any]) -> dict[str, object]:
    """Archive one independent native pass or an explicit eligible-free skip."""
    if session is not None and (session.exact_scannable() if kind == "exact"
                                else session.renamed_scannable() if kind == "renamed"
                                else session.similar_scannable()):
        report = json.loads(getattr(session, kind)())
        if report["statistics"]["total"]["sources"] != count:
            raise RuntimeError("native source count mismatch")
        code = int(bool(report["duplicates"]))
    else:
        report = {"skipped": SKIP_REASON}
        code = 3
    report.update(evidence)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return {"exit_code": code, "stderr": ""}


def run_passes(args: argparse.Namespace, session: Any, count: int,
               evidence: dict[str, Any]) -> dict[str, dict[str, object]]:
    """Keep one pass's native failure from erasing the other archived results."""
    statuses: dict[str, dict[str, object]] = {}
    for kind in KINDS if args.kind == BATCH_KIND else (args.kind,):
        output = args.output if args.kind != BATCH_KIND else (
            args.output.parent / ("" if kind == "exact" else "jscpd-" + kind) /
            "jscpd-report.json")
        try:
            view = evidence.copy()
            if session is not None:
                view.update({"eligibleUnits": getattr(session, kind + "_eligible_lines")(),
                             "unitSizes": getattr(session, kind + "_unit_lines")(),
                             "scannedUnitSizes": getattr(session, kind + "_scanned_unit_lines")()})
                if kind == "renamed":
                    view["candidateUnitSizes"] = session.call_unit_lines()
            elif kind == "renamed":
                view["candidateUnitSizes"] = {}
            view["unitCount"] = {path: len(sizes) for path, sizes in view["unitSizes"].items()}
            view["scannedUnitCount"] = {path: len(sizes) for path, sizes in
                                        view["scannedUnitSizes"].items()}
            view["pass"] = kind
            validate_source_evidence(view, Path.cwd(), view["sourcePaths"],
                                     skipped=not (session is not None and getattr(
                                         session, kind + "_scannable")()),
                                     minimum=view["minimumUnits"])
            statuses[kind] = run_pass(session, kind, output, count, view)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
            statuses[kind] = {"exit_code": 2, "stderr": f"CLONE_ENGINE_ERROR: {exc}"}
    return statuses


def main() -> int:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--manifest", type=Path, required=True)
    _ = parser.add_argument("--output", type=Path, required=True)
    _ = parser.add_argument("--kind", choices=(*KINDS, BATCH_KIND), default="exact")
    args = parser.parse_args()
    _ = installed_engine()
    native = importlib.import_module("quard_clones")
    sources, minimum = load_sources(args.manifest)
    session = native.SourceSession(sources, minimum,
                                   renamed=args.kind == RENAMED_KIND) if sources else None
    if session is not None and session.source_count() != len(sources):
        raise RuntimeError("incomplete in-memory clone scan")
    evidence = {"engine": dict(native.versions()),
                "sourcePaths": [path for path, _ in sources],
                 "eligibleUnits": {}, "unitSizes": {}, "scannedUnitSizes": {},
                 "minimumUnits": minimum,
                "sourceDigests": {path: hashlib.sha256(text.encode("utf-8")).hexdigest()
                                  for path, text in sources}}
    statuses = run_passes(args, session, len(sources), evidence)
    if args.kind == BATCH_KIND:
        print(json.dumps(statuses))
        return 0
    if statuses[args.kind]["exit_code"] == 2:
        raise RuntimeError(str(statuses[args.kind]["stderr"]))
    return cast(int, statuses[args.kind]["exit_code"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ImportError, OSError, UnicodeError, ValueError, KeyError, TypeError, RuntimeError,
             QuardError) as exc:
        print(f"CLONE_ENGINE_ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
