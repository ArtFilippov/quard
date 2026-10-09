"""Exercise the packaged extension, native reports and original source mappings."""

import json
from importlib import metadata

from quard_clones import SourceSession, versions

assert dict(versions()) == {
    "quard-clones": "0.1.0", "cpd-core": "0.1.20", "cpd-finder": "0.1.20",
    "cpd-reporter": "0.1.21", "cpd-tokenizer": "0.1.19",
}
assert metadata.version("quard-clones") == "0.1.0"
prefix = "def calculate(value):\n"
body = ("    first = value + 17\n    second = first * 3\n"
        "    third = second - 4\n    fourth = third / 2\n    return fourth + 10\n")
paths = ("/tmp/wheel-one.py", "/tmp/wheel-two.py")


def scan(first, second, kind):
    session = SourceSession(list(zip(paths, (first, second))), 3)
    report = json.loads(getattr(session, kind)())
    assert report["statistics"]["total"]["sources"] == 2, report
    assert report["duplicates"], report
    for clone in report["duplicates"]:
        assert clone["kind"] == kind, clone
        assert clone["firstFile"]["name"] in paths, clone
        assert clone["secondFile"]["name"] in paths, clone
        assert ("first =" in clone["fragment"] if kind != "similar"
                else "extra = 1" in clone["fragment"]), clone
        for side, source in (("firstFile", first), ("secondFile", second)):
            part = clone[side]
            assert part["start"] >= 1 and part["end"] >= part["start"], clone
            assert part["end"] <= len(source.splitlines()), clone
            assert part["name"] in paths, clone
        assert clone["fragment"] in first, clone
    return report


scan(prefix + body, prefix + body, "exact")
renamed = body.replace("value", "number").replace("first", "initial")
renamed = renamed.replace("second", "next_value").replace("third", "intermediate")
renamed = renamed.replace("fourth", "final_value")
scan(prefix + body, prefix.replace("value", "number") + renamed, "renamed")
beginning = "    alpha = value + 17\n    beta = alpha * 3\n    gamma = beta - 4\n"
ending = "    left = value + 71\n    right = left * 8\n    total = right - 6\n"
first = prefix + beginning + "    extra = 1\n" + ending
second = prefix + beginning + "    extra = 2\n" + ending
similar = scan(first, second, "similar")
assert any(clone["method"] == "gap" for clone in similar["duplicates"]), similar
clean = SourceSession([(paths[0], "def empty():\n    pass\n")], 3)
assert not clean.scannable()
clean = SourceSession(list(zip(paths, (
    prefix + body,
    "def distinct(value):\n    start = value - 82\n    middle = start ** 3\n"
    "    end = middle % 7\n    result = end // 2\n    return result - 44\n",
))), 3)
for kind in ("exact", "renamed", "similar"):
    assert not json.loads(getattr(clean, kind)())["duplicates"], kind
