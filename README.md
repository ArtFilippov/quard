# Eryx

Pinned native analysis for Linux `uv` projects; code-style checks use a `src/` layout.
Named after *Eryx jayakari*. Tools run in the **consumer's environment**;
Eryx is versioned independently and normally attached as a Git submodule.

## Select check groups (optional)

Without `.eryx/checks.toml`, existing consumers retain the code-style workflow
and project-wide notebook verification. To select groups and directories, create
the consumer-owned file **before installation**:

```toml
groups = ["anonymity"]
working_directories = ["review_target"]
```

The only groups are `codestyle` and `anonymity`; list both to enable both.
Directories must exist inside the consumer and cannot be symlinks. Directory
exclusions in `.eryx/scope.toml` still apply; an empty effective file scope is an
error. `anonymity` verifies notebooks with `nbstripout` and scans selected files
for potential secrets with `detect-secrets`, both through Pylint. Findings include
the file and line but not the secret value. Zero notebooks are explicitly reported,
not counted as verified. Reports distinguish selected `secret_inputs`, confirmed
`secret_scanned` / `secret_count`, and unsupported binary `secret_skipped` /
`secret_skipped_count`. An independent Presidio job scans the same effective file
scope in both English and Russian (including country-specific identifiers, dates,
URLs and IP addresses), using pinned local `en_core_web_sm` and `ru_core_news_sm`
models. It also reports `LOCAL_PATH_IDENTIFIER` for the account component of
conventional home, Users and scratch directories, and `REMOTE_ACCOUNT` for
user@host operands in SSH, SCP and SFTP commands. It reports entity type,
relative path, physical line/column, confidence score and span length without
matched values. `presidio_inputs`, `presidio_scanned` /
`presidio_count`, `presidio_skipped` / `presidio_skipped_count` and
`presidio_unconfirmed` separately describe selected, confirmed text, unsupported
binary and unconfirmed inputs. Missing resources, partial coverage and zero
scanned text files fail the check (exit 2), rather than claiming a clean scan. Each
text file is analyzed whole in both languages, up to 512 KiB of raw
UTF-8; larger files are unconfirmed and cause exit 2, without splitting an entity
across model windows. The worker checks file identity and timestamps around the
read and after analysis to reject changed files. Raw structured files and notebook
JSON are scanned as text, with physical file lines. Archived Presidio native output
contains only normalized status and coverage, never unvalidated worker output.
To flag known identifiers even inside comments, strings, compound names or
unusual paths and remote commands, optionally create consumer-owned
`.eryx/presidio.toml` outside the review target:

```toml
seeds = ["ExampleAlias", "ПримерПсевдоним"]
patterns = ["[0-9]{2}[a-z]_[a-z]{3}"]
```

List spelling variants and transliterations explicitly. Matching is literal and
case-insensitive, including substrings; each occurrence is reported as
`KNOWN_IDENTIFIER_SEED` with physical position and span length. Regex `patterns`
are separate from literal seeds; each nonempty match is reported as
`KNOWN_IDENTIFIER_PATTERN`, case-insensitively using Python's Unicode regex rules.
Missing files or empty collections leave the general-purpose recognizers active.
Invalid configuration
fails the Presidio job (exit 2) with a value-free error. Keep the private config
out of the review package; identifiers and matched values are never passed in process
arguments or emitted in reports or archived logs.

An anonymity-only consumer needs no `src/`, native code-analysis configuration or
accepted architecture. Code-style
analysis continues to require `src/` (or `.`) in `working_directories`; add `tests/` to
include root test files. Other selected directories participate in notebook
verification, while native code analyzers retain their `src/` and `tests/` scope.
The production Pylint worker verifies each notebook only once when both groups
are enabled. Run `eryx/apply.py` after changing selected code-style directories.

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

| Exit | Meaning |
|---:|---|
| 0 | All checks completed, no diagnostics |
| 1 | Code, documentation or architecture violations |
| 2 | Configuration/version/init failure, crash, timeout or parser failure |

Every native severity fails the check. Unknown failed output never becomes success.
Full diagnostics and per-tool statuses: `.eryx/reports/latest.json`. Archived reports
and raw stdout/stderr/native JSON: `.eryx/reports/<run-id>/`. The report contains
`eryx.commit` and `eryx.dirty` for the **running checkout** (null when unavailable).
For secret-enabled Pylint and Presidio workers, stdout/stderr are withheld from archived logs
because third-party failures can include source contents; structured status and
diagnostics remain in the report.
Schemas: `eryx-report/v1` in `report.schema.json`, events: `eryx-event/v1`.
Positions are one-based; unavailable positions are null. Diagnostics retain native
rule IDs or explicit adapter categories, stable fingerprints and tool/profile identity.

## Ownership and fresh clones

```text
consumer/
  eryx/                    # immutable gitlink; tool code, pins, defaults, tests
  .eryx/                   # versioned consumer settings and integration state
    presidio.toml          # optional private literal seeds and regex patterns
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

Commit `.gitmodules`, the Eryx gitlink, non-private `.eryx/` settings and native configs;
keep private seeds outside the published review package.
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
consumer in its integration PR after verifying migration. See [ORIGIN.md](ORIGIN.md).

## Development and PR workflow

1. In the affected consumer, create a branch inside Eryx first (submodules usually
   start detached): `git -C eryx switch -c fix/description`.
2. Reproduce and fix there. Add a standalone regression fixture without consumer
   source dependencies. A separate Eryx clone with `--project` works identically.
3. Run Eryx tests and the consumer's checks/runtime tests. From the consumer:
   `uv run --locked --group dev python -B -m unittest discover -s eryx/tests`.
   Tests also need `jsonschema`, supplied by Eryx's own dev group.
4. Commit the tool branch in Eryx. After testing the actual local revision
   (including a local merge result, if applicable), pin its exact SHA in a
   separate consumer change and run the consumer checks immediately. Reinstall
   if configs/dependencies changed. Record the running SHA, gitlink, checkout
   state and test outcomes. The consumer and Eryx remain separate repositories.
5. Publish the tool changes in an Eryx PR with `gh pr create --repo ArtFilippov/eryx`
   (or update the existing PR). Include reproduction and verification; record
   the PR link, or publication pending until it exists. Local integration and
   completion depend on implementation and verification, not external PR review
   or merge. Distinguish publication, review and merge in status reports.
6. After upstream merge, fetch Eryx and synchronize the consumer gitlink to the
   reachable merged SHA in a separate follow-up. Rerun affected checks and link
   the Eryx PR. A tested local pin is locally integrated, not a claim of upstream
   merge.

Standalone development:

```bash
uv sync --locked --group dev
uv run --locked --group dev python -B -m unittest discover -s tests -v
# Only when explicitly requested, run the slower real install/reinstall tests:
ERYX_RUN_LIFECYCLE=1 uv run --locked --group dev python -B -m unittest discover -s tests -p test_lifecycle.py -v
```

The default suite skips the slow `LifecycleTests`; run them only on an explicit
request. Offline orchestration and real compatibility regressions still run by
default; access to `tai` is not needed. Eryx's lockfile fixes its test environment;
consumer lockfiles fix theirs.

Profile/limits: [PROFILE.md](PROFILE.md). Compatibility rationale and provenance:
[COMPATIBILITY.md](COMPATIBILITY.md). Agent instructions: [AGENTS.md](AGENTS.md).
