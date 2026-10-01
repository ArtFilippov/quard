# Eryx

Pinned native Python analysis for Linux `uv` projects with a `src/` layout.
Named after *Eryx jayakari*. Tools run in the **consumer's environment**;
Eryx is versioned independently and normally attached as a Git submodule.

## Install

From the consumer root (Python >= 3.12 and `uv` required):

```bash
git submodule add git@github.com:ArtFilippov/eryx.git eryx
bash eryx/install.sh --project .
```

The installer pins dev dependencies, updates the consumer lockfile, synchronizes
its environment, initializes `.eryx/` and native configs, then runs real analyzer
smoke tests. It does not copy or modify Eryx sources. Inspect a plan with:

```bash
bash eryx/install.sh --project . --dry-run --json
```

Reinstall with the same command after updating the gitlink. Project limits, scope,
reviewed architecture, native settings (including `ignored-parents` and `stubPath`),
uv indexes, sources and metadata are retained. Missing native sections are initialized;
shared scope, numeric limits, plugin discovery and test profiles are regenerated.
`pyrightconfig.json` takes precedence over `[tool.pyright]` on installation.
Application files and root `AGENTS.md` are never generated.

Pass optional dependencies explicitly on first install, e.g. `--extra qwen`.
Repeat `--extra` for multiple extras. This selection is recorded in
`.eryx/installation.json` and reused on reinstallation. Keep the same extras on
ordinary `uv sync`/`uv run` commands to retain those dependencies.

Managed files are restored on failure; logs stay under `.eryx/logs/`. The uv cache
and virtual environment are not transactional: resync the restored lockfile if needed.
Dry-run writes no consumer files; uv bootstrap may populate its external cache.
Single projects are supported; uv workspaces and source symlinks are rejected.

### Architecture: explicit review

Installation does **not** accept architecture. For a new project:

```bash
uv run --locked --group dev python eryx/init_architecture.py --json
# Review/edit tach.toml: boundaries, layers, allowed dependencies, public exports.
uv run --locked --group dev python eryx/init_architecture.py --accept --json
```

Draft generation supports one package beneath `src/`, classifies conventional
directory names and runs `tach sync` once. Existing multi-package configurations
are supported and retained. `--force` explicitly replaces a draft. Current imports
are not automatically approved. Acceptance records a reviewed configuration, not
a violations baseline. After changing layers, repeat `--accept`; never sync before
routine checks. A project without accepted architecture cannot pass.

## Daily commands

```bash
uv run --locked --group dev python eryx/check.py
uv run --locked --group dev python eryx/check.py --json
# Edit .eryx/readability.toml or .eryx/scope.toml, then:
uv run --locked --group dev python eryx/apply.py --json
```

Every command accepts `--project` (defaults to the current directory). A separate
clone or a differently named submodule works with the consumer's interpreter:

```bash
uv run --locked --group dev python /path/to/eryx/check.py --project .
```

Check supports `--serial`, `--timeout 900` (per analyzer) and
`--max-diagnostics 50` (human output only). All jobs complete independently, with
up to four concurrent analyzers. Pylint runs production and test profiles separately,
with `jobs=1` to preserve whole-project checks. No concurrent dependency syncing.
LCOM4 runs as an independent job over discovered production and test `.py` classes;
see [PROFILE.md](PROFILE.md#basic-lcom4) for its metric and limitations.
The non-instance-method rule independently checks discovered `.py` and `.pyi` files
in both profiles, including when LCOM4 is narrowed or skipped.
The instance-field boundary rule uses the same full scope and reports proven
external access to consumer-defined stored fields, including first assignments
to known consumer instances. Literal-name built-in `getattr`/`setattr`/`delattr`
and known keys in `vars(obj)` or `obj.__dict__` follow the same boundary.
It forbids stored class values, including `Final` constants, at declaration and
statically named writes through known classes. Initialized dataclass instance
fields, Enum members and `__slots__` remain allowed.
The class-contract rule checks every discovered `.py` and `.pyi` class in both
profiles for an explicit inherited contract, independently of LCOM4 selection.

### Greenfield per-change LCOM4 checks

After any change in a greenfield project, run the normal check with `--lcom_files`
set to the changed Python files, or `--lcom_files ""` when no Python files changed:

```bash
uv run --locked --group dev python eryx/check.py --lcom_files "src/example.py tests/test_example.py"
uv run --locked --group dev python eryx/check.py --lcom_files ""
```

Pass project-relative `.py` paths under `src/` or `tests/`, separated by whitespace;
quote individual paths containing spaces inside the argument. Paths must exist and
belong to Eryx's discovered, non-excluded source scope. Repeated paths are checked
once. Only LCOM4 is narrowed: Pylint, Pyright, pydoclint and Tach keep their normal
project-wide coverage. An empty argument marks LCOM4 **skipped**, not passed.
The report's `coverage.lcom4` records the requested value, selected paths and
paths successfully analyzed; `tools` records the LCOM4 job status.

Run the **full check without `--lcom_files` after changes and in CI**:

```bash
uv run --locked --group dev python eryx/check.py
```

| Exit | Meaning |
|---:|---|
| 0 | All checks completed, no diagnostics |
| 1 | Code, documentation or architecture violations |
| 2 | Configuration/version/init failure, crash, timeout or parser failure |

Every native severity fails the check. Unknown failed output never becomes success.
Full diagnostics and per-tool statuses: `.eryx/reports/latest.json`. Archived reports
and raw stdout/stderr/native JSON: `.eryx/reports/<run-id>/`. The report contains
`eryx.commit` and `eryx.dirty` for the **running checkout** (null when unavailable).
Schemas: `eryx-report/v1` in `report.schema.json`, events: `eryx-event/v1`.
Positions are one-based; unavailable positions are null. Diagnostics retain native
rule IDs or explicit adapter categories, stable fingerprints and tool/profile identity.

## Ownership and fresh clones

```text
consumer/
  eryx/                    # immutable gitlink; tool code, pins, defaults, tests
  .eryx/                   # versioned consumer settings and integration state
    readability.toml
    scope.toml
    architecture.ready.json
    pylint-tests.toml
    pylint-catalog.json
    policy-notes.json
    installation.json
    reports/               # ignored
    logs/                  # ignored
    cache/                 # ignored
  pyproject.toml           # native configs remain in their standard locations
  uv.lock
  pyrightconfig.json
  tach.toml
```

Commit `.gitmodules`, the Eryx gitlink, `.eryx/` settings and native configs.
Ignore only runtime directories, not `.eryx/` wholesale. Analyzer caches and logs
are consumer-owned; uv retains its normal package cache. Tach may use `.tach/` in
the consumer. A normal install/check/apply/accept leaves the tool checkout clean.

```bash
git clone --recurse-submodules git@github.com:OWNER/CONSUMER.git
# Or in an existing clone:
git submodule update --init --recursive
uv sync --locked --group dev
uv run --locked --group dev python eryx/check.py
```

Legacy installations are migrated by reading their project settings into `.eryx/`
when missing. Remove the legacy scripts and self-extracting delivery from the
consumer after verifying migration. See [ORIGIN.md](ORIGIN.md).

## Development and integration

Reproduce and fix in the Eryx checkout. Add a standalone regression fixture
without consumer source dependencies. A separate Eryx clone with `--project`
works identically. Run Eryx tests and the consumer's checks/runtime tests. From
the consumer:

```bash
uv run --locked --group dev python -B -m unittest discover -s eryx/tests
```

Tests also need `jsonschema`, supplied by Eryx's own dev group.

When updating a consumer, pin a reachable Eryx commit, reinstall if configs or
dependencies changed, and run the consumer checks. Keep Eryx and consumer
histories separate.

Standalone development:

```bash
uv sync --locked --group dev
uv run --locked --group dev python -B -m unittest discover -s tests -v
uv run --locked --group dev python check.py
```

Eryx's implementation lives in `src/eryx/`. Root Python commands are compatibility
entry points for existing consumer integrations. The checkout has its own reviewed
`.eryx/` and `tach.toml`, so a full check from here analyzes its production modules
and tests; the resulting report is in this checkout's `.eryx/reports/latest.json`.
Consumer checks still write reports and caches only inside the selected consumer.

CI runs offline orchestration tests and real compatibility regressions on isolated
public fixtures; `LifecycleTests` is always skipped. Access to `tai` is not needed.
Eryx's lockfile fixes its test environment; consumer lockfiles fix theirs.

Profile/limits: [PROFILE.md](PROFILE.md). Compatibility rationale and provenance:
[COMPATIBILITY.md](COMPATIBILITY.md). Agent instructions: [AGENTS.md](AGENTS.md).
