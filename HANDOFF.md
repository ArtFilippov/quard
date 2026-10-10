# Handoff: test-suite profiling

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

No implementation of this split has been made yet. After changes follow
`AGENTS.md`: run locked dev sync, the full unittest suite, and the checkout's
full `check.py`; report the real exit code and native diagnostics. Run relevant
consumer checks if a consumer is changed. Do not use `tach sync` or relax checks.

## Open work

1. Pytest auto-loads Tach's pytest plugin, which panics on this checkout's
   architecture at configuration time. The README profiling command uses
   `-p no:tach`; this does not disable the native Tach job in `check.py`.
2. Pytest discovers additional tests beyond the `unittest` hook in
   `tests/discovery_utils.py` (notably `consumer_policy` and
   `dispatch_site_model`). `tests/quard/consumer_policy/test_configured_consumer_policy.py::TestConfiguredConsumerPolicy::test_each_new_switch_requires_a_boolean_and_unknown_jobs_fail`
   fails: one subtest mutates a missing/unchanged setting and the final
   assertion expects `INVALID_CHECKS` for duplicate TOML, which produces
   `INVALID_CONFIG`. Investigate and fix the test and discovery mismatch before
   treating pytest as the canonical green suite.
3. Carry out the agreed configured-architecture split above, then re-profile
   tests taking more than one second. Other slow calls include source builds,
   fresh-process Pylint compatibility regressions and real wheel runs. Keep
   real integration coverage; use per-job report durations before changing a loop.

## Suggested skills

- Load `diagnosing-bugs` when investigating the pytest/Tach crash or test
  discovery mismatch.
- Load `tdd` if making an optimization test-first with a meaningful regression.

Follow `AGENTS.md` for required tool and consumer verification. Keep this
handoff updated or remove it after the follow-up work has been incorporated.
