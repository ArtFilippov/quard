# Compare AST-owned units through the pinned native clone engine

## Decision

Quard parses original Python source once in the pinned Rust wheel, builds
independent AST-owned detection units for exact, renamed and similar passes,
then feeds those units to the linked native jscpd detector. We keep its native
findings and map each occurrence back to the original source; we do not discard
findings after detection. Complete executable constructs, rather than arbitrary
physical line windows, set the behavioral minimum. Exact-only schema and
contract declaration units are separate. The three jobs still share discovered
`.py` scope and the configured `min_units`, but use different eligible views.

## Trade-off and consequences

Unprepared CLI token windows matched different NamedTuple fields, two shifted
windows of one literal expression, and partial call tails padded with closing
parentheses (#136–#138). Raising a universal line threshold would lose genuine
three-operation clones; post-filtering would conceal enabled native findings.
AST ownership and in-memory preparation instead prevent those false candidates
before detection and retain legitimate exact, role-aware renamed and gap-similar
matches. The cost is a pinned RustPython/jscpd integration, additional source
mapping and wheel-build requirements; changing either parser or native crates
requires real-engine regressions, original-fragment validation and same-scope
performance measurement. The historical CLI remains a comparison baseline but
its physical-line minimum does not mean the same thing as Rust's eligible units.
See [ADR 0005](0005-in-memory-native-clone-engine.md) for installation and native
report integration and [#148 verification](../issue-148-clone-verification.md).
