# Handoff: speeding up Quard's full check

The user wants the complete static check on private `ArtFilippov/tbd` to approach
10 seconds. Do not run the tbd application or its tests: the user authorized
only `quard/check.py` against its source. Clone detection and LCOM4 are disabled
there and are outside this optimization. Preserve all enabled diagnostics and
native policies; see `AGENTS.md`, `README.md`, `PROFILE.md` and `COMPATIBILITY.md`.

Commit `25d60d6` contains the earlier cached native Pylint similarity hashes
and faster generated v2 Tach serialization. Its original, longer handoff is
available as `git show 25d60d6:HANDOFF.md`.

Measurements on 624 Python files (563 production, 61 tests): the pre-change
Quard checkout ran in 99.631 s and the updated checkout in 66.224 s, both with
exit 0 and no diagnostics. Under higher host load, the older consumer gitlink
took 152.819 s and the updated checkout 113.654 s; do not treat these noisy
runs as a stable benchmark. Profiling showed generated TOML serialization
falling from 16.112 s to 0.119 s. The original native Pylint similarity pass
spent about 35.7 s rehashing files for each pair. Reports are in the disposable
clone under `/tmp/opencode/tbd-checker-perf/.quard/reports/` (run IDs and
methodology are recorded in `git show 25d60d6:HANDOFF.md`).

The next optimization reuses one native isort configuration per Pylint
`ImportsChecker` instance. Pylint 4.0.8 formerly constructed a new config for
every import, defeating isort's own `module_with_reason` LRU cache. The patch
is version-guarded in `src/quard/pylint_imports_utils.py`; both Pylint profiles
load it through the worker. A real-Pylint regression checks that
`wrong-import-order` findings survive and separate checker instances retain
different import categories. An initial attempt using `functools.cache` keyed
by the checker was rejected by this regression: different `ImportsChecker`
instances compare equal. The final implementation caches on each instance.
No native rule, limit, diagnostic or consumer configuration was disabled.

On a synthetic 2700-import classification loop, repeated config construction
took 5.982 s versus 0.004 s with per-instance reuse; categories matched. On
the 624-file private checkout, a baseline run of the previous Quard code took
108.111 s (`20261028T104441Z-69d6803e`), and the final static check took
59.323 s (`20261028T113139Z-5b659c15`). Both exited 0 with zero diagnostics,
identical coverage and identical tool names, statuses and exit codes. Pylint
production was 87.521/48.902 s and Pyright was 37.156/22.813 s, respectively.
Because unchanged Pyright also sped up, host load explains part of the
improvement: **do not attribute the full difference to this patch**. Reports
are in `.quard/cache/tbd-checker-perf/.quard/reports/` in the Quard checkout.
That disposable clone lives on the main disk because `/tmp` has only 3.9 GB;
its consumer tree and pinned Quard submodule stayed clean. The failed partial
`/tmp` environment was removed. The consumer gitlink was not updated, so its
own `quard/check.py` still uses its older pinned version.

Verification: `uv sync --locked --group dev` passed. Full Quard unittest suite
passed **780 tests, 7 skips**, with the Rust developer toolchain on `PATH` and
`CARGO_HOME`/`RUSTUP_HOME` set to this checkout's `.quard/cache/` directories.
An earlier run without these settings and with a full `/tmp` failed for
environmental reasons; it is not a passing result. Final Quard `check.py`
exited **1** with **19,616 native findings** and **zero infrastructure errors**;
five findings in the new compatibility module concern its direct access to the
protected, setter-free Pylint property. Do not report Quard's static check as
green. `git diff --check` passed. No tbd application or tests were run.

**Next:** A 10-second full check has not been achieved. The user has now
approved a narrower Pylint policy; see the decision below before continuing
performance work. Profile remaining Pylint and Pyright time on a stable host.
Flag the protected native API compatibility warnings for separate refactoring
rather than hiding them. Explore persistent analysis only with dependency-aware
invalidation. Keep Quard and consumer histories separate, and do not update a
consumer gitlink without a reachable tool commit.

## Agreed next policy and optimization scope (not yet implemented)

The priority is Quard's custom checks and strict Pyright, not Pylint style or
Pylint's general bug-finding rules. The user explicitly approved reducing the
native Pylint policy and reporting the resulting coverage loss, including
findings for which Pyright has **no** equivalent. Do not claim unchanged
diagnostics or that Pyright replaces every removed rule.

- Retain Pylint `W0150` (`lost-exception`) and the argument/method counts:
  `R0913`, `R0917`, `R0903`, `R0904`. `R0903` is already disabled in the test
  profile; keep that test-profile exception. Retain fatal `F...` tool failures
  and ensure source/config/worker failures remain infrastructure errors rather
  than successful checks. Confirm exact enabled message IDs against pinned
  Pylint 4.0.8 when implementing the profile.
- Disable Pylint `R0401` import cycles, `R0801` similarity, import ordering,
  other style/refactoring rules, and other `E...`/`W...` findings besides
  `W0150`. Pyright and Quard cover some but not all removed rules. In
  particular, the user explicitly accepted losing Pylint-only `W0640`,
  `W1501`, `W1514` and `W1510`. On private `tbd`, Quard clone detection is
  also off, so disabling `R0801` removes clone coverage there altogether.
- Change the `encapsulated_only` default to `false` (including projects whose
  config omits the key); preserve an explicit `true` opt-in. Do not run this job
  during performance optimization. The disposable `tbd` consumer clone has an
  explicit `encapsulated_only = true` in `.quard/config.toml`: setting only the
  Quard default will **not** skip it there. Use a disposable configuration for
  benchmarks, then check that reports show it as `skipped`, with no worker or
  manifest. Avoid altering the private consumer's committed history.
- The native production policy is in `src/quard/policy_utils.py` and the test
  profile is derived from it by `test_pylint()`; the job defaults are in
  `src/quard/check_selection_utils.py`. The current Pylint config enables
  `all` and loads every official extension except `docparams`. Document exact
  retained/removed coverage in `PROFILE.md`, `COMPATIBILITY.md` and the policy
  notes. Update policy regressions, verify full Quard tests/check per `AGENTS.md`,
  and compare static-only `tbd` reports and timings. Never run the `tbd`
  application or its tests.

Previous `tbd` timings (59.323 s total; Pylint production 48.902 s, tests
22.405 s; Pyright 22.813 s; encapsulated-only 17.811 s) describe the **old**
policy and do not predict the speedup from disabling messages. Another profiled
run spent about 21 s generating architecture before launching jobs; its
`Contracts.provenance()` repeatedly sorted and searched all modules. After
the policy change, profile that path and Quard-specific work instead of
assuming that disabled Pylint messages remove all Astroid inference.

## Suggested skills

- `diagnosing-bugs`: reproduce and profile a specific slow check path.
- `codebase-design`: design an incremental analysis seam if profiling supports it.
