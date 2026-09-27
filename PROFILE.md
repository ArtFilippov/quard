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
`__init__.py` is included. Root scripts are outside the Python source scope.
Notebooks are discovered project-wide (excluding configured directory names,
tooling, environments and caches); the production Pylint worker checks them once
with `nbstripout --verify` without rewriting them. Coverage lists notebook paths
and count. Eryx's own code is verified by its autonomous test suite.
When `.eryx/checks.toml` is present, notebook discovery is limited to its
`working_directories`. `anonymity` scans selected files with pinned `detect-secrets`
and reports potential-secret types and locations without values. It requires no
secrets baseline or verification service. `anonymity` alone runs a minimal Pylint worker
without the native code-style or architecture analyzers. Secret coverage records
confirmed text-file scans separately from unsupported binary files and selected
inputs. Unreadable text, partial scans and zero text-file coverage are errors.
The installer verifies an intentional secret in an isolated fixture. An independent
Presidio job checks both languages in the pinned analyzer 2.2.360 with pinned local
`en_core_web_sm` and `ru_core_news_sm` 3.8.0 models and no downloads during a check.
The expected active inventory is checked on both installation and each scan: exactly
16 recognizers and the following 22 entity types (see `_presidio_policy.py` for the
exact per-recognizer mapping, including the spaCy NER mapping):

| Recognizers | Standard English entities |
|---|---|
| spaCy NER | AGE, DATE_TIME, EMAIL, ID, LOCATION, NRP, ORGANIZATION, PERSON, PHONE_NUMBER |
| Date, Email, Phone, URL, IP | DATE_TIME, EMAIL_ADDRESS, PHONE_NUMBER, URL, IP_ADDRESS |
| Credit card, crypto, IBAN, medical license | CREDIT_CARD, CRYPTO, IBAN_CODE, MEDICAL_LICENSE |
| UK NHS | UK_NHS |
| US bank, driver license, ITIN, passport, SSN | US_BANK_NUMBER, US_DRIVER_LICENSE, US_ITIN, US_PASSPORT, US_SSN |

All applicable recognizers run for each selected file, with no Eryx entity allowlist,
minimum score cutoff, baseline or value suppression. This inventory includes the
installed standard medical *license* identifier; optional clinical/medical NLP
models are not installed. A missing or unexpected recognizer, NER mapping or nonzero
threshold fails the scan (exit 2), rather than silently changing coverage. The job
confirms selected text and skipped binary coverage, rejects unconfirmed inputs, and
records only entity types and physical locations (and scores). The installer probes
name, contact, date, URL, network and country-specific identifiers as well as clean
input. `codestyle` retains
the `src/` layout and architecture requirements.

The Russian inventory contains the eight shared Presidio pattern recognizers
(CRYPTO, DATE_TIME, EMAIL_ADDRESS, IBAN_CODE, IP_ADDRESS, MEDICAL_LICENSE,
PHONE_NUMBER, URL), the seven English country-specific/credit-card recognizers
also applicable to identifiers in Russian prose (CREDIT_CARD, UK_NHS,
US_BANK_NUMBER, US_DRIVER_LICENSE, US_ITIN, US_PASSPORT, US_SSN), Russian spaCy
NER (PERSON, LOCATION, ORGANIZATION), a Russian month-name date pattern and
three Russian domestic identifier patterns: RU_INN (10/12 digits), RU_SNILS
(3-3-3-2 digits), RU_PASSPORT (4 + 6 digits).
All 20 Russian recognizers and their 21 entity types are verified at install and
check time alongside the 16 English recognizers. The Russian model's only NER
labels are PER, LOC and ORG; dates, nationality, age, ID, email and phone are not
claimed as Russian NER categories. Dates and contact details instead use pattern
recognizers. Identifier patterns indicate *potential* identifiers, not checksum
validation. Findings from both passes are deduplicated by span and entity type;
physical columns and span lengths keep distinct observations on the same line.
Each file is analyzed whole (no chunk-boundary omissions) with a 512 KiB raw-byte
limit per file. Larger/changed/unreadable inputs remain unconfirmed and force exit 2;
binary inputs are reported separately. JSON and notebook text use physical file
lines. Archived Presidio worker JSON is replaced with validated status and coverage
so malformed output cannot expose source text.

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

No source rewriting, formatting, dependency vulnerability scanning, runtime tests, community
discovery, physical function-line metric, spellchecker or universal Protocol-only
proof is part of `check.py`. Suppressions are governed by native analyzers,
including unused-suppression diagnostics and required Tach reasons. Exclusions
must not become hiding places for ordinary source files.

References: [Pylint](https://pylint.readthedocs.io/en/stable/user_guide/configuration/all-options.html),
[Pyright](https://github.com/microsoft/pyright/blob/main/docs/configuration.md),
[Tach](https://docs.gauge.sh/usage/configuration/),
[pydoclint](https://jsh9.github.io/pydoclint/config_options.html),
[uv groups](https://docs.astral.sh/uv/concepts/projects/dependencies/).
