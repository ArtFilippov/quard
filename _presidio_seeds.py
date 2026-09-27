"""Consumer-owned identifiers, kept out of reports and process arguments."""
from __future__ import annotations

from pathlib import Path
import re
import tomllib

from presidio_analyzer import EntityRecognizer, RecognizerResult


class IdentifierConfigError(ValueError):
    """Invalid identifier configuration; never include input values."""

    code = "PRESIDIO_IDENTIFIER_CONFIG_ERROR"


class SeedConfigError(IdentifierConfigError):
    """Keep the existing seed-only configuration diagnostic stable."""

    code = "PRESIDIO_SEED_CONFIG_ERROR"


def read_identifiers(path: Path | None) -> tuple[tuple[str, ...], tuple[re.Pattern[str], ...]]:
    """Missing configuration is optional; present but invalid configuration fails."""
    if path is None:
        return (), ()
    try:
        if path.is_symlink():
            raise SeedConfigError("Invalid Presidio seed configuration")
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        return (), ()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise SeedConfigError("Invalid Presidio seed configuration") from None
    if not isinstance(data, dict):
        raise SeedConfigError("Invalid Presidio seed configuration")
    if not set(data) <= {"seeds", "patterns"}:
        error = IdentifierConfigError if "patterns" in data else SeedConfigError
        raise error("Invalid Presidio identifier configuration")
    seeds, patterns = data.get("seeds", []), data.get("patterns", [])
    if (type(seeds) is not list or
            any(type(value) is not str or not value.strip() or
                any(char in value for char in "\r\n\0") for value in seeds)):
        raise SeedConfigError("Invalid Presidio seed configuration")
    if (type(patterns) is not list or
            any(type(value) is not str or not value.strip() or
                any(char in value for char in "\r\n\0") for value in patterns)):
        raise IdentifierConfigError("Invalid Presidio identifier configuration")
    try:
        # ASCII-only expressions keep [a-z] within Latin letters; expressions
        # containing Cyrillic literals retain Unicode case-insensitive matching.
        compiled = tuple(re.compile(value, re.IGNORECASE | (re.ASCII if value.isascii() else 0))
                         for value in patterns)
    except (re.error, RecursionError, OverflowError, ValueError):
        raise IdentifierConfigError("Invalid Presidio identifier configuration") from None
    return tuple(seeds), compiled


def seed_spans(text: str, seeds: tuple[str, ...]) -> list[tuple[int, int]]:
    """Return original character offsets, even when case folding expands a char."""
    folded = text.casefold()
    boundaries = [0]
    for char in text:
        boundaries.append(boundaries[-1] + len(char.casefold()))
    offsets = {position: index for index, position in enumerate(boundaries)}
    spans = set()
    for seed in seeds:
        needle = seed.casefold()
        start = folded.find(needle)
        while start != -1:
            end = start + len(needle)
            if start in offsets and end in offsets and offsets[start] < offsets[end]:
                spans.add((offsets[start], offsets[end]))
            start = folded.find(needle, start + 1)
    return sorted(spans)


class SeedRecognizer(EntityRecognizer):
    """Find every literal seed occurrence, including substrings in code or prose."""

    def __init__(self, language: str, seeds: tuple[str, ...]):
        super().__init__(["KNOWN_IDENTIFIER_SEED"], name="SeedRecognizer", supported_language=language)
        self.seeds = seeds

    def analyze(self, text, entities, nlp_artifacts):
        if "KNOWN_IDENTIFIER_SEED" not in entities:
            return []
        return [RecognizerResult("KNOWN_IDENTIFIER_SEED", start, end, 1.0)
                for start, end in seed_spans(text, self.seeds)]


class IdentifierPatternRecognizer(EntityRecognizer):
    """Find every nonempty match of consumer-supplied regular expressions."""

    def __init__(self, language: str, patterns: tuple[re.Pattern[str], ...]):
        super().__init__(["KNOWN_IDENTIFIER_PATTERN"], name="IdentifierPatternRecognizer",
                         supported_language=language)
        self.patterns = patterns

    def analyze(self, text, entities, nlp_artifacts):
        if "KNOWN_IDENTIFIER_PATTERN" not in entities:
            return []
        spans = {(match.start(), match.end()) for pattern in self.patterns
                 for match in pattern.finditer(text) if match.start() < match.end()}
        return [RecognizerResult("KNOWN_IDENTIFIER_PATTERN", start, end, 1.0)
                for start, end in sorted(spans)]
