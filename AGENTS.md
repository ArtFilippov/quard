# Eryx agent instructions

Read `README.md`, `PROFILE.md` and `COMPATIBILITY.md` before changing analyzer policy
or version-specific patches. Preserve native diagnostics: do not filter failures,
loosen limits, add baselines, or run `tach sync` routinely to obtain a green result.

Consumer commands (from its root; add `--extra qwen` when that environment uses it):

```bash
git submodule update --init --recursive
uv sync --locked --group dev
uv run --locked --group dev python eryx/check.py
uv run --locked --group dev python eryx/apply.py --json
uv run --locked --group dev python eryx/init_architecture.py --accept --json
```

Architecture acceptance follows explicit review of `tach.toml`; first generate a
draft with `init_architecture.py --json` only for an uninitialized project.
Edit readability/scope in `.eryx/`; native configs remain consumer-owned. Every
entry point accepts `--project`, defaulting to the current working directory.
Keep Eryx code, pins, defaults and schema independent from the consumer root.

Reports: `.eryx/reports/latest.json` and archived raw logs alongside each report.
Exit 0 means all checks passed, 1 means violations, 2 means config/tool failure.
Inspect `tools[*].status` and `is_infrastructure_error`; report actual exit codes
and unresolved diagnostics. Do not describe tool failures as a passing check.
Install failure details: `.eryx/logs/install-error.json` and `install.log`.

After tool changes, run from the Eryx checkout:

```bash
uv sync --locked --group dev
uv run --locked --group dev python -B -m unittest discover -s tests -v
```

This includes native regression and real installation/smoke tests. Test fixtures
must not require access to a private consumer. Run the affected consumer's full
static and relevant runtime checks too. Keep ordinary install/check/apply/accept
writes inside the consumer; verify the submodule remains clean.

Develop directly in a consumer submodule or separate clone. Reproduce, fix, add
an autonomous regression, and test. Keep the tool and consumer histories separate;
pin a reachable Eryx commit when updating the consumer gitlink.

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
