# Handoff: speeding up Quard's full check

The user wants the complete static check on private `ArtFilippov/tbd` to approach
10 seconds. Do not run the tbd application or its tests: the user authorized
only `quard/check.py` against its source. Clone detection and LCOM4 are disabled
there and are outside this optimization. Preserve all enabled diagnostics and
native policies; see `AGENTS.md`, `README.md`, `PROFILE.md` and `COMPATIBILITY.md`.

Commit `25d60d6` contains the current optimizations, regression tests and
compatibility notes: cached native Pylint similarity hashes and faster generated
v2 Tach serialization. Its diff is the source of truth; the earlier, longer
handoff remains available as `git show 25d60d6:HANDOFF.md`.

Measurements on 624 Python files (563 production, 61 tests): the pre-change
Quard checkout ran in 99.631 s and the updated checkout in 66.224 s, both with
exit 0 and no diagnostics. Under higher host load, the older consumer gitlink
took 152.819 s and the updated checkout 113.654 s; do not treat these noisy
runs as a stable benchmark. Profiling showed generated TOML serialization
falling from 16.112 s to 0.119 s. The original native Pylint similarity pass
spent about 35.7 s rehashing files for each pair. Reports are in the disposable
clone under `/tmp/opencode/tbd-checker-perf/.quard/reports/` (run IDs and
methodology are recorded in `git show 25d60d6:HANDOFF.md`).

Verification at implementation time: `uv sync --locked --group dev` passed;
the full Quard unittest suite passed 779 tests with 7 skips. Its own final
`check.py` exited 1 with 19,607 existing native findings and zero infrastructure
errors; do not report this as green. The tbd source check passed with exit 0,
zero diagnostics and a clean consumer submodule. The disposable 6.4 GB consumer
environment was cleared. The tbd gitlink was not changed; running its own
`quard/check.py` still uses its pinned older version.

**Next:** profile remaining native Pylint and Pyright costs on a stable host.
Pylint still takes tens of seconds, and Pyright alone about 20 seconds on a
faster run. A 10-second full check has not been achieved. Explore persistent
analysis only with dependency-aware invalidation and unchanged coverage; flag
broader refactoring as a separate issue. Keep Quard and consumer histories
separate, and do not update a consumer gitlink without a reachable tool commit.

## Suggested skills

- `diagnosing-bugs`: reproduce and profile a specific slow check path.
- `codebase-design`: design an incremental analysis seam if profiling supports it.
