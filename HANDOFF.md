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
3. Remaining slow calls include source builds, fresh-process Pylint compatibility
   regressions, real wheel runs and other full-check mutation loops. Keep real
   integration coverage; use per-job report durations before changing a loop.

## Suggested skills

- Load `diagnosing-bugs` when investigating the pytest/Tach crash or test
  discovery mismatch.
- Load `tdd` if making an optimization test-first with a meaningful regression.

Follow `AGENTS.md` for required tool and consumer verification. Keep this
handoff updated or remove it after the follow-up work has been incorporated.
