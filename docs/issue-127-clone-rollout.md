# #127: historical wheel rollout integration record

This records the earlier unpublished-wheel proposal. Issue #125 supersedes its
publication gate with a private, pinned Git source build. See the current
installation and supported-target contract in [README.md](../README.md#install).

## Current gate

#126's [same-scope benchmark](issue-126-clone-benchmark.md) measured seven
end-to-end runs at `min_lines = 3` (Rust/CLI median ratio 1.877×, under the 4×
ceiling). #125 is still open: four supported Linux `cp312-abi3` wheels have not
been verified as published, reachable index artifacts. The local wheel fixture
simulates `native_wheel` activation only in its copied tool tree. Ordinary
installations still select the pinned jscpd 5.4.0 CLI; removing its dependency
or version probe and changing the default engine must wait for the release gate.
The benchmark script retains the historical three-CLI-pass comparison.

## Public, real-engine coverage against #112 and #91

These fixtures build and execute the actual `quard-clones` wheel; focused
check-runner fixtures may simulate unrelated analyzer results, whereas
`verify_real_full_check` runs every analyzer in an isolated public consumer.

| Feature / evidence | Autonomous regression |
|---|---|
| #91 module imports, class/method documentation and ordinary class openings do not qualify; local imports and three real body lines do | `test_real_wheel_prepared_imports_and_docstrings`, `test_real_wheel_same_file_class_docstrings_from_91` |
| Ordinary signatures, decorators, annotations, aliases, value assignments, control flow, executable contract/special bodies and placeholders | `test_real_wheel_behavioral_declarations`, `test_real_wheel_behavioral_placeholders` |
| Exact schema fields across class/functional TypedDict forms, differing fields, same-file and original columns | `test_real_wheel_behavioral_declarations`, `test_real_wheel_functional_typed_dict_fields` |
| Ordered Protocol/abstract declarations, accessor names/kinds, multiline signatures and original ranges | `test_real_wheel_contract_methods` |
| Exact, renamed and two-line gap-similar jobs, cross-profile and same-file clones, scope boundaries, minimum and explicit empty-view skips | `test_real_wheel_renamed_prepared`, `test_real_wheel_similar_prepared`, `test_real_wheel_prepared_scope`, `test_real_wheel_same_file_and_mapping` |
| Verbatim original fragments, complete source/digest accounting, invalid syntax, mapping/tool failures and retention of peer findings | `test_real_wheel_prepared_imports_and_docstrings`, `test_real_wheel_invalid_output_retains_independent_diagnostics`, `test_real_wheel_renamed_failure_keeps_exact`, `test_real_wheel_similar_failure_keeps_peers` |
| ABI and linked-version validation, clean/exact/renamed/gap packaged smoke, binary-only uv install/reinstall with explicit min_lines, separate and nested tool trees | `test_real_wheel_rejects_incompatible_abi_tag`, `test_real_wheel_full_consumer_without_rust`, `test_real_wheel_uv_install_separate_tool_without_rust` |

The real-wheel separate-tree fixture verifies install, apply and check leave
both external tool copies byte-for-byte unchanged, including no new files.
It runs from outside the selected consumer, with a nested tool location
containing spaces; reports, managed configs and native logs stay in the
selected public project. The locked suite also exercises rejected invalid
minimums, retained settings, and transaction rollback. These checks establish
local integration only; a local wheel and a public fixture cannot prove PyPI
availability, the four target-index installs, or the affected consumer's
static/runtime checks with the released integration.

## Required completion gates

1. Finish #125: publish and verify reachable wheels for x86-64 and aarch64 on
   glibc and musl, prove their supported Python/ABI installs via real indexes,
   and pin the reachable distribution/Quard commit before activating the
   default wheel path. Preserve the consumer's uv indexes and optional extras.
2. Run the affected consumer's full static and relevant runtime checks on the
   released integration; verify its submodules are clean and pin only a
   reachable Quard gitlink. Report native diagnostics, statuses and errors.
3. Recheck released reporter parity and packaged probes for all three jobs
   before considering removal of the unused CLI pin/probe. Keep the historical
   CLI benchmark reproducible.

Do not mark #127 complete on the strength of local-wheel tests alone.

## Local verification (2026-10-09)

`uv sync --locked --group dev` exited 0. The full locked unittest discovery
exited 0 (385 tests, one expected lifecycle skip); this includes an unmocked
public-consumer full static check and runtime exercises, packaged native probes,
and separate-tree install/reinstall/apply/check with the real local wheel.
The Quard checkout full static check exited **1**: report
`.quard/reports/20261009T074454Z-d3a77963/report.json` archives **4,977
unresolved native diagnostics**, **zero infrastructure errors**, and raw logs.
Per-job `status / exit_code`:

| Jobs | Status / exit code |
|---|---|
| class-contracts, dependency-types, instance-fields, jscpd, jscpd-renamed, lcom4, pydoclint, pyright, tach, tach-external, test-placement | violations / 1 |
| pylint-production, pylint-tests | violations / 28 |
| contract-folders, encapsulated-only, free-function-placement, jscpd-similar, method-decorators, outer-classes | passed / 0 |
| constructor-purity | skipped / null |

This checkout result runs the ordinary CLI path because publication has not
enabled the wheel by default. The report contains native findings, not a
passing static check; no threshold, diagnostic or architecture boundary was
weakened to alter its exit status. No private affected-consumer checks or
published-wheel target installs have been performed in this local phase.
