# Issue #62: class-migration diagnostic provenance

This is a comparison of **native, unfiltered full-check output**, not a
violations baseline. Follow-up issues #63–#68 own the repairs. A lower total
does not mean that a new diagnostic is pre-existing; an unchanged count does
not mean that the same locations still fail.

## Recorded comparison

The measurements compare the last revision before the class migration (#43)
with a revision after it (#44 and follow-ups), using the same `uv.lock`,
`tools.json`, readability/scope/checks settings and Python 3.12.14. The later
revision adds a Domain mapping for `report_parsing` and explicitly reviews its
Tach boundary. These are recorded results from the respective reviewed
architecture configurations, not an auto-synced Tach graph or a baseline.
Both checks returned **1**; no consumer or tool source was written by a check.

| Job | Before status/exit | After status/exit |
| --- | --- | --- |
| class-contracts | violations / 1 | passed / 0 |
| constructor-purity | skipped / null | skipped / null |
| contract-folders | passed / 0 | passed / 0 |
| encapsulated-only | passed / 0 | passed / 0 |
| instance-fields | violations / 1 | violations / 1 |
| lcom4 | violations / 1 | violations / 1 |
| method-decorators | passed / 0 | passed / 0 |
| outer-classes | violations / 1 | passed / 0 |
| pydoclint | violations / 1 | violations / 1 |
| pylint-production | violations / 28 | violations / 28 |
| pylint-tests | violations / 30 | violations / 28 |
| pyright | violations / 1 | violations / 1 |
| tach, tach-external | passed / 0 each | passed / 0 each |
| test-placement | violations / 1 | passed / 0 |

Before: run `20261004T160601Z-ef7a0f13`, **4,917 diagnostics**, exit 1;
after: run `20261004T160017Z-738f0b8a`, **4,651 diagnostics**, exit 1.
Both reports have `summary.infrastructure_errors = 0`; none of the jobs has
an `error`. Constructor purity is disabled in both runs, **not** a pass.

## Attribution, not a line-number diff

Compare `(job, rule, message)` and then inspect the declaring class and
source diff. Moved modules change paths and line numbers; in particular the
instance-field messages also embed the module-qualified class name. For
example, `NON_PRIMITIVE_FIELD_REBINDING` on
`_constructor_purity_worker.ConstructorChecker.env` becomes the same violation
on `constructor_analysis.constructor_checker.ConstructorChecker.env` (seven
occurrences on either side). Likewise `Contracts.nodes` has seven external
field accesses on either side; `MarkerIndex` and `DispatchSite` still fail
LCOM4 after moving. Do not classify those as newly introduced simply because
their fingerprint or message changed. The 19 old test-placement, 16 old
outer-class and 74 old class-contract diagnostics are now resolved.

The table gives **current locations/rules to investigate** and identifies
which were introduced by the migration. Counts in parentheses are the full
before → after *rule totals*, not counts of newly introduced occurrences:

| Owner | Introduced findings / evidence in the after report | Existing findings to keep separate |
| --- | --- | --- |
| #63 commands, architecture, installation | `R0903` in `PinnedArchitectureCompatibility`, `ConfiguredCleanArchitectureLayers`, `TransactionalInstallation` (0→5 globally); new wrapper docstrings on `draft_configuration` and `run_checks` introduce `DOC101/103/201/203` at their original command modules. Their service files have new long lines, while `install.py`, `init_architecture.py` and `_architecture_compat.py` also retain older diagnostics. | Older command/compatibility typing, readability and clean-architecture findings remain. `W9003` and `W9006` totals are unchanged (6 and 41). |
| #64 captured jobs and check runner | `R0903` in `CapturedCheckJob` and `NativeCheckRunner` (the other two of the five). The extracted runner has new `C0301` lines and unused imports (`W0611` and Pyright `reportUnusedImport`); the new `run_checks` wrapper docstring has documentation findings in `check.py`. | `check.py` already had unknown-type, complexity and try-block diagnostics; relocating behavior to `native_check_runner.py` does not make those automatically new. |
| #65 core/state/dispatch analyzers | 15 `R6301` on no-op contract declarations in `cohesion_references` and `contract_analysis` (0→15); 3 `R0901` on extracted concrete traversal classes (0→3); 15 additional `W9202` stubs globally (12→27, including ten in `ContractAnalysis` and five in `CohesionReferences`). The new contract/import split produces `W0613` (2→30), `W0611` (2→73), and Pyright `reportUnusedImport` (6→73); inspect individual imports against changed code. New contract methods also contribute to `reportImplicitOverride` (67→104), `reportCallInDefaultInitializer` (9→16), and `R0904` (production 5→8). | Old `W9202` AST visitor stubs, `R0904` on large analyzer classes, field lifecycle violations and LCOM4 on `FieldOperation`/`DispatchSite` moved with their owners. `R0904` and `W9202` totals cannot be used alone to identify newly failing classes. |
| #66 marker/restricted-call analyzers | New `RestrictedTargets` annotations yield 12 `MISSING_FIELD_INITIALIZATION`, and its class docstring yields `DOC601/603`. `Calls`, `Restrictions` and marker modules have moved field diagnostics; the extracted `restricted_calls/calls.py` exceeds the module-line limit (`C0302`). | Old `Calls` rebinding/external-field diagnostics, restricted dispatch complexity, `MarkerIndex` LCOM4, and `W9006` Demeter diagnostics persist. The 42 global `EXTERNAL_INSTANCE_FIELD` occurrences on both sides mostly changed qualified paths. |
| #67 mirrored analyzer tests | `TestOwnership`, `TestCalls`, `TestConstructorChecker`, `TestFolderOwnership`, `TestTestPlacement` have LCOM4 failures; new class-local test helpers and setup fields contribute unknown/missing initialization diagnostics and `reportImplicitOverride` (16 analyzer-test occurrences). New local imports account for some `C0413/0415` and untyped fixture callbacks for `reportUnknownArgumentType`. | Some old test classes already failed LCOM4, field analysis, size limits and typing; e.g. `EncapsulatedOnlyTests` was a large failing test before it became `TestCalls`. Its move is not itself a new violation. |
| #68 mirrored command/integration tests | `TestNativeCheckRunner`, `TestConfiguredConsumerPolicy`, `TestTransactionalInstallation` have new LCOM4 failures, and `TestNativeCheckRunner`'s `runner` has new uninitialized/rebinding findings. Migrated fixtures also introduce `reportUnknownMemberType` and local-import diagnostics. | `ArchitectureCompatTest`, `RunnerTests`, `RepositoryTest` and the former `ArchitectureLayersTest` already had native/test field or cohesion diagnostics; check their relocated equivalents before labeling an occurrence new. |

For context, the full native rule totals most likely to be mistaken for new
failures are: `instance-fields` external access **42→42**, rebinding **68→75**,
unknown type **45→45**, missing initialization **34→45**; LCOM4 **13→12**;
Pylint production `C0301` **329→342**, `R0904` **5→8**, `W9202` **12→27**;
Pyright unknown-member **981→879** and unknown-argument **446→331**.
Many of the latter counts refer to old code relocated under a new path.

For a focused follow-up, select a rule and compare the two recorded reports,
then inspect the source diff to establish the owner and whether the offending
statement changed.

This inventory records the current native failures for the follow-on slices;
it does **not** mark the old unresolved native diagnostics as passing, change
analyzer policy, or store a diagnostic baseline in version control.
