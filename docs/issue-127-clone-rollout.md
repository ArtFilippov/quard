# #127: pinned-source clone rollout

## Installation decision and consumer rollout

#125 replaced the unpublished four-wheel proposal with a private, pinned uv
Git source build. A new consumer pins `quard-clones==0.1.0` in its dev group
and `[tool.uv.sources].quard-clones` to the selected Quard revision and
`rust/quard-clones` subdirectory. uv builds the wheel with pinned maturin and
`Cargo.lock` using the consumer's Rust/C toolchain; the source and build cache
live outside the tool checkout. The installer checks that an in-project
submodule's staged gitlink matches that revision. Reinstall, locked sync and
full check work in the public fixture from both a separate tool clone and a
nested, nonstandard submodule; neither tool tree is modified. The fixture's
`file://` origin is disposable and cannot serve as a shared consumer pin.

The integration has not been rolled out to an affected private consumer.
At the local verification on 2026-10-10, the remote advertised `b11ad22`
while the checkout contained #125 at `88a3bac` plus uncommitted #127 changes.
A later consumer must pin a Quard commit reachable from its remote, not this
pre-publication worktree state.
Linux x86-64 glibc with CPython 3.12/3.13 has been exercised. ARM64, musl and
CPython 3.14 source installs are not verified and are not claimed supported.
See [README](../README.md#install) for source/lockfile verification and the
[benchmark](issue-126-clone-benchmark.md) for the seven-run 1.877×
Rust/CLI median ratio (under the required 4× ceiling).

## Public real-engine regression matrix

The fixtures build and execute `quard-clones` rather than replacing the
detector with a mock. Focused runner fixtures may simulate other analyzers;
the public `verify_real_full_check` fixture runs the complete static and
relevant runtime checks. The historical CLI remains in Quard's dev group for
the reproducible benchmark; older gitlinks retain their own behavior. Updated
consumers without a source pin fail with exit 2 and the
[migration guide](clone-cli-migration.md).

| Feature / evidence | Autonomous regression |
|---|---|
| #91 file imports, documentation and ordinary class openings excluded; local imports and real body lines retained; same-file negative and executable positive | `test_real_wheel_prepared_imports_and_docstrings`, `test_real_wheel_same_file_class_docstrings_from_91` |
| Definition scaffolding, aliases, assignments, control flow, executable contract/special bodies and placeholders | `test_real_wheel_behavioral_declarations`, `test_real_wheel_behavioral_placeholders` |
| Exact fields in both class and functional TypedDicts, differing fields and original columns | `test_real_wheel_behavioral_declarations`, `test_real_wheel_functional_typed_dict_fields` |
| Ordered Protocol/abstract methods, properties, multiline signatures and ranges | `test_real_wheel_contract_methods` |
| Independent exact/renamed/gap-similar, cross-profile and same-file matches, scope boundaries, eligible minimum and empty-view skips | `test_real_wheel_renamed_prepared`, `test_real_wheel_similar_prepared`, `test_real_wheel_prepared_scope`, `test_real_wheel_same_file_and_mapping` |
| Verbatim fragments, source/digest and per-view accounting, invalid configuration/syntax/mapping/tool output, peer findings retained after one pass fails | `test_real_wheel_prepared_imports_and_docstrings`, `test_real_wheel_invalid_output_retains_independent_diagnostics`, `test_real_wheel_renamed_failure_keeps_exact`, `test_real_wheel_similar_failure_keeps_peers` |
| ABI and linked versions, source-built three-pass and clean packaged smoke, real uv install/reinstall/locked sync/full check, external tool trees and managed writes | `test_real_uv_source_install_reinstall_locked_sync_and_submodule`, `test_real_wheel_rejects_incompatible_abi_tag` |

The isolated installation fixtures verify consumer-owned uv indexes, unrelated
sources, extras and explicit duplication minimums, default `min_lines = 3`
for missing settings, transactional rollback with install logs, and generated
configs. Pylint's required duplicate-code check remains independent of the
three native clone jobs. The linked versions are checked against `tools.json`
on install and check; packaged detection probes archive their output in
`.quard/logs/install-probes/`. The full check archives per-job JSON, streams,
original positions/fragments and explicit skips in `.quard/reports/`.

## Consumer rollout deferred by owner

The owner authorized publishing and closing #127 before checking an affected
private consumer. That consumer's staged gitlink, uv source and lockfile must
later reference a reachable Quard commit. Install/reinstall and locked sync on
its Rust/C machine, full static and relevant runtime checks, build logs and
submodule cleanliness remain **unverified**, not passing. If problems emerge,
open a new issue. Additional ARM64, musl and CPython 3.14 source installs are
also unverified; a four-wheel publication matrix is not needed for source
builds. The public fixture and Quard checkout results below do not represent
the affected consumer.

## Historical local-wheel verification (2026-10-09)

Before the source-build change, `uv sync --locked --group dev` and the locked
385-test suite exited 0 (one expected skip). The checkout static check exited
1 with 4,977 native diagnostics, zero infrastructure errors; archived report:
`.quard/reports/20261009T074454Z-d3a77963/report.json`. Those local-wheel
results did not demonstrate release availability or affected-consumer health.
The current source-build checks and unresolved results must be reported
separately rather than reusing this historical count.

## Source-build verification (2026-10-09)

`uv sync --locked --group dev` exited **0**. The full locked unittest suite
exited **0**: 386 tests, one expected lifecycle skip. This includes the real
source-build install/reinstall/locked-sync/full-check public consumer fixture,
the full static/runtime adoption fixture, all three packaged clone probes and
the real-engine regression matrix above. The public fixtures do not stand in
for an affected private consumer on a reachable source pin.

`uv run --locked --group dev python check.py --json` exited **1**. Archived
report: `.quard/reports/20261009T194058Z-7066c350/report.json` (also at
`.quard/reports/latest.json`). It records **5,019 unresolved native
diagnostics** and **zero infrastructure errors**. Per-tool status and actual
exit code:

| Jobs | Status / exit code |
|---|---|
| class-contracts, dependency-types, instance-fields, jscpd, jscpd-renamed, lcom4, pydoclint, pyright, tach, tach-external, test-placement | violations / 1 |
| pylint-production, pylint-tests | violations / 28 |
| contract-folders, encapsulated-only, free-function-placement, jscpd-similar, method-decorators, outer-classes | passed / 0 |
| constructor-purity | skipped / null |

This tool checkout uses its pinned dev CLI for its three clone jobs;
the source-built engine is verified in the separate public consumer fixture.
No diagnostic was filtered and no numeric or architecture limit was weakened.
The private-consumer static/runtime gate, clean consumer submodules and
reachable gitlink remain outstanding.

## CLI retirement verification (2026-10-10)

The agreed migration removes `jscpd==5.4.0` from the consumer dev dependency
group on reinstall and from Quard's consumer pins/probes. Quard's own dev group
retains the CLI for the benchmark and checkout check. In the public real-source
fixture, a pre-existing CLI dev pin is removed from `pyproject.toml` and
`uv.lock`; source install, reinstall, locked sync with an extra, apply and full
check exit 0, with generated-architecture clone views explicitly skipped
when no prepared unit meets the minimum. Packaged exact, renamed, similar and
clean probes run against the installed wheel. Separate and nested tool trees
remain clean. Reviewed accept flows remain covered by the autonomous suite;
the generated source fixture does not require acceptance. A direct updated
consumer check without a valid source pin exits 2 with
`CLONE_SOURCE_PIN_REQUIRED`, the [migration guide](clone-cli-migration.md),
three explicit clone job errors and no CLI execution. Offline historical CLI
regressions use a test-only seam; they do not activate a consumer fallback.

Final `uv sync --locked --group dev` exited **0**. The full locked suite exited
**0**: 388 tests, one expected lifecycle skip. Quard's full checkout check
exited **1**, with **5,087 unresolved native diagnostics**, **zero
infrastructure errors** and no error-status jobs; report:
`.quard/reports/20261010T140237Z-093833d5/report.json`. Job status / exit:

| Jobs | Status / exit code |
|---|---|
| class-contracts, dependency-types, instance-fields, jscpd, jscpd-renamed, lcom4, pydoclint, pyright, tach, tach-external, test-placement | violations / 1 |
| pylint-production, pylint-tests | violations / 28 |
| contract-folders, encapsulated-only, free-function-placement, jscpd-similar, method-decorators, outer-classes | passed / 0 |
| constructor-purity | skipped / null |

At verification time, no private affected consumer or published updated Quard
commit was available. The owner's later decision defers the consumer's
static/runtime checks and clean-submodule evidence; these cannot be claimed
from the public fixture. The issue's closing comment records the published
Quard revision once confirmed reachable.
