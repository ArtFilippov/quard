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
