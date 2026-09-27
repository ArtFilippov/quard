"""Primitive POSIX-style path candidates in scanned text, without semantic inference."""
from __future__ import annotations

import re


# A slash and at least one component, bounded by punctuation/whitespace. Do not
# start in the middle of an URL, remote operand or another slash-delimited token.
# The intentionally narrow component alphabet avoids swallowing quotes, escapes,
# shell operators and source-code delimiters into a candidate.
PATH = re.compile(r"(?<![\w@:/\\.~+-])(?:/(?!/)|(?:\.{1,2}/)|(?:[\w.~+-]+/))"
                  r"[\w.~+@-]+(?:/[\w.~+@-]+)*")


def references(text: str, starts: list[int]):
    """Yield source positions and absolute flags; never export candidate text."""
    from bisect import bisect_right

    for match in PATH.finditer(text):
        line = bisect_right(starts, match.start())
        yield {"line": line, "column": match.start() - starts[line - 1] + 1,
               "span_length": match.end() - match.start(),
               "absolute": match.group().startswith("/")}


def correlate(text: str, starts: list[int], tree: dict[str, str]):
    """Resolve candidates in memory, exporting only opaque tree IDs.

    Leading /, ./ and ../ are anchors rather than matching components. Exact
    component equality preserves the filesystem's case sensitivity.
    """
    from bisect import bisect_right

    objects = [(tuple(path.split("/")), identity) for path, identity in tree.items()]
    for match in PATH.finditer(text):
        value = match.group()
        parts = tuple(part for part in value.split("/") if part and part not in {".", ".."})
        best = 0
        tied: list[str] = []
        for path, identity in objects:
            length = 0
            for left, right in zip(reversed(parts), reversed(path)):
                if left != right:
                    break
                length += 1
            if length > best:
                best, tied = length, [identity]
            elif length and length == best:
                tied.append(identity)
        line = bisect_right(starts, match.start())
        yield {"line": line, "column": match.start() - starts[line - 1] + 1,
               "span_length": match.end() - match.start(), "absolute": value.startswith("/"),
               "suffix_components": best, "candidate_object_ids": sorted(tied)}
