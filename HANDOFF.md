# Handoff: test-suite profiling

## Goal and current state

The user wants to speed up tests and keep them from slowing future development. The
first step was to profile the suite with the **pytest-durations package** (not
pytest's built-in `--durations` flag). `pytest-durations==1.9.0` is now pinned in
the dev group; see `pyproject.toml`, `uv.lock`, and the profiling command in
`README.md` under Development and integration. No test optimizations have been
made yet.

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
- `uv run --locked --group dev python -B -m unittest discover -s tests -v`:
  exit 0; 388 tests in 617.50 s, 1 skipped.
- `uv run --locked --group dev python check.py`: exit 1, 5087 diagnostics,
  0 infrastructure errors. Native findings remain unresolved. See
  `.quard/reports/latest.json` and archived raw logs for tool statuses and
  details; do not call this check passing or suppress diagnostics to green it.

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
3. Use the recorded slow calls to investigate subprocess/install costs. Keep
   real integration coverage; measure before and after each targeted change.

## Suggested skills

- Load `diagnosing-bugs` when investigating the pytest/Tach crash or test
  discovery mismatch.
- Load `tdd` if making an optimization test-first with a meaningful regression.

Follow `AGENTS.md` for required tool and consumer verification. Keep this
handoff updated or remove it after the follow-up work has been incorporated.
