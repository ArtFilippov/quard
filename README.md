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
optional checks, existing reviewed architecture, and native settings (including
`ignored-parents` and `stubPath`), uv indexes, sources and metadata are retained. Missing native
sections are initialized; shared scope, numeric limits, plugin discovery and test
profiles are regenerated.
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

### Generated architecture

On a new project without `tach.toml` or an accepted architecture marker, install
selects Eryx-managed contract-only boundaries. Commit
`.eryx/architecture.generated.json` and the installer's `/tach.toml` Git ignore
rule. The root `tach.toml` is generated from discovered production files, local
contracts and folder ownership; install, apply and check restore it when missing or
modified. Correct files are left untouched. Fresh clones need only sync dependencies
and run check. No manual acceptance or routine `tach sync` is required. Only eligible
source imports become exact dependency declarations; imports of private
implementations and utilities outside their contract folder remain native Tach
violations. Files anywhere under a production `main/` folder or a direct contract
`envelopes/` folder may assemble concrete implementations from any contract folder,
including type-only imports and companion stubs. Other modules' utilities stay
private. The generated policy also checks annotated parameters, stored fields
and returns on customer classes in source and stubs: concrete classes from
other modules require local contract types. Own types and `Self` are allowed;
`main/` and `envelopes/` may name concrete types. Explicit customer type
references that cannot be resolved receive separate diagnostics.
Free functions and assembly classes may live under
`main/`; placement checks continue outside it. Folder violations remain visible to
the independent checker. Malformed
source and regeneration failures are configuration errors (exit 2).

Existing consumer-owned Tach configurations remain on the reviewed-policy path:

Installation does **not** accept reviewed architecture. For a reviewed project:

```bash
uv run --locked --group dev python eryx/init_architecture.py --json
# Review/edit tach.toml: boundaries, layers, allowed dependencies, public exports.
uv run --locked --group dev python eryx/init_architecture.py --accept --json
```

Legacy draft generation supports one package beneath `src/`, classifies conventional
directory names and runs `tach sync` once. Existing multi-package configurations
are supported and retained. `--force` explicitly replaces a draft. Current imports
are not automatically approved. Acceptance records a reviewed configuration, not
a violations baseline. After changing layers, repeat `--accept`; never sync before
routine checks. A reviewed project without accepted architecture cannot pass.

For a legacy reviewable draft with local contracts in their required folders, use
file-level contract-only Tach boundaries with
`python eryx/init_architecture.py --contract-only --force --json`. Review the
resulting `tach.toml`, then run the ordinary `--accept`. This draft uses discovered
production files, contract declarations and folder ownership. It exposes only local
contracts (including verified package re-exports), keeps implementation and utility
imports within their owning contract folder, and declares only eligible dependencies
actually used by source. It does not run `tach sync`; forbidden imports remain
violations. The default draft and existing reviewed configurations are unchanged.

## Daily commands

```bash
uv run --locked --group dev python eryx/check.py
uv run --locked --group dev python eryx/check.py --json
# Edit .eryx/readability.toml or .eryx/scope.toml, then:
uv run --locked --group dev python eryx/apply.py --json
```

To opt in to constructor purity, set `constructor_purity = true` in
`.eryx/checks.toml` and run the ordinary full check. New installations contain
`constructor_purity = false`; older installations without `checks.toml` also
default to disabled. Reinstallation retains an explicit value. Invalid settings
fail with exit 2. The report records the disabled job as `skipped`, with no exit
code; no constructor manifest is created and its worker does not run.

Every command accepts `--project` (defaults to the current directory). A separate
clone or a differently named submodule works with the consumer's interpreter:

```bash
uv run --locked --group dev python /path/to/eryx/check.py --project .
```

Check supports `--serial`, `--timeout 900` (per analyzer) and
`--max-diagnostics 50` (human output only). All jobs complete independently, with
up to four concurrent analyzers. Pylint runs production and test profiles separately,
with `jobs=1` to preserve whole-project checks. No concurrent dependency syncing.
The pinned `jscpd` binary checks Type 1 (`exact`) and Type 2 (`renamed`) Python clones
in independent native jobs over the same discovered `src/` and `tests/` `.py` files,
including cross-profile matches. `.pyi` stubs are not a supported jscpd Python
extension. Type 2 matches normalize
identifiers and string/numeric literals. The native minimum is five lines and one
token in strict mode. It runs on the full scope even with `--lcom_files ""`.
Every clone produces a kind-specific positioned diagnostic for each fragment.
Each job saves its native JSON and raw streams with the report; an empty or
incomplete scan is an error.
LCOM4 runs as an independent job over discovered production and test `.py` classes;
see [PROFILE.md](PROFILE.md#basic-lcom4) for its metric and limitations.
The non-instance-method rule independently checks discovered `.py` and `.pyi` files
in both profiles, including when LCOM4 is narrowed or skipped.
When enabled, the constructor-purity rule independently checks eager constructor
syntax in all discovered production and test `.py` files, including nested classes,
even when LCOM4 is narrowed or skipped. Deferred nested bodies are allowed;
definition-time defaults and annotations, plus proven project decorator factories
and applications are checked. External decorators are opaque; unknown applications
fail. See
[PROFILE.md](PROFILE.md#constructor-purity).
The instance-field boundary rule uses the same full scope and reports proven
external access to consumer-defined stored fields, including first assignments
to known consumer instances. Literal-name built-in `getattr`/`setattr`/`delattr`
and known keys in `vars(obj)` or `obj.__dict__` follow the same boundary.
Stored receiver fields, source annotations and literal slots require a proven
assignment on every normal-return path of the owning constructor; literal-name
`setattr` and instance-dictionary writes through the receiver or a proven alias count.
Missing initialization is a violation. Standard dataclass-generated assignments count;
stub-only declarations establish ownership without an initialization check.
Stored fields also need a single resolved type: only exact `bool`, `int`, `float`,
`complex`, `str` or `bytes` field types permit reassignment after initialization.
Unknown types and conflicting source/stub evidence are violations; rebinding other
fields is prohibited, as is deleting an initialized field. `+=` after initialization
is allowed for every type but never initializes a field. Mutation remains allowed.
It forbids stored class values, including `Final` constants, at declaration and
statically named writes through known classes. Initialized dataclass instance
fields, Enum members and `__slots__` remain allowed.
The class-contract rule checks every discovered `.py` and `.pyi` class in both
profiles for an explicit inherited contract and public member coverage by local
contracts, independently of LCOM4 selection. See [PROFILE.md](PROFILE.md#tools-and-scope)
for the opaque-external-base exception.
It also rejects redeclarations of methods and recognized properties from any
resolvable customer-owned non-contract ancestor, while allowing declarations supplied
only by local contracts. Each member receives one violation per class and source file.
The independent outer-class rule checks every discovered production and test `.py` and
`.pyi` file, even when LCOM4 is narrowed or skipped. A file may declare at most one
class outside another class; function-local classes count as outer classes.
The contract-folder job checks local Protocol and abstract-interface declarations and
their implementations across the same scope, including companion stubs. A contract
named `HTTPPort` belongs in `http_port/http_port.py` (or `.pyi`); implementations
belong in that folder or its nested folders. See [PROFILE.md](PROFILE.md#tools-and-scope).
An optional direct `envelopes/` folder holds `HTTPPortEnvelope` in
`http_port/envelopes/http_port_envelope.py` and named envelope subclasses in
separate, class-named files; they inherit the parent contract through the base.
The independent test-placement job maps `TestHTTPPort` in
`tests/pkg/http_port/test_http_port.py` to the production `HTTPPort` declaration
in `src/pkg/http_port/`. The same mirrored layout applies to implementations
and envelopes; top-level `test_*` functions are violations.
The independent free-function-placement job checks discovered production and test
`.py` and `.pyi` files. Free synchronous and asynchronous functions belong in
class-free `<group_name>_utils.py` modules (or companion stubs); related functions
may share a module named for their shared purpose. Source and stub modules are
checked separately. Module-scope conditional declarations count, but methods and
functions inside functions do not. Every class in a utilities module is reported.
Read, parse, malformed-report and worker failures are infrastructure errors (exit 2);
raw stdout and stderr remain archived alongside the standard positioned diagnostics.

The independent encapsulated-only job checks explicit method calls and property access
restricted by a consumer-supplied identity decorator on a member or its class, including
resolvable inherited members, imported aliases and stub declarations. It permits
the current instance, one direct stored instance field, and proven current-instance
`super` access; proven calls on peers, new instances, property results, class
values and deeper chains fail with `ENCAPSULATED_ONLY_CALL` for methods and
`ENCAPSULATED_ONLY_PROPERTY` for property reads, writes and deletions. It also checks
extracted callable invocation, class-qualified unbound calls, literal-name builtin
`getattr` calls, and proven receiver aliases in nested functions. Statically resolved
construction, callable objects, subscriptions, attribute hooks, context managers,
iteration, awaiting, operators, comparisons, membership, truth tests and proven
builtin protocol helpers also check explicitly restricted protocol methods. It keeps
full discovered scope when LCOM4 is selected or skipped. See
[PROFILE.md](PROFILE.md#encapsulated-only-explicit-calls) for the marker convention
and implemented inference boundaries.
`ENCAPSULATED_ONLY_DECLARATION` also checks restriction agreement across explicitly
inherited contracts, newly declared implementations, companion stubs and overloads;
marking one property accessor marks the whole property for this comparison.

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
    checks.toml
    architecture.generated.json  # generated-policy selection on new installs
    architecture.ready.json      # legacy reviewed-policy acceptance
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
  tach.toml                # ignored when generated; committed when consumer-owned
```

Commit `.gitmodules`, the Eryx gitlink, `.eryx/` settings and consumer-owned native
configs. Generated `tach.toml` is ignored; commit its selection marker instead.
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
