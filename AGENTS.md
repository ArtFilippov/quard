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

Develop directly in a consumer submodule or separate clone: branch first if HEAD
is detached, reproduce, fix, add an autonomous regression, test, commit/push and
open a PR **in Eryx** through `gh`. After CI/review/merge, pin the merged SHA in a
**separate consumer PR**, with integration updates and a link to the tool PR.
Never combine the two repository histories or publish an unreachable gitlink.
