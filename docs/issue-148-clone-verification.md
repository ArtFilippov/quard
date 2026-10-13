# #148: clone policy verification

Run from the Quard checkout after `uv sync --locked --group dev`:

```bash
uv run --locked --group dev python -B -m unittest discover -s tests -v
uv run --locked --group dev python check.py
uv run --locked --group dev python -B scripts/benchmark_clones.py --runs 7
```

The autonomous `test_real_wheel_consolidated_clone_redesign` builds the public
wheel and executes all three passes on isolated discovered production and test
files. It checks #136's different NamedTuple fields and shifted same-declaration
windows, #137's one concatenated literal and multiline collection, and #138's
partial `.to_pylist()` suffix across different conditions. Positive controls
cover cross-profile and same-file exact behavior, renamed behavior, three
complete nested composition calls and gap-similar behavior. Each case checks
archived native JSON, per-job status and exit code, normalized positioned
diagnostics, source fragments and independent pass results. Other analyzers are
simulated at this focused seam; the full unittest suite also runs the public
consumer full static and runtime fixture. No private consumer is necessary.

## Same-scope performance comparison (2026-10-13)

`scripts/benchmark_clones.py` generates a temporary consumer of ten public
`.py` paths (three Quard production sources, one tracked test utility and six
generated examples in `src/` and `tests/`), totaling **53,718 bytes**. The
script prints the precise paths and per-file SHA-256 values for each run. Both
paths receive that identical cross-profile file list, strict native detection,
one-token minimum, numeric threshold **3**, and a maximum two-unit gap. Three
historical jscpd 5.4.0 CLI processes use `--min-lines 3` on *unprepared physical
source lines*; the source-built `quard-clones 0.1.0` wheel uses one batch process
and `min_units = 3` on *eligible units within independent prepared views*.
These thresholds have the same number but **different semantics**; clone counts
must not match. The Rust path found 6/17/1 native exact/renamed/similar clones,
and the CLI found 5/13/1. Each native report was normalized and validated.

After one warm-up for each path, seven alternating-order measured runs included
process startup, parsing/preparation/detection, three native JSON reports,
Quard normalization and separate normalized JSON and raw stream writes.
`perf_counter` wall seconds (display rounded to milliseconds):

| Run | CLI complete | Rust complete |
|---:|---:|---:|
| 1 | 0.146 | 0.223 |
| 2 | 0.148 | 0.212 |
| 3 | 0.141 | 0.193 |
| 4 | 0.139 | 0.207 |
| 5 | 0.137 | 0.203 |
| 6 | 0.139 | 0.224 |
| 7 | 0.144 | 0.203 |
| **Median** | **0.141** | **0.207** |

The ratio, computed from unrounded medians, was **1.470× Rust/CLI**: below the
existing [#126 budget](issue-126-clone-benchmark.md) of **4× required** and
**2× target**. At this run's CLI median, the 4× ceiling is 0.564 s and the 2×
target 0.282 s. The #126 seven-run baseline was 1.877× on its earlier 52,058-byte
revision of this corpus; this is a new measurement, not a same-byte speedup
claim. Host: Linux WSL2 x86-64, AMD Ryzen 7 5700U, glibc 2.43,
CPython 3.12.14, Node 22.22.1; linked crates: cpd-core/finder 0.1.20,
tokenizer 0.1.19, reporter 0.1.21.

## Verification and remaining diagnostics

`uv sync --locked --group dev` exited **0**. The complete locked unittest suite
exited **0** (446 tests, three expected lifecycle skips), including the public
adoption consumer's full static and runtime checks and the unmocked isolated
consumer check. An affected private consumer is not available in this workspace;
its own static/runtime checks and remote-reachable gitlink remain unverified.
There are no checked-out consumer submodules here (`git submodule status` is
empty); the checkout's tool tree stays clean after ordinary fixture checks.

The Quard checkout's full `check.py` exited **1**. Report:
`.quard/reports/20261013T140748Z-51bca038/report.json`. It contains **14,950
unresolved diagnostics**, including **360 exact**, **8,650 renamed** and **10
similar** clone-fragment diagnostics; **zero infrastructure errors** and no
error-status jobs. Actual status / exit code by job:

| Jobs | Status / exit |
|---|---|
| class-contracts, contract-folders, dependency-types, instance-fields, jscpd, jscpd-renamed, jscpd-similar, lcom4, leading-underscores, pydoclint, pyright, tach, tach-external, test-placement | violations / 1 |
| pylint-production | violations / 28 |
| pylint-tests | violations / 30 |
| encapsulated-only, free-function-placement, lambda-injection, method-decorators, outer-classes | passed / 0 |
| constructor-purity (checkout configuration) | skipped / null |

Native diagnostics, source findings and configuration limits were not filtered,
disabled or relaxed for this verification.
