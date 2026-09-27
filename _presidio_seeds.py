"""Consumer-owned literal identifiers, kept out of reports and process arguments."""
from __future__ import annotations

from pathlib import Path
import tomllib

from presidio_analyzer import EntityRecognizer, RecognizerResult


class SeedConfigError(ValueError):
    """Invalid seed configuration; the exception never contains input values."""


def read_seeds(path: Path | None) -> tuple[str, ...]:
    """Missing configuration is optional; present but invalid configuration fails."""
    if path is None:
        return ()
    try:
        if path.is_symlink():
            raise SeedConfigError("Invalid Presidio seed configuration")
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise SeedConfigError("Invalid Presidio seed configuration") from None
    if (not isinstance(data, dict) or set(data) != {"seeds"} or
            type(data["seeds"]) is not list or
            any(type(seed) is not str or not seed or not seed.strip() or
                any(char in seed for char in "\r\n\0") for seed in data["seeds"])):
        raise SeedConfigError("Invalid Presidio seed configuration")
    return tuple(data["seeds"])


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
