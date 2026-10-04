# Issue #69: combined migration verification

The checkout at `bd8bf65` plus the compatibility-export and docstring repairs
in this working tree was checked with Python 3.12.14 and the locked dev group.
This is a record of native output, **not** an accepted violations baseline.

## Execution

From the Eryx checkout:

```bash
uv sync --locked --group dev
uv run --locked --group dev python -B -m unittest discover -s tests -v
uv run --locked --group dev python check.py --max-diagnostics 3
```

The autonomous suite ran **283 tests**, passed, with one expected skip:
`test_real_install_separate_clone_and_nonstandard_submodule` explicitly skips
itself under the project's lifecycle policy. The independent
`test_real_native_installer_smoke_probes` passed using real Pylint, Pyright,
pydoclint and Tach executables in an isolated public fixture, including their
positive and negative probes. The installer transaction/rollback cases ran
with mocked uv. The skipped full lifecycle install is **not** claimed as run.

The full Eryx check returned **1 (violations)**. Its complete, untruncated
report is `.eryx/reports/latest.json`, archived at
`.eryx/reports/20261004T185528Z-0e8e8c01/report.json`; the adjacent `raw/`
directory holds native outputs. Reported revision: `bd8bf65`, `dirty: true`.
Summary: **3,920 diagnostics, zero infrastructure errors**; 70 instance-field,
2 LCOM4, 313 pydoclint, 1,516 Pylint and 2,019 Pyright. The CLI's display
limit affects only the terminal, not the report.

| Job | Status | Native exit |
| --- | --- | ---: |
| class-contracts | passed | 0 |
| constructor-purity (disabled by project setting) | skipped | null |
| contract-folders | passed | 0 |
| encapsulated-only | passed | 0 |
| instance-fields | violations | 1 |
| lcom4 | violations | 1 |
| method-decorators | passed | 0 |
| outer-classes | passed | 0 |
| pydoclint | violations | 1 |
| pylint-production | violations | 28 |
| pylint-tests | violations | 28 |
| pyright | violations | 1 |
| tach | passed | 0 |
| tach-external | passed | 0 |
| test-placement | passed | 0 |

Both production (`src/eryx/`, 93 `.py` files) and tests (`tests/`, 26 `.py`
files) were included in full LCOM4 scope. Parsing the actual test-file ASTs
found **zero top-level `test_*` functions**; the apparent occurrence in
`tests/eryx/field_ownership/test_ownership.py` is text inside a test fixture.
The four contract/folder/outer/placement jobs therefore have zero violations;
method-decorators and encapsulated-only are also clean. Constructor purity is
skipped, not passed. Both reviewed Tach boundaries passed. `tach.toml` was not
changed or synchronized; the existing reviewed `eryx` Infrastructure to
`eryx.report_parsing.report_parsing` Domain edge remains in effect. There are
no consumer submodules to update; `git submodule status --recursive` is empty.

## Diagnostic provenance and outstanding findings

Compared with the pre-migration report identified in
[the #62 inventory](issue-62-diagnostic-inventory.md), the full check fell
from 4,917 to 3,920 diagnostics without loosening policy. Among the 70 field
and two cohesion findings, known old behavior includes constructor checker
mutable-field rebinding, external `Contracts.nodes` access and
`FieldOperation`'s zero eligible cohesion methods; moving files changed their
paths. The old `maxDiff` test-field findings remain. These jobs **fail**, and
are not described as clean.

This verification also repaired introduced compatibility re-export warnings:
explicit `__all__` declarations now state the intended legacy public surface,
including `eryx.install.capture` used by the installer transaction. The
`DispatchSite.forbidden_targets` docstring now records its actual default.
The combined check before those repairs had 4,055 diagnostics; afterwards it
has 3,920. The remaining `W0611`/`reportUnusedImport` findings (two each in
`_audit.py` and `_policy.py`) already appeared in the pre-migration report.

**Issue #61's native-diagnostic criterion is not yet met.** Among remaining
findings, `R0901` on `ancestry_traversal/ancestry.py` (four ancestors, limit
three) was absent before migration, and the rule totals still increased for
`R0904` (8→14), `C0413` (58→95), `C0415` (31→34), `DOC101/103/201/203`,
`reportImplicitOverride` (67→77) and other native rules. These totals mix
old relocated violations with potentially introduced ones: they require
class/member-by-member attribution before they can be called resolved or
pre-existing. In particular, the ancestry excess is a confirmed new finding.
The archived report retains every diagnostic and `is_infrastructure_error`
value. The full check remains exit 1. Issues #69, #61, #48 and #49 were
subsequently closed at the user's direction with these unresolved findings
explicitly recorded, rather than counted as passed checks.
