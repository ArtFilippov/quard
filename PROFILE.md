# Native analysis profile

Eryx 1.0.0 preserves the original strict profile. Exact tool pins live in
`tools.json`; top-level versions are verified on every check. Consumers control
their native configs; there is no hash-based anti-edit system or hidden baseline.

## Tools and scope

* **Pylint 4.0.8**: core and every discovered official extension except docparams.
  Readability, naming, exceptions, logging, inference, duplicates and import cycles.
* **clean-architecture-linter-plugin 1.2.2**: architecture, DI, Protocol, I/O and
  structural heuristics. This is an Alpha plugin, not a universal correctness proof.
  Narrow version-guarded compatibility patches are documented in `COMPATIBILITY.md`.
* **Pyright 1.1.414** with its Node runtime: strict plus the additional diagnostics
  in `_policy.PYRIGHT_EXTRA`. Cycles are assigned to Pylint.
* **Tach 0.35.1**: both internal and external checks, explicit module/public API
  boundaries, layers, cycles, exact dependencies (including unused declarations).
  The adapter uses pinned native APIs: this CLI version's JSON path would skip the
  extra exact/unused stage. Native detail is retained in reports.
* **pydoclint 0.9.1**: existing Google docstrings checked against signatures/bodies.

Production is `src/`; tests are `tests/` and the test directory basenames configured
in `.eryx/scope.toml`. Exclusions match **exact directory basenames**, not substrings:
`generated_models` is not `generated`. Discovered excluded directories appear in
reports. `.pyi` files are checked by Pyright; other tools use their native scope.
`__init__.py` is included. Root scripts, notebooks and Eryx itself are outside the
consumer's source scope. Eryx's own code is verified by its autonomous test suite.

## Readability

Edit `.eryx/readability.toml`, then run `eryx/apply.py` in the consumer environment.
Unapplied limits fail with `READABILITY_NOT_APPLIED`; invalid TOML, unknown names,
booleans/nonpositive limits and contradictory bounds are errors.

Default values below apply to **new** installations. Existing consumer values win.
Tests inherit unspecified production limits.

| Setting | Production | Tests |
|---|---:|---:|
| max_line_length | 100 | 100 |
| max_module_lines | 400 | 800 |
| max_statements | 30 | 60 |
| max_complexity | 8 | 10 |
| max_branches | 8 | 10 |
| max_nested_blocks | 4 | 4 |
| max_locals | 10 | 20 |
| max_returns | 4 | 6 |
| max_bool_expr | 4 | 4 |
| max_args | 5 | 10 |
| max_positional_arguments | 4 | 10 |
| max_attributes | 7 | 7 |
| max_public_methods | 12 | 30 |
| max_parents | 3 | 3 |
| min_public_methods | 2 | 0 |
| min_similarity_lines | 4 | 4 (duplicate-code disabled) |
| max_try_statements | 1 | 3 |

Metrics come from Pylint/McCabe. `max_statements` is not physical function length.
`max_parents` counts ancestors, not just immediate bases. Generic exception,
Protocol and Enum bases are excluded; additional framework policies belong to
the consumer's `[tool.pylint.design].ignored-parents`. Pylint also excludes those
bases' ancestors. Project-defined ancestors still count. Similarity checking
ignores comments, docstrings, imports and signatures.

## Documentation and test policy

Docstrings are optional. Existing ones (including short/private functions) must
follow the Google contract. Empty/malformed docs are not missing docs. Argument
types are expected in annotations and Google Args. Upstream check-arg-defaults
only adds NumPy checks; it does not prove Google default-value consistency.

`pylint.extensions.docparams` collides with the architecture plugin's W900x/W901x
IDs and is not loaded. Pylint docstyle and pydoclint cover documentation without
claiming identical rule sets. The discovered catalog and policy notes are saved
in `.eryx/pylint-catalog.json` and `.eryx/policy-notes.json`.

Production disables mandatory docstrings and four checks explicitly delegated to
Pyright: reimported, undefined-variable, used-before-assignment and
possibly-used-before-assignment. Unused/private/unreachable diagnostics and
architecture W9001/W9003 remain: partial overlap is not identical coverage.

Tests retain strict typing, execution/syntax/exception checks and validation of
existing docs. They allow fixture/reused names and unused fixture arguments,
protected access, one-operation containers, literal expected values and repeated
arrange/assert blocks. Production-only architecture rules are disabled for tests;
W9101 (mock-count heuristic) and W9501 (anti-bypass) remain. W9102 is disabled to
allow white-box tests. Test Pyright environments allow private access and ignored
call results. Exact disable lists and rationales are in generated policy notes.

## Architecture

Default direction matrix, always requiring explicit `depends_on`:

| From | Allowed target layers |
|---|---|
| Bootstrap | All layers |
| Interface | Interface, UseCase, Domain |
| Infrastructure | Infrastructure, Domain |
| UseCase | UseCase, Domain |
| Domain | Domain |

Drafts add `cannot_depend_on` to enforce directions beyond Tach's linear ordering.
Recognized folders: domain; application/use_cases/usecases/usecase;
infrastructure/adapters; interfaces/interface; bootstrap/composition.
Unknown folders start as provisional Domain. Review them explicitly. Default
package exports are `api.py` and its public symbols, not all implementation.
Tach's public import surface and typing.Protocol are distinct contracts.

## Limitations

No source rewriting, formatting, security scanning, runtime tests, community
discovery, physical function-line metric, spellchecker or universal Protocol-only
proof is part of `check.py`. Suppressions are governed by native analyzers,
including unused-suppression diagnostics and required Tach reasons. Exclusions
must not become hiding places for ordinary source files.

References: [Pylint](https://pylint.readthedocs.io/en/stable/user_guide/configuration/all-options.html),
[Pyright](https://github.com/microsoft/pyright/blob/main/docs/configuration.md),
[Tach](https://docs.gauge.sh/usage/configuration/),
[pydoclint](https://jsh9.github.io/pydoclint/config_options.html),
[uv groups](https://docs.astral.sh/uv/concepts/projects/dependencies/).
