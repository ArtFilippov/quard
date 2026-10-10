# In-memory native clone engine from pinned source

The #125 installation decision uses a private Git source instead of published
binary artifacts. The consumer's uv source pin names the selected Quard Git
revision and `rust/quard-clones` subdirectory. uv builds with pinned maturin
and Cargo.lock using the consumer's Rust/C toolchain, outside the Quard checkout.
Installation selects the Rust path after pinning the source and checks the
built distribution's WHEEL tags, ABI and linked crate versions. Exact, renamed,
gap-similar and clean packaged probes run during installation. The revision
must be fetchable from the tool's origin; a local test remote does not establish
reachability for another consumer. Linux x86-64 glibc on CPython 3.12/3.13
has been exercised; ARM64, musl and 3.14 remain unverified.

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

The consumer builds the versioned wheel from its locked Git source with a Rust
toolchain and a C compiler/linker; maturin and the Cargo dependency graph are
pinned. The installer verifies the staged gitlink for in-project submodules;
the isolated public fixture verifies install, reinstall, locked sync and full
check from a separate clone and a nested nonstandard submodule without dirtying
either tool tree. The runner selects the built component for all three jobs
when the consumer has the uv source pin. Updated installations without that
pin fail with exit 2 and a migration link. Older gitlinks retain their own
historical behavior; the CLI in Quard's dev group supports the benchmark.
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

#119 excludes ordinary standalone `pass`, ellipsis statements and bare or
literal-argument `raise NotImplementedError` from the shared prepared behavior
view. Stub-only bodies (with or without documentation) have no eligible lines;
two real lines plus a placeholder remain below the default minimum. Other
executable statements, including computed exception arguments and `return None`,
still count. The source map retains original lines and archives verbatim fragments
when a real match spans a placeholder. The exact contract-declaration units
added in #123/#124 count eligible declaration placeholders as physical lines
without altering the behavior view.

#120 keeps the gap pass on that same prepared session: native detection first
requires each constituent to meet the configured eligible-line minimum, then
merges at most two unmatched prepared lines in each unit. Unit-specific native
source IDs prevent adjacent, nested and same-file functions or classes from
merging. Comments, blanks and excluded scaffolding inside a unit contribute
neither to the minimum nor to the gap allowance. Native `unmatchedLines` is
measured in prepared lines; report `lines`, positions and verbatim fragments
describe the original source span, including any omitted interior material.
Strict native Python whitespace tokens are anchored to their preceding eligible
line before detection: matching indentation on the next, otherwise different
line cannot turn two executable lines into a three-line clone.

#121 adds exact-only units for uninterrupted class-based TypedDict field runs
when the base resolves to an explicitly imported `typing` or
`typing_extensions.TypedDict` (including aliases). The class header and name are
not eligible. Each schema/run stays independent, and the same minimum counts
original eligible field lines; a different first field need not prevent an
identical trailing run. Exact uses native strict tokens, while renamed and similar
still consume only the behavior view. Both views retain independent per-unit
accounting and explicit empty-view skips; original mappings and native diagnostics
apply to schema fragments too. #122 adds functional calls proven from the same
imports: literal dictionary keys or keyword fields each on a separate original
line, with a single-line type expression and no surrounding call/dictionary
syntax on a field line. Compact or mixed-line calls are outside the supported
schema view; their declaration material also stays out of behavior views.
In-memory canonical field tokens let class and functional schemas match exactly
without treating the class/factory name, quotes or commas as fields. Per-byte
column maps restore the real field positions; archived fragments are extracted
from original files, including their original syntax. Each schema remains its
own unit and uses the same eligible-line minimum; mapping/parser errors fail
as infrastructure errors rather than silently returning an empty scan.

#123 adds exact-only ordered method declarations from local Protocols and
abstract interfaces. Each one-line signature retains its exact method name,
parameters and return type; a standalone one-line declaration placeholder is
tied to that signature so repeated `pass` or ellipsis cannot match differently
named methods. The class name, decorators, documentation and special dunder
and property-accessor declarations are not eligible at this stage. The same
eligible physical-line minimum applies to signatures and placeholders; real
bodies, including special-method bodies,
remain in the behavior view. Native fragments map back to original signature
and placeholder lines; renamed/similar never receive declaration units.

#124 includes getter, setter and deleter declarations in that exact-only unit.
Each prepared line is tagged with the whole exact signature, its row and, for
properties, the public property name and accessor kind. Thus an ordinary
method, a changed signature or another accessor cannot match a continuation
or placeholder as the same declaration. Multiline signatures retain one prepared
line per original signature line, followed by a signature-tied placeholder when
the body is separate. Per-byte maps restore original columns and complete
source spans; archives and normalized details contain only verbatim original
multiline fragments, never canonical declaration text. Only eligible
signature/placeholder lines clear the shared minimum, irrespective of excluded
decorators, documentation or the length of a reported original span. Special
declarations stay excluded; real contract, property and special-method bodies
stay behavioral in all three passes.

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
The historical CLI uses original-source physical lines; the source-built Rust
path applies the shared minimum to eligible prepared lines. The CLI benchmark
remains available in Quard's dev environment. Consumer installation no longer
adds or version-probes the CLI. Check fails closed with a migration link for an
updated consumer with no source pin; all three jobs are explicit errors rather
than skipped. The tool checkout retains its pinned dev CLI for its full check.
