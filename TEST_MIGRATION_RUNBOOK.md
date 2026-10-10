# Migrating slow tests to focused checks

Use this runbook when optimizing a test directory that repeatedly runs the
consumer check, native tools, or installation merely to verify one rule. The
goal is to separate **feature behavior** from **delivery into the report**.
Repeated full checks create a combinatorial cost: each source mutation starts
unrelated Pylint, Pyright and other jobs again. See `HANDOFF.md` for measured
profiles and the `tests/quard/architecture_draft/` report-path migration.

## Constraints

- Budget **one or two genuine integration scenarios per migrated test
  directory**, not per test file. Count real runner/native/external-process
  checks, including repeated checks inside one test. Use the smallest relevant
  real job set in each retained report scenario. If behavior genuinely needs
  additional native or runtime coverage, put a focused regression in the
  owning analyzer/runtime suite and explain the exception; relocating the same
  repeated checks does not reduce the work.
- Keep a real scenario for each essential native boundary the directory owns
  (for example Tach and Pylint in `architecture_draft`). An output fake proves
  report orchestration, **not** Tach/Pylint/worker semantics or that the entire
  consumer passed. Keep actual diagnostics when testing native semantics.
- Preserve every distinct behavioral assertion before removing an integration:
  allowed and forbidden cases, source/stub scope, rule, path, line/column,
  execution errors, and relevant statuses/raw logs. A generated `depends_on`
  or `interfaces` assertion proves the generated policy, not native Tach's
  interpretation of it. Cover native-only behaviors such as Tach cycles and
  unused exact dependencies with a real pinned adapter regression.
- Ordinary `check.py` still runs its complete configured profile. Preserve
  native failures and limits: no production job filters, baselines, routine
  `tach sync`, or fake successes in a real check. Follow `AGENTS.md` for the
  consumer and checkout verification requirements. Test fixtures use public,
  autonomous projects, never a private consumer.

## Migration loop

1. **Profile the exact directory or files before changing them.** Use the
   pinned `pytest-durations` plugin, not pytest's built-in `--durations`:

   ```bash
   uv run --locked --group dev python -B -m pytest tests/quard/<group> \
     -q -p no:tach -p no:cacheprovider --pytest-durations=60 \
     --pytest-durations-min=0.005 \
     --pytest-durations-json=/tmp/opencode/<group>-before.json
   ```

   Record elapsed time, passing tests/subtests, slowest **test calls**, and
   setup/teardown separately. Inspect `report["tools"][*]["duration_seconds"]`
   on repeated consumer reports to locate repeated jobs. Jobs run concurrently:
   their durations cannot simply be added to estimate wall time. Fixture
   creation and source builds can be real bottlenecks too; measure them before
    changing them. Keep timing JSON outside the checkout. `-p no:tach` prevents
    Tach's *pytest plugin* from failing during collection; it does not disable
    the native Tach job in `check.py`. `-p no:cacheprovider` avoids leaving a
    `.pytest_cache/` that breaks the source-install clean-tree test. For a plot,
    use `scripts/plot_test_durations.py` on the saved JSON; install matplotlib
    ephemerally instead of adding it to the project dependencies.

2. **Map every assertion to its owner.** For each slow test, list (a) the
   feature/rule it proves, (b) the output it expects from a native tool, (c)
   what report assembly it proves, and (d) its runtime/install behavior.
   Mark each old assertion with its new test location before deleting the
   repeated run. Keep each old case in a subtest or an equivalently specific
   assertion, including positive cases. Use this decision table:

   | Question | Narrowest check |
   | --- | --- |
   | Does source/stub X violate rule Y? | Real rule `analyze(...)` over the actual discovered production/test paths, including `.pyi` where supported; assert error is `None` and positioned findings. |
    | Which roles, dependencies, or exports are generated? | `quard.architecture_draft.generated_architecture_utils.render(root)` or `ConfiguredArchitectureDraft.draft(...)`; inspect `modules`/`interfaces`. |
   | Does the pinned native tool actually reject or allow the case? | One targeted real worker/adapter regression in its owning suite, or one of the retained selected-job report scenarios. Assert real output. |
   | Are diagnostics, statuses, exit codes and logs delivered independently? | `NativeCheckRunner` with deterministic fake job output in `tests/quard/check_runner/`; assert selected jobs, normalized findings, saved report and raw files. |
   | Does installation/apply preserve a file or setting? | Test the installer/apply with a fake uv/probe/other external dependency and assert the real state transition. |

    Existing focused entry points under `quard` include
    `folder_analysis.folder_utils.analyze`,
    `contract_analysis.dependency_type_utils.analyze`,
    `test_placement_analysis.placement_utils.analyze`,
    `outer_classes_worker_utils.analyze`, and
    `contract_analysis.contract_worker_utils.analyze`. Use
   `ConfiguredConsumerPolicy(root).files()` to discover production/test scope
   rather than hand-picking paths that omit stubs. Keep the rule under test
   real; fake only dependencies that are not the behavior being asserted.

3. **Move report-only assertions to runner tests.** Reuse
   `tests/offline_fixture_utils.py::fake_tools()` and existing cases in
    `tests/quard/check_runner/test_native_check_runner.py` and
    `tests/quard/check_runner/test_check_runner.py`. Feed `capture_fn=fake`
    (and a controlled `versions_fn` if needed) to the real runner. Assert job independence,
   `passed`/`violations`/`error`/`blocked`/`skipped`, native exit codes, paths,
   `is_infrastructure_error`, archived raw streams and latest report. Assert
    exactly which fake jobs were invoked. These tests verify orchestration only;
    put a native semantic assertion in a real-tool test. Follow the existing
    runner fixtures for disabling the real clone batch in fake-output tests;
    a fake `capture_fn` alone does not necessarily stop that batch.

4. **Keep only the essential selected real reports.** In architecture tests,
   `tests/quard/architecture_draft/selected_report_utils.py::selected_report`
   selects real job definitions at `make_jobs()`, **before**
   `capture_clone_batch()`; filtering later leaves clone work running. It
   verifies requested versus executed names, statuses, lack of infrastructure
   errors and archived streams. The reference directory retains one report
   for `tach`, `tach-external`, `contract-folders`, and one for
   `pylint-production`, `pylint-tests`. Select only jobs whose findings the
    scenario asserts. Optional disabled jobs may still appear as `skipped`;
    mandatory Pylint remains mandatory on ordinary production checks. A
    selected report cannot claim the full consumer check passed. This reference
    directory also retains runtime subprocess assertions: its **two reports**
    are not a claim that all its cross-process checks fit the broader budget.

5. **Trim fixture costs, then re-profile.** Supply a known public fake plugin
   catalog (`offline_fixtures.CATALOG`) instead of probing installed Pylint
   plugins when a test verifies folder rules or policy generation; retain a
   real discovery/profile case for Pylint. Stub uv/install subprocesses for
   config-only tests; preserve a real source-build lifecycle test when build
   behavior is the subject. Batch independent edits only if each mutation
   still has its own diagnostic assertion; restore files with
   `temporary_edits`. Batching full checks is an interim improvement, not a
   replacement for splitting feature checks from reporting.

6. **Verify the same scope and report honestly.** Repeat step 1 with an
   `-after.json` path, then compare elapsed times **and** sums of test-call
   durations, counts and coverage. One run is a measurement, not a stable
   baseline. Pytest collects tests beyond the `unittest` discovery hook
   (`tests/discovery_utils.py`); do not treat a pytest failure outside the
   migrated scope as a passing unittest suite. Run the affected test files
   during edits; at completion, from the Quard checkout:

   ```bash
   uv sync --locked --group dev
   uv run --locked --group dev python -B -m unittest discover -s tests -v
   uv run --locked --group dev python check.py
   ```

   Run the affected consumer's full static and relevant runtime checks too.
   Inspect `.quard/reports/latest.json`, each `tools[*].status`, actual exit
   codes, `summary.infrastructure_errors` and archived raw logs. Exit 0 means
   all checks passed; exit 1 means violations; exit 2 means configuration/tool
   failure. Record unresolved native findings as findings, never as a green
   check. Confirm selected consumer submodules are clean. Keep reports and
   timing JSON out of committed test fixtures.

**Done when:** every distinct old assertion has a narrow real-behavior test or
an appropriate fake-output report test; each migrated directory has at most
two justified real integration scenarios; the remaining real-job scenarios
check native output and report delivery; before/after profiles and required
checks have recorded actual results. Update `HANDOFF.md` with measurements and
any scope-specific exceptions.
