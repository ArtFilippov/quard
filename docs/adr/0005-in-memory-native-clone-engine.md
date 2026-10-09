# In-memory native clone engine alongside the CLI

## Decision

The first-party `quard-clones` Python wheel links the pinned jscpd Rust engine
crates (`cpd-core` 0.1.20, `cpd-finder` 0.1.20, `cpd-tokenizer` 0.1.19 and
`cpd-reporter` 0.1.21). Its `SourceSession` accepts the discovered, original
`.py` sources as in-memory `(absolute_path, text)` pairs. The pinned native
`FilePreparer` tokenizes each file in strict mode with one minimum token and the
effective `[duplication] min_lines`. The session retains prepared detection
sources and original text. Detection uses `detect_prepared`; native statistics
use `cpd-finder::statistics::compute`. The exact job keeps only the engine's
exact kind, as the CLI's `--kind exact` does. The native report uses the
engine's original locations and counts and `cpd-reporter`'s original-text line
extraction without reading source a second time in the reporter. No consumer
module is imported and no prepared `.py` file is written.

The wheel is built by developers/CI with Rust 1.99.0 and a locked Cargo
dependency graph; the consumer installs the binary wheel without Rust. CI
requires the real wheel build, installs that artifact into an isolated uv
consumer, and runs its full clean and clone checks without a Rust toolchain in
the consumer's `PATH`. The Quard runner selects the wheel for all three jobs
with `QUARD_CLONE_ENGINE=rust`. #114 calls for a full check that *can* use the
wheel, not for every consumer to install it by default. Normal uv pinning and
publication across supported Linux targets belong to #125. Until the #125
installation gates are verified, the normal CLI remains the default and remains
available for differential comparisons.
#115 adds the renamed pass to `SourceSession`: it loads the same original source
manifest and uses the same strict native `FilePreparer` pipeline with
`ignore_identifiers` and `ignore_literals` enabled. Before #117 the jobs ran in
separate processes, each with its own session and native report.
The renamed job keeps only native Type 2 (`renamed`) matches; normalized Type 1
matches do not displace findings from the separate unnormalized exact job.
Before preparation all jobs used the same positive physical-line minimum and
discovered Python scope.
#116 adds the independent gap-similar job. It calls the pinned native
`merge_gapped_clones` after `detect_prepared`, with a two-unmatched-line limit
in each file; each constituent must first meet the common detection minimum.
It keeps only native Type 3 with the `gap` method, never AST similarity.

#117 adds RustPython parsing before native tokenization. The session parses
each discovered `.py` once and creates separate module/class/function detection
units. File-level and class-level imports, actual docstrings and attribute
documentation are omitted from the units; local imports and assigned values
remain. Units compact eligible lines for the native minimum without changing
the original text. A line map restores file paths, line numbers and byte
offsets on findings; archived fragments always come from original text. The
batch worker prepares one session for all three passes, writes each pass's
report separately and returns independent statuses. Its own stdout/stderr are
archived and linked from all three jobs alongside their individual streams.
Normalized candidate
units sharing native token windows are scanned as pairs: the pinned detector
retains only two secondary one-token occurrences, which can otherwise lose
Type 2 clones in a large normalized scope. These are native scans, not filters
on reported findings.

#118 excludes ordinary class and function headers (including decorator syntax,
multiline parameters and annotations), annotation-only fields and recognized
type aliases from the prepared units. PEP 695 `type` statements and assignments
annotated with explicitly imported `typing.TypeAlias` or
`typing_extensions.TypeAlias` (including aliases) count as type aliases;
ordinary assignments, including annotated assignments with values, do not.
Control flow, function-local imports, decorator implementations and real bodies
in contracts or special methods remain behavioral. All three jobs consume the
same view at this stage. Dedicated schema/contract declaration views remain
dependent work; omission here does not reclassify them as behavioral clones.

## Reporting and failures

The three jobs retain the `jscpd`, `jscpd-renamed` and `jscpd-similar` identities,
separate raw JSON reports and stdout/stderr logs, original paths, one-based
normalized locations and both
native fragment locations. The JSON adds `engine` (the wheel and linked-crate
versions compiled from the actual Cargo.lock) and `sourcePaths` (the complete
accepted source list). Prepared reports also contain per-unit eligible-line
lengths, total eligible lines, configured minimum and original-source digests.
An entirely ineligible view is skipped with a reason. The worker
checks wheel metadata and linked-version evidence against `tools.json`, and
rejects missing or incomplete source preparation. The report parser checks
the complete path list, native source count, original locations and fragment
text. The parser validates each job's expected native kind, counts and mappings;
similar also verifies its score, method and unmatched-line accounting.
Mismatches, parser failures, tool failures and invalid output are infrastructure errors
(exit 2); other jobs finish and retain their own diagnostics. Native findings
are never suppressed to obtain a passing result.

## Consequences

The native API is pinned deliberately: updating the CLI or any engine crate
requires rebuilding the wheel, rerunning unprepared CLI/library parity tests
and verifying the prepared-view fixtures with the real component.
The ordinary CLI still uses original-source physical lines. The opt-in Rust
path applies the shared minimum to eligible prepared lines. Declaration-only
material for exact contract/schema clones remains for later slices of #112.
An explicit wheel-selection switch keeps the existing CLI executable and its
independent native diagnostics in ordinary installations while the rest of the
three-pass installation contract is built.
