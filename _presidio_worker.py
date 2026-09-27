#!/usr/bin/env python3
"""Scan selected UTF-8 text with Presidio; emit only locations and entity types."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from importlib import metadata
import os
from pathlib import Path
import re
import stat
import sys
sys.dont_write_bytecode = True

from _common import read_json, write_json
from _content_policy import content_excluded
from _path_references import references
from _presidio_policy import ALL_ENTITIES, PresidioPolicyError, add_russian_recognizers, verify_inventory
from _presidio_seeds import IdentifierConfigError, read_identifiers

MAX_TEXT_BYTES = 512 * 1024


def file_signature(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def read_stable_file(name: str) -> tuple[bytes, tuple]:
    """Read one complete, bounded file; reject replacements and concurrent edits."""
    source = Path(name)
    with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_TEXT_BYTES:
            raise OSError("Unsupported input")
        raw = stream.read(MAX_TEXT_BYTES + 1)
        after = os.fstat(stream.fileno())
    current = source.stat(follow_symlinks=False)
    if (len(raw) > MAX_TEXT_BYTES or len(raw) != before.st_size or
            file_signature(before) != file_signature(after) or file_signature(before) != file_signature(current)):
        raise OSError("Input changed or exceeds scan limit")
    return raw, file_signature(before)


def unchanged(name: str, signature: tuple) -> bool:
    return file_signature(Path(name).stat(follow_symlinks=False)) == signature


def analyzer(seeds: tuple[str, ...] = (), patterns: tuple[re.Pattern[str], ...] = ()):
    """Load pinned local resources only; never download models during a check."""
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    if (metadata.version("presidio-analyzer") != "2.2.360" or
            metadata.version("en-core-web-sm") != "3.8.0" or
            metadata.version("ru-core-news-sm") != "3.8.0"):
        raise RuntimeError("Unexpected Presidio installation")
    engine = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy", "models": [
            {"lang_code": "en", "model_name": "en_core_web_sm"},
            {"lang_code": "ru", "model_name": "ru_core_news_sm"}],
    }).create_engine()
    engine.load()
    if not engine.is_loaded() or set(engine.nlp) != {"en", "ru"}:
        raise RuntimeError("Required NLP models unavailable")
    analyzer_engine = AnalyzerEngine(nlp_engine=engine, supported_languages=["en", "ru"])
    add_russian_recognizers(analyzer_engine, seeds, patterns)
    verify_inventory(analyzer_engine)
    return analyzer_engine


def scan(paths: list[str], config: Path | None = None) -> tuple[dict, int]:
    """Return structured, value-free findings and confirmed coverage."""
    from detect_secrets.filters.heuristic import is_non_text_file

    report = {"scanned": [], "skipped": [], "excluded": [], "findings": [],
              "references": [], "failed": []}
    try:
        seeds, patterns = read_identifiers(config)
    except IdentifierConfigError as exc:
        return report | {"error": exc.code}, 2
    try:
        engine = analyzer(seeds, patterns) if seeds or patterns else analyzer()
    except PresidioPolicyError:
        return report | {"error": "PRESIDIO_POLICY_MISMATCH"}, 2
    except Exception:
        # NLP initialization exceptions can include sensitive local paths or text.
        return report | {"error": "PRESIDIO_MODEL_UNAVAILABLE"}, 2
    for name in paths:
        if content_excluded(name):
            report["excluded"].append(name)
            continue
        try:
            raw, signature = read_stable_file(name)
            if b"\0" in raw:
                if not unchanged(name, signature):
                    raise OSError("Input changed during classification")
                report["skipped"].append(name)
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                if (is_non_text_file(name) or raw.startswith((b"PK\x03\x04", b"\x1f\x8b", b"\x80\x02", b"\x80\x04"))):
                    if not unchanged(name, signature):
                        raise OSError("Input changed during classification")
                    report["skipped"].append(name)
                    continue
                raise
            starts = [0] + [index + 1 for index, char in enumerate(text) if char == "\n"]
            candidates = [{"path": name, **entry} for entry in references(text, starts)]
            observations = set()
            findings = []
            for language in ("en", "ru"):
                for item in engine.analyze(text=text, language=language):
                    if (item.entity_type not in ALL_ENTITIES or
                            not 0 <= item.start < item.end <= len(text)):
                        raise ValueError("Invalid recognition position")
                    # The two language passes can produce the same observation.
                    # Keep different spans and entity types, even on the same line.
                    key = (item.start, item.end, item.entity_type)
                    if key not in observations:
                        observations.add(key)
                        line = bisect_right(starts, item.start)
                        findings.append({"path": name, "line": line,
                                         "column": item.start - starts[line - 1] + 1,
                                         "span_length": item.end - item.start,
                                         "entity": item.entity_type, "score": round(float(item.score), 4)})
            # A second edit while the models ran invalidates both language passes.
            if not unchanged(name, signature):
                raise OSError("Input changed during analysis")
            report["findings"].extend(findings)
            report["references"].extend(candidates)
            report["scanned"].append(name)
        except Exception:
            # No source text, third-party messages or matched strings leave this worker.
            report["failed"].append(name)
    return report, (2 if report["failed"] or not (report["scanned"] or
                                                    (report["excluded"] and not report["skipped"]))
                    else 1 if report["findings"] or any(ref["absolute"] for ref in report["references"]) else 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        paths = read_json(args.manifest)
        if not isinstance(paths, list) or any(type(path) is not str for path in paths):
            raise ValueError("Invalid input manifest")
        report, code = scan(paths, args.config)
        write_json(args.output, report)
        return code
    except Exception:
        # Even malformed manifests or output errors must not produce source-bearing tracebacks.
        return 2


if __name__ == "__main__":
    sys.exit(main())
