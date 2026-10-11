# Handoff: test-suite profiling

The current migration practices and the one-to-two integration-scenarios-per-
directory requirement are in `TEST_MIGRATION_RUNBOOK.md`. Sections below record
the measurement and decision history; the final architecture state is at the end.

## Goal and current state

The user wants to speed up tests and keep them from slowing future development. The
first step was to profile the suite with the **pytest-durations package** (not
pytest's built-in `--durations` flag). `pytest-durations==1.9.0` is now pinned in
the dev group; see `pyproject.toml`, `uv.lock`, and the profiling command in
`README.md` under Development and integration. Independent fixture mutations
now share full-check runs while retaining individual diagnostic assertions.

## Measurements (10 October 2026)

- `uv sync --locked --group dev`: exit 0.
- Pytest with `-p no:tach --pytest-durations=30 --pytest-durations-min=0.005`
  and `--pytest-durations-json=/tmp/opencode/quard-test-durations.json`:
  649.65 s; 406 passed, 1 skipped, 2 reported failures in one test. Timing
  report: `/tmp/opencode/quard-test-durations.json` (local, not committed).
- Slowest calls: `test_real_uv_source_install_reinstall_locked_sync_and_submodule`
  50.64 s; `test_independent_forbidden_mutations` 45.68 s;
  `test_provenance_and_composition_mutations` 45.61 s;
  `test_generated_semantic_boundaries_full_consumer` 44.07 s;
  `test_generated_package_facades_full_consumer_check` 33.55 s. These are
  integration/consumer checks, not fixture setup.
- The JSON records 408 test calls totaling 645.55 s (excluding setup and
  teardown); 296 calls exceed 100 ms, and 74 exceed 1 s. The slowest 30
  account for 79% of total test-call time. This is one run, not a baseline;
  the 649.65 s above is elapsed suite time, not the sum of test calls.
- `test_durations.png` at the repository root plots the slowest 30 by name
  and all 408 by duration rank. Rebuild from the local timing report with
  `uv run --no-project --with matplotlib python scripts/plot_test_durations.py /tmp/opencode/quard-test-durations.json test_durations.png`.
  Matplotlib is used ephemerally; it is not a project dependency. The timing
  JSON remains local and is not committed.
- `uv run --locked --group dev python -B -m unittest discover -s tests -v`:
  exit 0; 388 tests in 617.50 s, 1 skipped.
- `uv run --locked --group dev python check.py`: exit 1, 5087 diagnostics,
  0 infrastructure errors. Native findings remain unresolved. See
  `.quard/reports/latest.json` and archived raw logs for tool statuses and
  details; do not call this check passing or suppress diagnostics to green it.

## Follow-up profiling and optimization (10 October 2026)

- One adoption mutation test made 21 full consumer checks in 43.08 s; their
  Pylint production/test jobs took about 1.8/1.9 s each, versus 0.18 s for
  contract-folders. A 17-check facade test showed the same Pylint/Pyright
  startup bottleneck (29.44 s). Repeating the entire job set per mutation,
  rather than fixture setup, dominates these tests.
- The source-install fixture took 59.43 s on a separate run: its first real
  uv source installation (including the Rust build) took 37.02 s, while the
  two reinstalls took 6.41 and 6.12 s, and full checks 3.82 and 3.40 s.
- Batched independent mutations in `test_adoption_consumer.py` and two full
  consumer tests in `test_configured_architecture_draft.py`. Every case still
  checks its rule, path, job and applicable native Tach finding; temporary
  edits restore fixture files. The adoption mutation tests changed from about
  45.7 to 14.1 s and 45.6 to 26.3 s in targeted runs; semantic boundaries
  changed from 44.1 to 24.6 s and the role matrix from 31.3 to 24.7 s.
- Final full unittest: 388 tests, 1 skipped, exit 0 in 576.29 s (previously
  617.50 s; another post-batching run took 569.21 s). Final native checkout
  check: exit 1, 5088 diagnostics, 0 infrastructure errors; inspect
  `.quard/reports/latest.json` for the unresolved native findings.

## Agreed next step: radically speed up configured architecture tests

The user agreed to split rule behavior from report orchestration, retaining
one real end-to-end Tach scenario and one real Pylint scenario. Focus first on
`tests/quard/architecture_draft/test_configured_architecture_draft.py`.
The saved pytest JSON predates the batching above: its 28 calls total 211.72 s;
13 calls exceed 1 s. Do not treat that total as a current measurement. A rough
post-batching estimate is 180–200 s for this file; a 20–50 s target is a
**hypothesis**, not a measured result. Re-profile before and after.

- Test the many forbidden/allowed mutations with the real, focused rule
  functions, not a full `NativeCheckRunner` per mutation. Existing entry points
  include `quard.folder_analysis.folder_utils.analyze(root, paths)` for generated
  `contract-folders`, `quard.contract_analysis.dependency_type_utils.analyze`,
  `quard.test_placement_analysis.placement_utils.analyze`, and
  `quard.outer_classes_worker_utils.analyze`. Use discovered production/test
  paths, including stubs; assert each case's rule, path, position and absence of
  execution errors. For generated roles, allowed dependencies and public
  exports, inspect `generated_architecture_utils.render(root)` or regenerate
  `tach.toml` and assert its `modules`/`interfaces`, rather than inferring native
  Tach success from a fake. Keep consumer fixtures independent of private code.
- Where a scenario specifically promises a native Tach diagnostic, use the
  real `tach_worker_utils.run("check", root)` (or its real worker) on regenerated
  config and assert its native result. Keep a small real full-check scenario
  that proves the generated policy reaches Tach, plus the real Pylint-profile
  scenario. Do not replace either semantic analyzer with a mock in its own
  behavior test. Existing real full-check coverage also lives in
  `test_adoption_consumer.py`.
- Test report assembly, job independence, statuses, exit codes and archived
  logs separately with deterministic fake job output via `fake_tools()` and
  `NativeCheckRunner().run(root, capture_fn=fake, versions_fn=...)`. Reuse existing
  `test_native_check_runner.py` coverage instead of duplicating it. Fake reports
  prove orchestration only: never claim that mocked native jobs passed, or
  suppress real native failures to make a check green. Keep genuine integration
  for cases explicitly testing failure propagation or real Pylint findings.
- Highest-leverage mutation loops are facades (~17 former full checks, lines
  512–630), external inherited contracts (759–904), semantic boundaries
  (1143–1298), and envelopes/main (1300–1455). Role-matrix (951–1114),
  multiple contracts (632–758), provenance (1473–1535), and directory groups
  (443–511) also mix rule/config assertions with full checks. Preserve runtime
  assertions where they prove executable behavior. Start by measuring each
  targeted test and per-job report durations, then move assertions to the
  narrowest real interface and compare timings with the original coverage.

## Configured-architecture optimization (10 October 2026)

Implemented the split in `test_configured_architecture_draft.py`. Mutation cases
now use the actual discovered production/test scope with the real folder,
dependency-type, placement and outer-class analyzers. Native Tach mutations use
the actual Tach adapter on regenerated configuration. Generated roles and public
exports use `render(root)`. Initial full consumer checks, the real Pylint profiles,
runtime assertions and real full-check failure-propagation coverage remain.

- Same-file pytest profile (`-p no:tach`): 28 passing calls and 46 passing
  subtests; 181.42 s before, 47.63 s after (elapsed). Sum of test calls:
  180.37 s before, 46.53 s after. Timing reports are local at
  `/tmp/opencode/quard-architecture-before.json` and
  `/tmp/opencode/quard-architecture-after.json`. Facades: 32.89 → 3.33 s;
  semantic boundaries: 24.93 → 3.38 s; role matrix: 23.82 → 3.47 s;
  envelopes/main: 23.48 → 3.71 s; external contracts: 19.70 → 3.31 s.
- Pytest should use `-p no:cacheprovider` (now in README), or
  `-o cache_dir=/tmp/opencode/quard-pytest-cache`: its default `.pytest_cache/`
  inside the checkout breaks the source-install test's clean-tree assertion.
  The default cache created during profiling was removed.
- `uv sync --locked --group dev`: exit 0. Final full unittest: 388 tests,
  1 skipped, exit 0, 420.755 s. Checkout `check.py`: exit 1,
  5140 native diagnostics, 0 infrastructure errors; see
  `.quard/reports/latest.json`. These native findings remain unresolved.

## Adoption-consumer optimization (10 October 2026)

Optimized `tests/quard/architecture_draft/test_adoption_consumer.py` using the
same focused-rule approach. Mutation batches discover the actual consumer scope,
including stubs, and run real folder, dependency-type and outer-class analyzers.
Cases requiring native Tach findings regenerate the policy and run the real Tach
adapter; external dependencies use native `check-external`. Each case retains
its rule, path, position and relevant native finding. The clean adoption checks,
runtime checks, Pylint profiles and real failure-propagation scenarios remain
full consumer checks.

- Pytest-durations profiling ran **only this file** with `-p no:tach -p
  no:cacheprovider`: 5 passing tests in 50.06 s before and 29.99 s after
  (another after run took 32.61 s). The two mutation calls changed from
  12.17/22.09 s to 5.61/5.75 s in the first after run. Timing JSON stays
  local at `/tmp/opencode/quard-adoption-before.json` and
  `/tmp/opencode/quard-adoption-after.json` (the latter is the final run).
- `uv sync --locked --group dev`: exit 0. Full unittest verification (not a
  profiling run): 388 tests, 1 skipped, exit 0 in 408.291 s. Checkout
  `check.py`: exit 1, 5148 native diagnostics, 0 infrastructure errors;
  see `.quard/reports/latest.json` for unresolved findings.

## Architecture-suite rerun and clarified next step (10 October 2026)

Reran all four `tests/quard/architecture_draft/test_*.py` files with:

```bash
uv run --locked --group dev python -B -m pytest tests/quard/architecture_draft \
  -q -p no:tach -p no:cacheprovider --pytest-durations=60 \
  --pytest-durations-min=0.005 \
  --pytest-durations-json=/tmp/opencode/quard-architecture-rerun.json
```

- Exit 0: 46 tests and 93 subtests passed in 133.27 s elapsed. Test calls sum
  to 132.34 s; fixtures/setup/teardown were negligible. Per-file test-call
  totals: configured architecture (28) 46.21 s, review follow-up (4) 33.22 s,
  architecture draft (9) 27.57 s, adoption consumer (5) 25.35 s. Timing JSON
  is local, not committed; these figures are one run, not a stable baseline.
- Slowest call: `test_generated_value_contracts_and_independent_forbidden_exposures`
  22.91 s, with 13 full `NativeCheckRunner` invocations. Separately instrumented,
  those checks took 29.03 s of cumulative report wall time; cumulative job
  durations were 27.93 s `pylint-production`, 15.79 s `pylint-tests`, and
  17.89 s `pyright`. Jobs run concurrently, so do not add their durations to
  estimate elapsed time. Four `test_review_followup.py` calls took 6.28–9.04 s
  each and contain 11 full checks in total. The already-focused mutation loops
  generally take about 3–5 s per test. No code or analyzer policy changed in
  this rerun; the checkout-wide `check.py` was not rerun.

**Latest user decision (supersedes the proposed next-step implementation in the
earlier sections):** Preserve the `NativeCheckRunner` report path in these tests,
but execute only the jobs whose diagnostics each test actually verifies. For
architecture scenarios that verify native imports/boundaries, the selected real
job is `tach` (`tach-external` only for external-dependency cases). Other tests
select their actual jobs, such as `contract-folders`, `dependency-types`,
`outer-classes`, `pylint-production`, or `pylint-tests`. Tests of job independence
select exactly the interacting jobs. Do not replace the runner with direct
`analyze()` calls as the main approach, nor execute unrelated jobs and fake their
success. Preserve real native output, statuses, exit codes, paths, raw logs and
positioned findings of every selected job. A selectively executed test report is
not proof that the entire consumer check passed. Ordinary production `check.py`
must continue to run its complete configured profile, without new filters,
baselines or relaxed limits.

Implementation investigation: `NativeCheckRunner.run()` in
`src/quard/check_runner/native_check_runner.py` calls `make_jobs()` then
`capture_clone_batch()` **before** `run_jobs()`. Selecting inside `run_jobs()` or
faking unrelated outputs is too late: clone work may already have executed.
Prefer test-only selection of job definitions before `capture_clone_batch()`;
check how this interacts with automatic report entries for skipped optional jobs,
mandatory Pylint in normal runs, and existing tests that patch `make_jobs()` to
simulate worker errors. Assert the selected tool names as well as their real
results so missing or accidental jobs cannot silently pass. Start with the
13-check draft test and the 11-check review follow-ups, then apply the same
selection to other architecture tests where a full job set is unnecessary.
Compare the same-file and whole-directory pytest-durations profiles before and
after; verify the full unittest suite and the real checkout `check.py` per
`AGENTS.md`, recording any unresolved native diagnostics faithfully.

## Selected real-job architecture reports (10 October 2026)

Applied the latest decision without changing production `NativeCheckRunner` or the
ordinary check profile. `selected_report_utils.py` patches only the test's
`make_jobs()` return value, before clone batching; requested real jobs still run
through `NativeCheckRunner`, and the helper checks exact executed names, real
statuses, no infrastructure errors, and archived raw paths. Optional disabled jobs
may still appear as `skipped`. Adoption and configured-architecture mutation
cases now exercise selected real workers through reports; full adoption,
failure-propagation, Pylint-profile and other end-to-end checks remain real/full.
Report status for a selected set does not imply that a full check passed.

- Same two-file pytest-durations profile (architecture draft + review follow-up):
  13 passing tests, 7 subtests, 64.98 s before and 15.99 s after. The 13-check
  case changed from 30.10 s to 3.56 s; the four review tests totaled about
  28.0 s before and 6.85 s after. JSON: `/tmp/opencode/quard-selected-before.json`
  and `/tmp/opencode/quard-selected-after.json` (local, not committed).
- Whole architecture directory: 46 tests, 93 subtests passed in 110.51 s,
  versus the previous 133.27 s (separate run). JSON:
  `/tmp/opencode/quard-architecture-selected.json`. The configured file alone
  is slower after moving its already-focused direct analyses to real report jobs:
  55.68 s of calls vs 46.21 s in the earlier whole-directory profile. This
  trade-off preserves real report execution for mutations, as requested.
- `uv sync --locked --group dev`: exit 0. Full unittest: 388 tests, 1 skipped,
  exit 0 in 418.449 s. Checkout `check.py`: exit 1 with 5144 native diagnostics,
  0 infrastructure errors. Findings remain unresolved in
  `.quard/reports/latest.json` and archived raw logs. No production tool policy
  was modified.

## Final split: focused rules and minimal real reports (10 October 2026)

The user clarified that tests verifying a combined report must keep the real
`NativeCheckRunner`, selecting the smallest relevant set of jobs; the other
mutation tests should call the real analyzers directly. This supersedes the
earlier change that routed already-fast focused mutations through the runner.
`test_architecture_draft.py` uses direct folder, dependency-type and outer-class
rules and real native Tach for behavioral cases; its report checks select only
the interacting jobs. `test_review_followup.py` uses direct folder analysis and
real Tach for rule cases, retaining a two-job runner report for the facade/Tach
interaction. Adoption and configured-architecture mutation loops again use
direct rules; genuine full-report, Pylint and failure-propagation cases remain.

- Same two-file pytest profile: 13 tests, 7 subtests pass in 8.03 s, compared
  with 15.99 s for the previous selected-report-only version and 64.98 s before
  selection. Local JSON: `/tmp/opencode/quard-architecture-focused-pair.json`.
- Whole architecture directory: 46 tests, 93 subtests pass in 84.22 s versus
  110.51 s for the previous selected-report-only version and 133.27 s before
  selection. Local JSON: `/tmp/opencode/quard-architecture-hybrid.json`. These
  are single-run wall times, not a stable benchmark.
- `uv sync --locked --group dev`: exit 0. Full unittest: 388 tests, 1 skipped,
  exit 0 in 386.996 s. Checkout `check.py`: exit 1 with 5160 native findings,
  0 infrastructure errors; unresolved details and job statuses are in
  `.quard/reports/latest.json` with archived raw logs. No analyzer policy or
  production runner changes.

## Architecture checks split into focused rules and two native reports (10 October 2026)

The architecture-draft directory now keeps two selected real runner invocations:
`test_invalid_companion_facade_blocks_import` runs `tach`, `tach-external`
and `contract-folders`; `test_generated_pylint_report_and_reviewed_profile`
runs `pylint-production` and `pylint-tests` for the generated profile.
Behavioral mutations call the real folder/dependency/placement/class rules or
inspect generated `modules`/`interfaces` directly. Native-report error and
independence assertions live with the fake-output runner tests. Pinned native
Tach cyclic/unused findings have a focused check in the runner tests too.
Other Pylint plugin diagnostics remain covered by the pinned architecture
compatibility tests. Installation probes are stubbed in non-Pylint scenarios.
Production check selection and analyzer policy were not changed.

- Previous architecture-directory profile: 46 tests and 93 subtests in 84.22 s.
  New profile: 43 tests and 93 subtests passing in 14.14 s, with 13.11 s of
  test-call time. Both are single runs, not stable benchmarks. New JSON:
  `/tmp/opencode/quard-architecture-unit-verified.json` (local).
- `uv sync --locked --group dev`: exit 0. Final full unittest: 387 tests,
  1 skipped, exit 0 in 277.737 s. Checkout `check.py`: exit 1, 5188 native
  findings, 0 infrastructure errors; `.quard/reports/latest.json` and raw logs
  retain unresolved findings. Do not interpret this as a passing checkout check.

## Remaining top-30 profiling and migration (11 October 2026)

At the user's request, `test_real_uv_source_install_reinstall_locked_sync_and_submodule`
and `test_real_wheel_uv_install_separate_tool_without_rust` are permanently
skipped. The release wheel workflow no longer invokes the skipped test. The
separate real-wheel full-consumer check and installer smoke probes still run;
neither verifies a real uv Git-source installation or the disabled separate-tool
wheel installation. Documentation now states that gap explicitly.

- Repeated clone mutations in `check_runner` use the installed real
  `quard_clones.SourceSession` over `ConfiguredConsumerPolicy.files()` and actual
  discovered `.py` sources. They assert native counts, original paths, positions,
  duplicates and negative cases. Selected genuine runner reports still verify
  worker output, normalization, archived JSON, errors and independent statuses.
  The full unmocked consumer test now runs one clean and one combined violating
  check for exact, renamed and similar clones; a focused real-engine scan checks
  the strict Type-3 negative case. Real error/adapter/worker report tests in this
  owning runner directory remain justified additional native/report scenarios;
  the two-report budget has not been met for this directory.
- Paired original/patched Pylint compatibility results share one isolated process
  per fixture, but still run real Pylint twice with the original and patched
  checker. A separate fresh patched process retains the before-plugin-loading
  regression; two native-only Protocol fixtures now run in one process. Different
  plugin configurations remain isolated, so the directory has additional
  justified real native scenarios beyond the two-report budget.
- Invalid duplication values are checked individually at the real config parser;
  representative malformed-value and malformed-TOML cases still exercise real
  `apply.py`, `check.py` and installer errors. A switch-matrix test consolidates
  its fully enabled fake-runner assertions. Pytest/unittest discovery now include
  `consumer_policy` and `dispatch_site_model`; the former's test checks the
  actual false default for `dependency_types`, an unknown job, and the distinct
  duplicate-TOML `INVALID_CONFIG` result.
- Before profiles (one run each): `architecture_compatibility` 38 passing in
  65.87 s; `installation` 27 passing, 1 skipped in 64.49 s. The
  `check_runner` before attempt had 66 passing and two **infrastructure failures**
  after simultaneous profiling filled `/tmp`; its 58.00 s is not a passing
  baseline. After: compatibility 38 passing in 38.27 s; installation 26 passing,
  2 skipped in 8.51 s; check_runner 67 passing, 1 skipped in 26.78 s.
  Timing JSON is local at `/tmp/opencode/{architecture-compat,installation,check-runner}-{before,after}.json`
  (check_runner's final after run: `check-runner-after-final.json`).
- Whole-suite pytest-durations: before 257.20 s, 405 passed, 1 skipped, one
  test with two failures; final green run 129.59 s, **404 passed, 3 skipped,
  324 subtests passed**, 407 calls totaling 124.74 s. Slowest remaining call:
  `test_real_wheel_full_consumer_without_rust` at 8.83 s; the final top-30 JSON
  is `/tmp/opencode/quard-test-durations-final.json` (local). Measurements are
  single runs, not a stable benchmark. Unittest: 407 tests, 3 skipped, exit 0
  in 121.349 s (final post-cleanup run: 123.793 s). `uv sync --locked
  --group dev`: exit 0. Final checkout `check.py --json`: exit **1** with
  **5,356 unresolved native findings**, zero infrastructure errors. Pylint
  production/tests: violations with exit 28/30; other violating jobs have exit
  1; clean jobs exit 0; constructor-purity is skipped. See
  `.quard/reports/latest.json` and archived raw logs for all job statuses and
  details. This is not a passing static check. No analyzer policy or ordinary
  production job selection was changed; no consumer submodules were present.

## Incremental Quard tests (11 October 2026)

- The user confirmed `pytest-testmon` (not `pytest-testmom`) for Quard's own tests,
  not consumer projects. `pytest-testmon==2.2.0` is pinned in the dev group and
  `uv.lock`; `.testmondata*` is ignored. The disabled source-install fixture's
  checkout copy excludes this database. `README.md` gives the local command;
  `AGENTS.md` prefers incremental runs during development while retaining full
  `unittest` and `check.py` completion checks. No consumer dependencies or analyzer
  policy changed.
- The working command is `uv run --locked --group dev python -B -m pytest tests -q
  -p no:tach -o cache_dir=.quard/cache/pytest-testmon --testmon`. Testmon needs
  pytest's cache provider: `-p no:cacheprovider` causes a configuration-time
  `KeyError: 'lf'`. The redirected cache stays inside ignored `.quard/cache/`;
  `-p no:tach` still avoids the unrelated Tach pytest-plugin crash.
- The first full testmon collection (`--testmon-noselect`) had one failure in
  `TestCalls.test_default_inequality_delegates_to_marked_equality`; that test
  passed alone with and without testmon, and a repeated full collection passed:
  **441 passed, 3 skipped, 345 subtests passed**, exit 0 in 228.02 s (one
  `TestPlacement` collection warning). With an unchanged tree, `--testmon`
  ran no tests and exited 0. A temporary edit to
  `src/quard/report_parsing/native_report_parser.py` selected **1 test**
  (12 deselected, exit 0); reverting the edit selected that test again. The
  temporary source edit is absent from the final diff.
- `uv sync --locked --group dev`: exit 0. Full `unittest discover -s tests -v`:
  **427 tests, 3 skipped**, exit 0. Checkout `check.py`: exit **1** with
  **6,128 unresolved diagnostics** and **0 infrastructure errors**; statuses,
  native exit codes and raw logs are in `.quard/reports/latest.json` and its
  archived run. No consumer submodules are present. `git diff --check` passes.

## Open work

1. Pytest auto-loads Tach's pytest plugin, which panics on this checkout's
   architecture at configuration time. The README profiling command uses
   `-p no:tach`; this does not disable the native Tach job in `check.py`.
2. The only current pytest collection warning is a production `TestPlacement`
   class with an `__init__`, imported by the folder-ownership test. The latest
   testmon-enabled pytest run counted 444 calls, while unittest counted 427 tests;
   do not infer equal coverage from the older 407-test measurements. The three
   skipped calls include the two requested permanent lifecycle skips, which
   remove real installation lifecycle coverage; do not call these skipped tests
   passing install verification.
3. Remaining tests above one second mostly exercise real Pylint, installer
   smoke probes and one full consumer check. Measure per-job reports before
   moving any further native integration cases.

## Suggested skills

- Load `diagnosing-bugs` when investigating the pytest/Tach crash or test
  discovery mismatch.
- Load `tdd` if making an optimization test-first with a meaningful regression.

Follow `AGENTS.md` for required tool and consumer verification. Keep this
handoff updated or remove it after the follow-up work has been incorporated.
