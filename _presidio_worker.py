#!/usr/bin/env python3
"""Scan selected UTF-8 text with Presidio; emit only locations and entity types."""
from __future__ import annotations

import argparse
from bisect import bisect_right
from importlib import metadata
from pathlib import Path
import sys
sys.dont_write_bytecode = True

from _common import read_json, write_json
from _presidio_policy import ALL_ENTITIES, PresidioPolicyError, add_russian_recognizers, verify_inventory


def analyzer():
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
    add_russian_recognizers(analyzer_engine)
    verify_inventory(analyzer_engine)
    return analyzer_engine


def scan(paths: list[str]) -> tuple[dict, int]:
    """Return structured, value-free findings and confirmed coverage."""
    from detect_secrets.filters.heuristic import is_non_text_file

    report = {"scanned": [], "skipped": [], "findings": [], "failed": []}
    try:
        engine = analyzer()
    except PresidioPolicyError:
        return report | {"error": "PRESIDIO_POLICY_MISMATCH"}, 2
    except Exception:
        # NLP initialization exceptions can include sensitive local paths or text.
        return report | {"error": "PRESIDIO_MODEL_UNAVAILABLE"}, 2
    for name in paths:
        try:
            source = Path(name)
            if source.is_symlink() or not source.is_file():
                raise OSError("Not a regular file")
            raw = source.read_bytes()
            if b"\0" in raw:
                report["skipped"].append(name)
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                if (is_non_text_file(name) or raw.startswith((b"PK\x03\x04", b"\x1f\x8b", b"\x80\x02", b"\x80\x04"))):
                    report["skipped"].append(name)
                    continue
                raise
            starts = [0] + [index + 1 for index, char in enumerate(text) if char == "\n"]
            observations = set()
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
                        report["findings"].append({"path": name, "line": line,
                                                    "column": item.start - starts[line - 1] + 1,
                                                    "span_length": item.end - item.start,
                                                    "entity": item.entity_type, "score": round(float(item.score), 4)})
            report["scanned"].append(name)
        except Exception:
            # No source text, third-party messages or matched strings leave this worker.
            report["failed"].append(name)
    return report, 2 if report["failed"] or not report["scanned"] else 1 if report["findings"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        paths = read_json(args.manifest)
        if not isinstance(paths, list) or any(type(path) is not str for path in paths):
            raise ValueError("Invalid input manifest")
        report, code = scan(paths)
        write_json(args.output, report)
        return code
    except Exception:
        # Even malformed manifests or output errors must not produce source-bearing tracebacks.
        return 2


if __name__ == "__main__":
    sys.exit(main())
