---
name: clean-tests
description: Use when asked to speed up tests, replace repeated integration checks with focused tests, or shorten the local test feedback loop.
---

# Clean tests

Measure first, preserve the signal, and ask the human to choose the trade-off.
Use the target repository's test commands, documentation, and coverage expectations;
adapt the tools to its language and framework. For Python/pytest tools and their
limits, read [PYTHON-PYTEST.md](PYTHON-PYTEST.md) when working in such a project.

## 1. Establish the baseline

Read the project's testing instructions, existing test and fixture code, and
required full verification commands. Identify the test runner(s), discovery
rules, static checks, installed profiling tools, and any clean-tree constraints.
Run the relevant existing suite before changing tests. Record its real outcome:
wall time, counts of passes/failures/skips, and failures unrelated to this work.

Profile the exact files or directory under consideration with the same scope you
will re-profile later. Capture structured per-test timings when possible; separate
test call, setup, and teardown from elapsed suite time. For repeated subprocesses,
installations, or analyzer reports, measure those operations or their individual
jobs too. Concurrent job durations cannot be added to infer wall time. Keep
profiling artifacts outside tracked fixtures. Identify the tests and repeated
operations responsible for most elapsed time; one run is a measurement, not a
stable benchmark.

**Done when:** the candidate groups have measured costs, their slow work is
identified, and the baseline outcome and scope are known.

## 2. Inventory coverage and ask

Read each candidate's assertions and the code it exercises. Distinguish rule
behavior, tool semantics, report/orchestration behavior, runtime behavior, and
installation behavior. Include allowed and forbidden cases, discovered source
scope, diagnostic identity and position, negative cases, tool status, exit code,
and archived output where applicable. Count expensive operations *inside* a test,
not just the number of test functions. Find existing focused tests before proposing
new ones.

Rank candidate test suites by measured elapsed time at a consistent granularity
(for example, test files or directories). Present only the slowest suites, at
most 30 per round, in one compact table. Address remaining suites in later
rounds after the human chooses what to do with the current batch:

| Suite / tests | Purpose and evidence | Measured duration and expensive operations | How they run now |
| --- | --- | --- | --- |

Below the table, give numbered items keyed to its suites, each with the options,
their coverage trade-offs, and a recommendation. Offer concrete alternatives
grounded in the measurements: retain an essential real integration, exercise
behavior at a narrower real interface, test report assembly with controlled
outputs, select only relevant real jobs, reduce fixture
setup, batch independent cases, or adopt incremental local runs. Explain what
each option would and would not prove. Propose fewer repeated integrations by
adding focused/unit coverage where it preserves the behavior; impose no numeric
integration-test quota. If proposing a permanent skip, state exactly which
coverage disappears and request a separate explicit decision.

**Wait for the human's choice before changing tests.** Discuss similar tests as
a group, but obtain a separate decision for any skipped, removed, or replaced
real integration boundary. Align planned changes with the project's current
documentation and update applicable docs in their existing locations; do not
mandate a new decision log.

**Done when:** the human has chosen a treatment for each proposed group and each
change to real integration coverage.

## 3. Make the approved changes

Map every distinct old assertion to its surviving test before removing its
original check. Prefer the narrowest **real** interface that proves the claim:

| Claim | Appropriate test |
| --- | --- |
| A rule accepts or rejects a source case | Invoke the actual rule over the project's discovered production/test scope (including stubs if supported); assert findings, positions, absence of execution errors, and positive cases. |
| Generated configuration has particular roles, dependencies, or exports | Inspect generated configuration directly; separately retain a real native-tool case if interpretation by that tool matters. |
| A native tool or runtime actually behaves a certain way | Exercise the real pinned worker, adapter, or runtime at its owning boundary; assert its actual output, including native-only behavior. |
| Results are combined, archived, or reported independently | Use the real report/orchestration code with deterministic fake *job outputs*; assert invoked jobs, statuses, exit codes, paths, saved streams, and error propagation. This proves orchestration, not native semantics. |
| A report delivers real native findings | Keep a small real report scenario, executing only jobs whose findings it checks; select jobs before any expensive preparatory work, and assert exact execution, findings, status, and raw output. A selected report is not a full-suite result. |
| An installer or command changes state | Run the real state transition; replace unrelated network, package-manager, or plugin probes with controlled dependencies, retaining real lifecycle coverage where required. |

Keep necessary integrations at the boundaries they own. Replacing a semantic
analyzer with a fake cannot verify that analyzer; generated policy alone cannot
prove the native tool's interpretation. In fake-output report tests, also
isolate any real preparatory work that runs before output capture; faking a job
callback alone may leave expensive batches running. Group independent fixture
mutations only when each case keeps its own assertions and edits are reliably
restored. Batching repeated full checks can help temporarily, but does not
replace splitting behavior from reporting. Sharing a process between compatible
cases can remove startup cost; isolate cases whose tool configurations must not
leak into one another. Moving the same repeated external work to another test
file does not save time.

Preserve the ordinary production checks and their diagnostic limits. Treat
violations, tool/configuration failures, and disabled or skipped checks as
distinct outcomes. Follow the project's existing conventions for real test
fixtures and required full static/runtime checks.

**Done when:** every old behavior claim has a specific surviving assertion, and
real-tool claims still have real-tool coverage.

## 4. Consider an incremental feedback loop

If the project would benefit, investigate a change-aware test selector compatible
with its runner and discovery model. Pin/configure it according to project
conventions and keep its database and cache out of tracked sources and
clean-tree-sensitive fixtures. Seed it with a full run, verify an unchanged run,
then make and restore a controlled change to confirm that relevant tests are
selected. Check how it handles previous failures and changes outside executed
code, such as configuration or external services. Incremental selection speeds
up development; it does not establish full-suite coverage.

**Done when:** any adopted selector demonstrably selects affected tests and the
project still has a documented full verification command.

## 5. Re-profile and verify

Repeat the original profiling command and scope. Compare wall time, test-call
time, setup/teardown, slow operations, passing/failing/skipped counts, and the
assertions covered before and after. Account for runner discovery differences;
do not equate counts from different runners. Run the affected tests, then the
project's required full test, static, and relevant runtime checks without
incremental selection. Inspect job statuses, real exit codes, infrastructure
errors, and archived diagnostics where available. Report unresolved findings
and coverage gaps plainly, including any explicitly approved skips. Check that
the project's existing test documentation matches the new workflow.

**Done when:** every migrated claim is covered, the before/after measurements
are comparable, and full verification outcomes (including failures) are reported.
