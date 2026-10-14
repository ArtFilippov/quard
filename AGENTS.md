## Changes

Keep changes focused and minimal.
Leave puzzles for later, following Puzzle Driven Development.
Flag smells and refactoring, suggesting issues rather than fixing silently.
Fix style violations in code, trusting checkers rather than suppressing them.

## Code

Favor aesthetics over functionality.
Prepend classes with plain-text docblocks explaining purpose, not usage.
Name variables as single nouns.
Inline single-use expressions, dropping unnecessary local variables.
Name methods as single nouns or verbs, separating commands from queries.

## Objects

Honor Elegant Objects and SOLID.
Follow DDD (domain-driven design) and YAGNI (you arent gonna need it).
Apply SRP (single-responsibility) and DRY (dont repeat yourself).
Favor failing immediately over failing silently.
Allow only assignments in constructor.
Compose objects instead of inheriting implementation.
Reject -er names and utility classes.

## Methods

Drop static methods, public static literals, setters, and getters.
Declare methods in interfaces first, exposing nothing public outside them.
Reject null returns and arguments.
Skip type introspection, casting, and reflection.

## Tests

Follow "Angry Tests".
Place one assertion per test, as its last statement.
Phrase each failure message negatively.
Keep tests short, verifying one behavior with one statement.

## Fixtures

Provide custom arguments to tested objects, not defaults.
Skip irrelevant code, side effects, accessors, constructors, and errors.
Favor fakes and stubs over mocks.
Inline brief fixtures, generating bulky ones at runtime.
Create fixture objects against duplication, using temporary directories.
Keep filesystem, sockets, and memory unmocked.

## Other

Read `README.md`, `PROFILE.md` and `COMPATIBILITY.md` before changing analyzer policy
or version-specific patches. Preserve diagnostics from enabled checks: do not filter
failures, loosen limits, add baselines, or run `tach sync` routinely to obtain a green
result. A policy decision made while developing Quard may disable a native rule
when it does not fit Quard's current model. Make that change in the analyzer
configuration and report the resulting check coverage accurately.
Before migrating slow integration tests to focused/unit tests, read
`TEST_MIGRATION_RUNBOOK.md`.

Consumer commands (from its root; add `--extra qwen` when that environment uses it):

```bash
git submodule update --init --recursive
uv sync --locked --group dev
uv run --locked --group dev python quard/check.py
uv run --locked --group dev python quard/apply.py --json
uv run --locked --group dev python quard/init_architecture.py --accept --json  # reviewed configurations only
```

Fresh installs select generated contract-only architecture: commit
`.quard/architecture.generated.json` and the installer-added `/tach.toml` ignore
rule. Install/apply/check regenerate root `tach.toml` from source without syncing
or accepting imports. Existing reviewed configurations keep explicit acceptance;
use `init_architecture.py` for that flow. Edit readability/scope in `.quard/`;
other native configs remain consumer-owned. Every
entry point accepts `--project`, defaulting to the current working directory.
Keep Quard code, pins, defaults and schema independent from the consumer root.

Reports: `.quard/reports/latest.json` and archived raw logs alongside each report.
Exit 0 means all checks passed, 1 means violations, 2 means config/tool failure.
Inspect `tools[*].status` and `is_infrastructure_error`; report actual exit codes
and unresolved diagnostics. Do not describe tool failures as a passing check.
Install failure details: `.quard/logs/install-error.json` and `install.log`.

During development, prefer incremental `pytest-testmon` runs for Quard tests
using the command in `README.md` (Development and integration). Seed its database
with a full pytest run first; finish tool changes with the full checks below.

After tool changes, run from the Quard checkout:

```bash
uv sync --locked --group dev
uv run --locked --group dev python -B -m unittest discover -s tests -v
uv run --locked --group dev python check.py
```

This includes native regression and real installation/smoke tests. Test fixtures
must not require access to a private consumer. Run the affected consumer's full
static and relevant runtime checks too. Keep ordinary install/check/apply/accept
writes inside the selected project; verify consumer submodules remain clean.
The Quard checkout is also a configured project: its full check covers `src/quard/`
and `tests/`, with reports in this checkout's `.quard/`. Its root Tach configuration
uses the generated policy; run `check.py` to restore an absent `tach.toml`.

Develop directly in a consumer submodule or separate clone. Reproduce, fix, add
an autonomous regression, and test. Keep the tool and consumer histories separate;
pin a reachable Quard commit when updating the consumer gitlink.

## Agent skills

### Issue tracker

Issues and specs live in this repo’s GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five default triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: root `CONTEXT.md` and `docs/adr/`. See `docs/agents/domain.md`.

## English

After every user command written in English, check its grammar before doing anything else.

- If there are grammatical errors, start your response with: `Better say: <corrected version>`
- Preserve the user's original meaning and phrasing as much as possible.
- If the grammar is already correct, start your response with: `Great grammar`
- If there is reasonable doubt about the intended meaning and correcting the grammar would require guessing the user's intent, ask a clarifying question instead of assuming the meaning.
- After the grammar feedback or clarification, proceed with the user's request normally.

Keep your own responses extremely concise, simple, and easy to understand.
