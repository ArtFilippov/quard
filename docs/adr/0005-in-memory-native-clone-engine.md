# In-memory exact clone engine alongside the CLI

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

The wheel is built by developers/CI with a Rust toolchain and a locked Cargo
dependency graph; the consumer installs the binary wheel without Rust. The
Quard runner selects the wheel for the exact job with `QUARD_CLONE_ENGINE=rust`.
Until the other two in-memory jobs and their installation gates are verified,
the normal CLI remains the default and remains available for differential
comparisons. The renamed and gap-similar jobs remain independent CLI runs.

## Reporting and failures

The exact job retains the `jscpd` identity, a separate raw JSON report and
stdout/stderr logs, original paths, one-based normalized locations and both
native fragment locations. The JSON adds `engine` (the wheel and linked-crate
versions) and `sourcePaths` (the complete accepted source list). The worker
checks wheel metadata and linked-version evidence against `tools.json`, and
rejects missing or incomplete source preparation. The report parser checks
the complete path list, native source count, original locations and fragment
text. Mismatches, tool failures and invalid output are infrastructure errors
(exit 2); other jobs finish and retain their own diagnostics. Native findings
are never suppressed to obtain a passing result.

## Consequences

The native API is pinned deliberately: updating the CLI or any engine crate
requires rebuilding the wheel and repeating real CLI/library parity tests.
The first slice uses the original source with physical-line accounting; source
preparation and eligible-line accounting belong to later slices of #112.
An explicit wheel-selection switch keeps the existing CLI executable and its
independent native diagnostics in ordinary installations while the rest of the
three-pass installation contract is built.
