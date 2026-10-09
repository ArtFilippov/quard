# #126: prepared clone performance

The seven-run, end-to-end comparison on 2026-10-09 measured **1.877×**
Rust/CLI (median 0.272/0.145 s). This meets the 2× target and the required
4× ceiling. At this baseline the 4× allowance is 0.580 s, leaving 0.308 s
(2.13× the observed Rust time) of headroom; the 2× target leaves 0.018 s.

## Reproduce

From the Quard checkout on Linux, with Rust available to the developer:

```bash
uv sync --locked --group dev
maturin build --manifest-path rust/quard-clones/Cargo.toml --locked --release \
  --interpreter .venv/bin/python --out /tmp/opencode
uv pip install --python .venv/bin/python --reinstall \
  /tmp/opencode/quard_clones-0.1.0-cp312-abi3-manylinux_2_34_x86_64.whl
.venv/bin/python scripts/benchmark_clones.py --runs 7
```

Use the wheel filename produced by maturin for the current Linux target. If
`maturin` is not installed, use `uvx --from maturin==1.9.6 maturin build` with
the same arguments. Keep the wheel installation **after** `uv sync` and invoke
`.venv/bin/python` directly: `uv run` may remove this opt-in local wheel when
reconciling the ordinary lockfile. Neither Quard's default engine nor the
consumer's dependencies are changed by the script.

The script copies four tracked public Quard Python sources and creates six
small public sources under `src/` and `tests/`: executable exact/renamed/gap
clones, Protocol method declarations, TypedDict fields and an ordinary clean
source. It reports the ten relative paths, per-file SHA-256 and aggregate bytes;
on this run they totaled **52,058 bytes**. Both paths receive the identical
discovered `.py` list, cross-profile scope, `min_lines = 3`, strict mode,
`min_tokens = 1` and the same two-unmatched-line gap limit. CLI uses three
unprepared pinned jscpd 5.4.0 invocations; Rust uses the actual packaged wheel
and one batch worker with one `SourceSession` (one `preparation::prepare` per
original file) for exact, renamed and similar. Expected native findings are
required in all three jobs; their *counts need not be equal* because the Rust
path excludes scaffolding and adds exact declaration units. This fixture
produced CLI **6 / 15 / 1** and Rust **11 / 58 / 2** native findings for
exact / renamed / similar, respectively. All six native reports passed
normalization; the Rust parser validated source digests, per-unit accounting,
original positions and fragments, and all three native kinds.

Each run starts the clock immediately before spawning the first process. The
clock stops after all three processes (or the single Rust batch) have loaded
sources, prepared/detected, written separate native JSON, returned independent
statuses, undergone Quard's real `normalize` parser and written separate
normalized JSON and stdout/stderr logs (including the Rust batch logs).
Config and source-manifest creation precede the clock equally on both sides.
The script warms both paths once,
alternates their order for seven measured repetitions, uses `perf_counter` wall
time and divides **median Rust by median CLI using unrounded times**. All
durations below are seconds rounded to milliseconds; the recorded ratio is
computed before rounding. The inner process time includes startup, loading,
preparation/detection and native report writing, but not Python-side normalization.

| Run | CLI complete | Rust complete | CLI processes | Rust process |
|---:|---:|---:|---:|---:|
| 1 | 0.142 | 0.272 | 0.132 | 0.199 |
| 2 | 0.141 | 0.279 | 0.131 | 0.211 |
| 3 | 0.152 | 0.252 | 0.142 | 0.178 |
| 4 | 0.150 | 0.278 | 0.140 | 0.200 |
| 5 | 0.145 | 0.262 | 0.137 | 0.196 |
| 6 | 0.140 | 0.305 | 0.131 | 0.225 |
| 7 | 0.147 | 0.245 | 0.137 | 0.178 |
| **Median** | **0.145** | **0.272** | **0.137** | **0.199** |

Host: Linux 6.18.40.1-microsoft-standard-WSL2, x86-64, AMD Ryzen 7 5700U
(8 cores/16 logical CPUs), glibc 2.43; CPython 3.12.14, Node 22.22.1,
uv 0.12.21, Rust/Cargo 1.99.0, jscpd 5.4.0, `quard-clones` 0.1.0
(`cpd-core`/`cpd-finder` 0.1.20, `cpd-tokenizer` 0.1.19,
`cpd-reporter` 0.1.21). Local wheel:
`quard_clones-0.1.0-cp312-abi3-manylinux_2_34_x86_64.whl`.

Before optimizing, a three-run same-corpus comparison gave 0.226 s CLI
versus 0.676 s Rust (2.991×). Direct native profiling attributed about
0.148 s to preparation and 0.164 s to renamed detection. Sharing original
line-start offsets among prepared units removes repeated per-token rescans;
parallel independent native candidate-pair scans reduce the renamed cost
without changing candidate generation, detection minimum, findings, source
mapping or report validation. The source is still parsed exactly once per file
and reports remain separate. Timing is deliberately outside the unit suite.

## Verification and unresolved repository diagnostics

`uv sync --locked --group dev` exit 0; `cargo fmt --check`, `cargo check
--locked` and `cargo test --locked` exit 0; the full locked unittest suite exit
0 (**384 tests, one expected skip**), including real packaged-wheel/full
consumer checks, declaration/behavior/gap and mapping regressions. The isolated
wheel-missing fixture now installs the pinned `packaging` dependency so it
reaches the intended version check. No private consumer was required or used;
the real isolated consumer static and runtime checks are part of that suite.

The required checkout `uv run --locked --group dev python check.py` exited
**1**. Report `.quard/reports/20261009T070608Z-91b5aa3b/report.json`
records **4,974** unresolved native diagnostics, **zero infrastructure
errors**, Pylint production/tests exit **28**, violating jobs exit **1**,
passing jobs exit **0**, and opt-out constructor-purity skipped with a null
exit code. The three CLI clone jobs were respectively violations (1),
violations (1), passed (0). The pre-change #125 report also recorded 4,974
native diagnostics; these are not filtered or treated as a passing check.
