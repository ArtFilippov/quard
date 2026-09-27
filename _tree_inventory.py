"""Independent, non-following inventory of selected review trees."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from _presidio_seeds import read_identifiers, seed_spans
from _reports import diagnostic
from _contextual_recognizers import SHARED_DIRECTORIES


CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache",
                        ".ruff_cache", ".ipynb_checkpoints"})
ACCOUNT = re.compile(r"(?:^|/)(?:home|Users|scratch|mnt/scratch)/"
                     r"(?P<account>[\w][\w.-]*)(?=$|/)")


def path_spans(path: str, seeds: tuple[str, ...], patterns: tuple[re.Pattern[str], ...]):
    """Identify spans in original relative-path character offsets."""
    spans = {("KNOWN_IDENTIFIER_SEED", start, end) for start, end in seed_spans(path, seeds)}
    spans.update(("KNOWN_IDENTIFIER_PATTERN", match.start(), match.end())
                 for pattern in patterns for match in pattern.finditer(path)
                 if match.start() < match.end())
    spans.update(("LOCAL_PATH_IDENTIFIER", *match.span("account"))
                 for match in ACCOUNT.finditer(path)
                 if match.group("account").casefold() not in SHARED_DIRECTORIES)
    return sorted(spans, key=lambda item: (item[1], item[2], item[0]))


def redact(path: str, spans: list[tuple[str, int, int]]) -> str:
    """Cover overlapping matches without exposing even a partial identifier."""
    mask = [False] * len(path)
    for _, start, end in spans:
        mask[start:end] = [True] * (end - start)
    output = []
    index = 0
    while index < len(path):
        if mask[index]:
            while index < len(path) and mask[index]:
                index += 1
            output.append("[REDACTED]")
        else:
            output.append(path[index])
            index += 1
    return "".join(output)


def inventory(root: Path, directories: list[str], run_id: str, *, hide_all: bool = False,
              identifiers: tuple[tuple[str, ...], tuple[re.Pattern[str], ...]] | None = None):
    """Return public objects and diagnostics plus a private raw-to-display map."""
    seeds, patterns = ((), ()) if hide_all else (identifiers if identifiers is not None else
                                            read_identifiers(root / ".eryx/presidio.toml"))
    objects = {}
    findings = []
    failures = []
    displays = {}
    for selected in directories:
        if selected != ".":
            displays[selected] = ("/".join("[REDACTED]" for _ in selected.split("/")) if hide_all else
                                  redact(selected, path_spans(selected, seeds, patterns)))
    # Open every directory relative to an already-open parent. O_NOFOLLOW on
    # every component prevents a replaced ancestor from redirecting traversal.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    for selected in directories:
        parts = () if selected == "." else Path(selected).parts
        current = None
        try:
            current = os.open(root, flags)
            for part in parts:
                child = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = child
        except OSError:
            if current is not None:
                os.close(current)
            failures.append(selected)
            continue
        pending = [(selected if selected != "." else "", current)]
        while pending:
            parent, fd = pending.pop()
            try:
                try:
                    with os.scandir(fd) as stream:
                        entries = sorted(stream, key=lambda item: item.name)
                except OSError:
                    failures.append(parent or ".")
                    continue
                for entry in entries:
                    relative = parent + "/" + entry.name if parent else entry.name
                    if relative in objects:
                        continue
                    try:
                        mode = entry.stat(follow_symlinks=False).st_mode
                    except OSError:
                        mode = None
                        failures.append(relative)
                    kind = ("directory" if mode is not None and stat.S_ISDIR(mode) else
                            "symlink" if mode is not None and stat.S_ISLNK(mode) else
                            "file" if mode is not None and stat.S_ISREG(mode) else "other")
                    if kind == "directory":
                        try:
                            child = os.open(entry.name, flags, dir_fd=fd)
                            pending.append((relative, child))
                        except OSError:
                            failures.append(relative)
                    objects[relative] = kind
            finally:
                os.close(fd)

    public = []
    object_ids = {}
    for relative, kind in sorted(objects.items()):
        # The selected root itself is not part of its relative package tree.
        selected = min((name for name in directories if name == "." or
                        relative.startswith(name + "/")), key=len, default=".")
        start = 0 if selected == "." else len(selected) + 1
        inspected = relative[start:]
        spans = [(rule, begin + start, end + start)
                 for rule, begin, end in path_spans(inspected, seeds, patterns)]
        # Display paths also hide identifiers in selected-root names, if present.
        display_spans = path_spans(relative, seeds, patterns)
        display = ("/".join("[REDACTED]" for _ in relative.split("/")) if hide_all else
                   redact(relative, display_spans))
        object_id = hashlib.sha256((run_id + "\0" + relative).encode()).hexdigest()[:24]
        object_ids[relative] = object_id
        displays[relative] = display
        public.append({"object_id": object_id, "path": display, "kind": kind,
                       "path_checked": relative not in failures and not hide_all})
        for rule, begin, end in spans:
            findings.append(diagnostic("tree", rule, f"Potential {rule} in tree path.",
                                       path=display, column=begin + 1,
                                       span_length=end - begin, object_id=object_id))
        basename = relative.rsplit("/", 1)[-1]
        artifact = ("CACHE_DIRECTORY" if kind == "directory" and basename in CACHE_DIRS else
                    "BYTECODE_FILE" if kind == "file" and basename.endswith((".pyc", ".pyo")) else
                    "DS_STORE" if basename == ".DS_Store" else None)
        if artifact:
            findings.append(diagnostic("tree", artifact, "Unwanted package artifact present.",
                                       path=display, object_id=object_id))
    for relative in sorted(set(failures)):
        display = displays.get(relative, "<uninspected tree root>")
        findings.append(diagnostic("tree", "TREE_INVENTORY_INCOMPLETE",
                                   "Could not inspect a tree entry or directory.", path=display,
                                    object_id=object_ids.get(relative),
                                   is_infrastructure_error=True))
    return public, findings, displays, len(set(failures))


def safe_paths(value: str, root: Path, displays: dict[str, str],
               seeds: tuple[str, ...] = (), patterns: tuple[re.Pattern[str], ...] = ()) -> str:
    """Replace raw path spellings in reports and archived worker output."""
    for raw in sorted(displays, key=len, reverse=True):
        shown = displays[raw]
        if raw != shown:
            for source, target in ((str(root / raw), str(root / shown)), (raw, shown)):
                # A complete path or component boundary is required: "foo" must
                # not rewrite a distinct name such as "foobar".
                boundary = r"(?=$|/|[\s\"',}:\]])"
                value = re.sub(re.escape(source) + boundary, lambda _: target, value)
                escaped = json.dumps(source)[1:-1]
                value = re.sub(re.escape(escaped) + boundary,
                               lambda _: json.dumps(target)[1:-1], value)
    value = redact(value, path_spans(value, seeds, patterns))
    value = value.replace(json.dumps(str(root))[1:-1], "<consumer-root>")
    return value.replace(str(root), "<consumer-root>")
