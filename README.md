# Quard

Pinned native Python analysis for Linux `uv` projects with a `src/` layout.
Tools run in the **consumer's environment**. Quard is versioned independently
and normally attached as a Git submodule.

## Install

From the consumer root (Python >= 3.12 and `uv` required):

```bash
git submodule add git@github.com:ArtFilippov/quard.git quard
bash quard/install.sh --project .
```

Install requires Rust/Cargo and a working C compiler/linker on every consumer
machine. uv builds `quard-clones==0.1.0` from the selected Quard Git revision
using pinned `maturin==1.9.6` and `rust/quard-clones/Cargo.lock`; no manual wheel
step or published wheel is needed. The revision must be fetchable from the
tool clone's `origin` and, for submodules, match the consumer's staged gitlink.
Commit the generated `[tool.uv.sources].quard-clones` and `uv.lock` alongside
the gitlink; push that commit before running locked sync on a fresh machine.
Use a shared private Git remote for committed locks, not a developer-local
`file://` origin (the latter is used only by the disposable public test fixture).
Verify the `rev` in `pyproject.toml` against the staged gitlink (`git ls-files
--stage -- path/to/quard`), and the `quard-clones` Git source in `uv.lock`
against that same commit. `uv sync --locked --group dev` rejects stale lock
metadata and builds from the pinned source without writing to the tool checkout.
uv caches the source and built wheels outside the tool checkout; a new revision
or interpreter/build environment may trigger another build.

The installer pins dev dependencies, updates the consumer lockfile, synchronizes
its environment, initializes `.quard/` and native configs, then runs real analyzer
smoke tests. It does not copy or modify Quard sources. Inspect a plan with:

```bash
bash quard/install.sh --project . --dry-run --json
```

Reinstall with the same command after updating the gitlink. For an existing CLI
installation, follow the [CLI → pinned Git source migration guide](docs/clone-cli-migration.md).
Project limits, scope, optional checks, existing reviewed architecture, and
native settings (including `ignored-parents` and `stubPath`), uv indexes,
sources and metadata are retained. Missing native
sections are initialized; shared scope, numeric limits, plugin discovery and test
profiles are regenerated.
`pyrightconfig.json` takes precedence over `[tool.pyright]` on installation.
Application files and root `AGENTS.md` are never generated.

Pass optional dependencies explicitly on first install, e.g. `--extra qwen`.
Repeat `--extra` for multiple extras. This selection is recorded in
`.quard/installation.json` and reused on reinstallation. Keep the same extras on
ordinary `uv sync`/`uv run` commands to retain those dependencies.

Managed files are restored on failure; logs stay under `.quard/logs/`. The uv cache
and virtual environment are not transactional: resync the restored lockfile if needed.
Dry-run writes no consumer files; uv bootstrap may populate its external cache.
Single projects are supported; uv workspaces and source symlinks are rejected.

### Generated architecture

On a new project without `tach.toml` or an accepted architecture marker, install
selects Quard-managed contract-only boundaries. Commit
`.quard/architecture.generated.json` and the installer's `/tach.toml` Git ignore
rule. The root `tach.toml` is generated from discovered production files, local
contracts and nearest-directory ownership across multiple import packages; install,
apply and check restore it when missing or modified. Correct files are left untouched.
Fresh clones need only sync dependencies
and run check. No manual acceptance or routine `tach sync` is required. Only eligible
source imports become exact dependency declarations; unauthorized imports of concrete
implementations and private utilities remain native Tach violations. Files anywhere
under a production `main/` folder or anywhere in a group's `envelopes/` subtree may assemble
concrete implementations from other groups,
including type-only imports and companion stubs. Ordinary directories beside
`envelopes/` follow ordinary rules. Exactly one
`main/` directory is allowed across `src/`, even if another is empty. Neither
envelopes nor main may import Bootstrap modules, including in stubs; Bootstrap
modules may not import one another. Package facades may be empty or
re-export proven contracts directly, through aliases or through ancestor facades;
source and companion stub evidence must agree. Concrete classes and factory values
cannot be exported, even when unused or listed in `__all__`. Import implementations
from their defining modules in assembly; split out concrete facade exports during
migration. Contract modules import only contracts; ordinary infrastructure modules
import only contracts or external packages, even within their own folder. Source imports
of whole contract modules are eligible, while assembly imports concrete symbols,
including top-level functions, from their defining modules. Other modules' utilities
stay private to ordinary consumers. Ordinary modules named `main.py`
do not gain assembly privileges. A Python
file or stub directly under `src/` receives a positioned layout violation; other
file types do not. Empty package facades and extra import packages are supported.
Existing version-1 generated selections keep their previous boundaries; migrate
explicitly by updating the tracked marker to version 2 before running apply or check.
The generated policy also checks annotated parameters, stored fields
and returns on customer classes in source and stubs: concrete classes from
other modules require local contract types. Own types and `Self` are allowed;
`main/` and `envelopes/` may name concrete types. Explicit customer type
references that cannot be resolved receive separate diagnostics.
Generated version-2 folder analysis also proves roles from local Protocol and
abstract-interface declarations: a directory may own multiple contracts and an
implementation may inherit several of its own group's contracts. An implementation
using contracts from another directory must directly inherit a combined contract
declared in its own group that covers those contracts. Mixed contract/concrete
modules, conflicting source/stub class roles and unresolved local bases receive
positioned violations. Reviewed configurations keep their existing folder rules.
Exact external types explicitly inherited by a local class, including generic
bases and `Exception`, may be re-exported as contracts elsewhere in the project.
Generic exports must retain the same specialization: inheriting `UserList[str]`
does not expose bare `UserList` or `UserList[int]` through an alias.
Other symbols from the same package and local concrete subclasses are not contracts;
unproved or conflicting external bases receive positioned findings. External
dependency validation still runs separately through native Tach.
Proven `typing.NamedTuple`/`collections.namedtuple` factory assignments are concrete
classes, count toward the one-outer-class limit in source, stubs and tests, and
cannot be exported as contracts. Put explicit Protocol value shapes in separate
contract modules; annotate nested fields and cross-group consumers with those
contracts. Concrete factory imports from ordinary production modules, including
stubs, and concrete field/parameter/result annotations receive positioned findings.
Unproved or shadowed factories and conflicting source/stub value evidence are
reported rather than granting contract visibility. For migration, split files with
multiple factory values and move concrete assembly imports into `envelopes/` or
`main/`.
Generated version-2 ordinary modules also reject concrete class and module imports
in source and stubs (including `TYPE_CHECKING`), direct concrete inheritance and
concrete value references even within one file or group. Own-class references and proven
contracts remain usable; tests can compose concrete implementations freely.
Free functions and assembly classes may live under
`main/`; placement checks continue outside it. The generated version-2 folder job
reports source-root layout; reviewed and generated version-1 projects retain their
contract-name folder diagnostics. Malformed
source and regeneration failures are configuration errors (exit 2).

Generated projects run Pylint core and configured official extensions without the
legacy clean-architecture plugin or its layer map. Protocol compatibility fixes
still apply. Reinstall to migrate an older generated Pylint configuration; apply
also restores the generated profile. Plugin-only W9xxx findings are not checks in
generated mode. Existing consumer-owned Tach configurations remain on the
reviewed-policy path:

Installation does **not** accept reviewed architecture. For a reviewed project:

```bash
uv run --locked --group dev python quard/init_architecture.py --json
# Review/edit tach.toml: boundaries, layers, allowed dependencies, public exports.
uv run --locked --group dev python quard/init_architecture.py --accept --json
```

Legacy draft generation supports one package beneath `src/`, classifies conventional
directory names and runs `tach sync` once. Existing multi-package configurations
are supported and retained. `--force` explicitly replaces a draft. Current imports
are not automatically approved. Acceptance records a reviewed configuration, not
a violations baseline. After changing layers, repeat `--accept`; never sync before
routine checks. A reviewed project without accepted architecture cannot pass.

For a legacy reviewable draft with local contracts in their required folders, use
file-level contract-only Tach boundaries with
`python quard/init_architecture.py --contract-only --force --json`. Review the
resulting `tach.toml`, then run the ordinary `--accept`. This draft uses discovered
production files, contract declarations and folder ownership. It exposes only local
contracts (including verified package re-exports), keeps implementation and utility
imports within their owning contract folder, and declares only eligible dependencies
actually used by source. It does not run `tach sync`; forbidden imports remain
violations. The default draft and existing reviewed configurations are unchanged.

## Daily commands

```bash
uv run --locked --group dev python quard/check.py
uv run --locked --group dev python quard/check.py --json
# Edit .quard/config.toml ([checks], [readability.*], [scope]), then:
uv run --locked --group dev python quard/apply.py --json
```

Select independent jobs under `[checks]` in `.quard/config.toml`:

| Setting (default) | Job |
|---|---|
| `lcom4 = true` | `lcom4` |
| `method_decorators = true` | `method-decorators` (decorators and receivers) |
| `instance_fields = true` | `instance-fields` |
| `class_contracts = true` | `class-contracts` |
| `contract_folders = true` | `contract-folders` |
| `test_placement = true` | `test-placement` |
| `outer_classes = true` | `outer-classes` |
| `free_function_placement = true` | `free-function-placement` |
| `leading_underscores = true` | `leading-underscores` (first-party names and paths) |
| `encapsulated_only = true` | `encapsulated-only` |
| `constructor_purity = true` | `constructor-purity` |
| `lambda_injection = true` | `lambda-injection` |
| `pyright = true` | `pyright` |
| `pydoclint = true` | `pydoclint` |
| `duplicate_detection = true` | `jscpd`, `jscpd-renamed`, `jscpd-similar` |
| `tach = true` | `tach`, `tach-external` (two separate native results) |
| `dependency_types` | `dependency-types` (generated architecture only) |

Pylint's production and test profiles remain required. `dependency_types` defaults
to `true` on generated projects and `false` on reviewed projects, including upgrades.
Explicitly enabling it on a reviewed project is a configuration error (exit 2).
`tach` defaults to `true`; disabling it skips both Tach jobs and removes the
Tach-only acceptance requirement for reviewed projects. Generated architecture
still regenerates `tach.toml` even with Tach disabled. With Tach enabled,
reviewed projects still require acceptance.
Other switches default to `true` on fresh installations and upgrades; explicit
values survive reinstallation. Missing new keys use those defaults until upgrade.
All values must be booleans; unknown keys fail
with exit 2. Disabled jobs appear as `skipped` with null exit codes and have no
manifest or worker execution. Missing `constructor_purity` also defaults to `true`;
set it to `false` to skip the job.
Disabling duplicate detection skips all three clone jobs, their manifests and their
native JSON, without changing Pylint's required duplicate-code rule. Pyright and
pydoclint likewise skip only their own jobs; their native configuration remains
consumer-owned. All consumer-pinned tools are still installed and version-checked
on every check; installation runs source-built clone and other native smoke
probes regardless of these switches. The CLI remains dev-only in this checkout.
If `lcom4 = false`, passing `--lcom_files` (even `""`) is a configuration error
(exit 2); when enabled, full, selected and empty-string coverage work as below.
Installation migrates existing `.quard/readability.toml`, `.quard/scope.toml` and
`.quard/checks.toml` into the single configuration (constructor purity defaults
to true), then removes those three files. It retains existing limits, test
overrides and scope. Check/apply before migration fail with exit 2 and instruct
you to reinstall. Installation rollback restores the original files on failure.
Use `apply.py` after changing readability or scope; check reports unapplied
native limits or scope as configuration errors. Unknown keys and check names fail.

Every command accepts `--project` (defaults to the current directory). A separate
clone or a differently named submodule works with the consumer's interpreter:

```bash
uv run --locked --group dev python /path/to/quard/check.py --project .
```

Check supports `--serial`, `--timeout 900` (per analyzer) and
`--max-diagnostics 50` (human output only). All jobs complete independently, with
up to four concurrent analyzers. Pylint runs production and test profiles separately,
with `jobs=1` to preserve whole-project checks. No concurrent dependency syncing.
The pinned first-party `quard-clones` engine checks Type 1 (`exact`), Type 2 (`renamed`) and
gap-merged Type 3 (`similar`) Python clones in independent native jobs over the
same discovered `src/` and `tests/` `.py` files, including cross-profile matches.
`.pyi` stubs are not a supported jscpd Python extension. Type 2 matches normalize
identifiers and string/numeric literals. The `similar` job merges detected fragments
separated by at most **two unmatched executable constructs in each file** (`--max-gap-lines 2`);
it records native similarity and the `gap` method. Merging cannot detect copies
whose individual matching parts fall below the configured minimum and
one token in strict mode. The JavaScript/TypeScript-only AST `--similarity` pass
does not scan Python. The jobs run on the full scope even with `--lcom_files ""`.
The renamed pass prepares three nested calls starting on distinct source lines
as complete subtree candidates, comparing only disjoint subtrees. Surrounding
constructors need not match. Literal-only and punctuation-only continuation lines
do not count toward that pass's prepared behavioral minimum. Its source accounting
reflects this view and records additional complete-call candidates separately;
native kinds and original fragments remain intact.
Callable and class identities in that pass remain significant, both at call sites
and when passed into composition calls. Proven import and simple binding aliases
share the same identity; unresolved calls retain their spelling. Other local names
retain their within-unit repeated/distinct relationships under renaming, and
eligible string and numeric literals remain normalized. Exact and similar passes
continue to use their own independent views; similar uses exact complete behavioral
constructs, without schema declarations, nested-call candidates or normalization.
Keyword labels and proven ordinary stored-field names also retain their
repeated/distinct relationships when renamed. Field proof uses discovered class
constructors, known receiver types and safe import/local aliases; property access
and unproved attributes retain their spelling. Called method names remain significant.
The renamed behavior view omits simple value bindings (including proven ordinary
field reads) and simple returns. Empty conditionals, matches, loops and context
managers are pruned recursively, even when their condition or context calls a
function. Pruned empty controls separate surviving behavior into distinct renamed
detection units. Surviving control headers constrain comparisons but add no eligible
line; call-containing assignments, operations, indexing, property and unknown attribute
reads remain behavioral. Exact and similar retain their independent source views.

Set `[duplication] min_units = 3` in `.quard/config.toml` to choose one positive
integer minimum for all three Python clone jobs. Fresh installs use 3; existing
configs without the section or key also use 3. Reinstall migrates an explicit
positive `min_lines` value to `min_units` without changing its number. Check and
apply require reinstall until that migration is done. Both names together, unknown
keys and invalid numbers fail with exit 2. An explicit `min_units` choice survives
install, reinstall and apply. This setting is independent of
Pylint's required `[readability.production] min_similarity_lines` limit and
`[checks] duplicate_detection`, which selects the three jscpd jobs together.
The historical native CLI scans unprepared `.py` files and counts physical source
lines. The installed Rust path prepares discovered files in memory, applying the
same minimum to eligible lines within each module, class or function unit.
File-level and class-level imports, actual docstrings, attribute documentation,
ordinary class/function headers (including decorators, multiline signatures and
annotations), annotation-only fields and recognized type aliases do not count.
Function-local imports, value assignments (including annotated values and literal
constants), control flow and executable bodies, including decorator implementations
and real contract/special-method bodies, do count. Recognized aliases are
`type Name = ...` and assignments annotated with explicitly imported
`typing.TypeAlias` or `typing_extensions.TypeAlias` (including import and module
aliases). Ordinary standalone `pass`, `...` and `raise NotImplementedError`
(bare or constructed with literal arguments) are excluded, including in
documented stubs. They cannot turn two executable lines into a three-line clone.
Other executable statements, including `return None`, computed raise arguments,
and real bodies in contracts or special methods remain eligible. The exact Rust
pass also scans consecutive fields of class-based and functional TypedDicts
imported from `typing` or `typing_extensions`, including explicit import aliases.
Functional dictionary keys or keyword fields must each occupy a separate
original line, with the name and type on that line and no opening/closing
syntax there. Compact forms are outside this policy and receive no invented
positions. Each schema is its own unit: class and factory names do not count,
but an identical trailing run of at least `min_units` eligible field units
does, even after different leading fields and across syntax forms. Different
names or types are not normalized; schema declarations do not enter renamed or
similar behavior views. Native reports retain original columns and fragments.
Ordinary annotation-only fields remain excluded. The exact Rust view also
matches ordered method declarations in local Protocols and abstract interfaces,
regardless of class name. Exact names and signatures and their one-line `pass`,
`...` or `raise NotImplementedError` placeholders count as physical eligible
lines. Getter, setter and deleter declarations carry the public property name
and accessor kind; changed names, signatures or method order do not become
equivalent through normalization. Supported multiline signatures count their
original signature lines. Native JSON and normalized details retain original
columns, ranges and verbatim multiline fragments, never flattened declarations.
Contract headers, decorators and documentation do not count, regardless of
the length of the reported original range. Special `__dunder__` declarations
are excluded; executable contract, property and special-method bodies remain
behavioral. Declaration placeholders do not enter renamed/similar. Exact behavior
compares complete executable AST constructs: each assignment, return or control
header counts once, even when it spans several physical lines. Three nested
exact calls on distinct lines may form a separate complete-call candidate when
their enclosing calls differ. Literal spellings remain exact; partial call
suffixes, punctuation-only continuations and partial conditions cannot supply
the minimum. Declaration-only exact units stay independent. Views with no
qualifying unit are explicitly skipped with a reason.

The exact view additionally compares proven class-based `typing.NamedTuple`
and functional `typing.NamedTuple`/`collections.namedtuple` declarations in separate schema
units. Each eligible field occupies its own original line; names, types and
defaults must agree exactly. Compact fields have no invented positions and do
not enter behavioral views. Real methods and surrounding executable statements
retain their normal clone checks. Original paths, columns and fragments survive
native and normalized reports. Rust reports retain
original paths, positions and verbatim fragments, with source digests and
per-unit accounting.
Ordinary installs run all three independent jobs through the pinned Rust
in-memory engine over the same discovered scope. Similar
merges native fragments separated by at most two unmatched constructs in each file;
each constituent fragment must clear the same detection minimum (and one token)
before merging. Gap detection and merging stay inside the same prepared
module, class or function unit on both sides; independent units in one file
cannot be joined. Blank lines, comments, physical continuations and excluded
scaffolding inside a unit
do not count toward the minimum or the two unmatched *eligible-construct* gap,
but remain in original source ranges when a match spans them. Native
`unmatchedLines` counts prepared gap constructs; `lines`, positions and `fragment`
refer to the original source, which may span more lines than were eligible.
It reports only native Type 3 `similar` clones with score and `gap` method.
Renamed uses native strict identifier and string/numeric literal
normalization and reports only native Type 2 clones; the exact job retains its
Type 1 findings. Each job has its own archived native JSON and streams. Each native
JSON identifies its pass and `minimumUnits`, `eligibleUnits`, `unitSizes`,
`unitCount`, `scannedUnitSizes` and `scannedUnitCount` per original path; renamed
also records separate complete-call `candidateUnitSizes`. A clone records
`matchedUnits` for both sides, while `lines`, ranges and fragments remain physical
source evidence. Similar `unmatchedLines` counts unmatched prepared constructs. A
missing or mismatched built component is an infrastructure error for the affected
jobs. See [ADR 0005](docs/adr/0005-in-memory-native-clone-engine.md).
On Linux x86-64 glibc, source builds and CPython 3.12/3.13 have been exercised;
ARM64, musl and CPython 3.14 source installs remain unverified. The PyO3
`abi3-py312` build targets CPython 3.12+, not PyPy. `Cargo.lock` fixes the Rust
dependencies, including linked jscpd 5.4.0 crates `cpd-core 0.1.20`,
`cpd-finder 0.1.20`, `cpd-tokenizer 0.1.19` and `cpd-reporter 0.1.21`.
Install and check validate the distribution, wheel ABI and linked crate versions;
install also runs packaged exact/renamed/gap-similar and clean probes with logs
under `.quard/logs/install-probes/`. Pylint's required duplication analysis
remains independent. The jscpd CLI is retained in Quard's dev group solely for
historical benchmark comparisons, not installed or probed in consumers. This
checkout's locked dev group builds `quard-clones` from `rust/quard-clones`; its
ordinary full check runs the same three native jobs as consumers. Consumers
independently pin a Git revision in their own `pyproject.toml` and `uv.lock`;
the checkout's local path source is not installed into them. Older Quard gitlinks
retain their own CLI behavior; after upgrading Quard, reinstall to acquire the
pinned Git source or check exits 2
with a migration link and three explicit job errors.
The reproducible same-scope CLI/Rust performance comparison and seven-run
measurements are in [the #126 benchmark](docs/issue-126-clone-benchmark.md).
The completed-policy [#148 verification](docs/issue-148-clone-verification.md)
includes all three original false-positive reproducers, positive real-engine
controls and a fresh same-scope seven-run comparison. The CLI's `--min-lines 3`
counts original physical lines; Rust's `min_units = 3` counts eligible prepared
units inside independent AST-owned views. See the [glossary](GLOSSARY.md) and
[ADR 0007](docs/adr/0007-native-ast-clone-units.md) for these boundaries.
The source-install and separate-tool wheel-install lifecycle fixtures are
permanently skipped. They formerly covered uv install, reinstall, locked sync,
full check and clean tool trees from separate clones and nonstandard submodules.
The rollout history is in
[the #127 integration record](docs/issue-127-clone-rollout.md).
Every clone produces a kind-specific positioned diagnostic for each fragment.
Each job saves its native JSON and raw streams with the report; an empty or
incomplete scan is an error.
LCOM4 runs as an independent job over discovered production and test `.py` classes;
proven production assembly Envelopes are exempt when `contract_folders` is enabled.
See [PROFILE.md](PROFILE.md#basic-lcom4) for its metric and limitations.
The non-instance-method rule independently checks discovered `.py` and `.pyi` files
in both profiles, including when LCOM4 is narrowed or skipped.
When enabled, the constructor-purity rule independently checks eager constructor
syntax in all discovered production and test `.py` files, including nested classes,
even when LCOM4 is narrowed or skipped. Deferred nested bodies are allowed;
definition-time defaults and annotations, plus proven project decorator factories
and applications are checked. External decorators are opaque; unknown applications
fail. See
[PROFILE.md](PROFILE.md#constructor-purity).
The independent lambda-injection job checks argument provenance and direct or locally
aliased lambda syntax in production `main/` descendants and files in a contract
group's `envelopes/` subtree,
for both generated and reviewed architecture. It does not narrow with LCOM4;
see [PROFILE.md](PROFILE.md#lambda-injection).
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
profiles for an explicit inherited contract, independently of LCOM4 selection.
Implementation methods and properties absent from inherited local contracts are
implementation-only members, including when an unrelated contract has the same name.
Reviewed production Pylint disables W9201 through its native configuration on
installation and apply; W9202 and other enabled rules remain active. Generated
projects do not load the legacy plugin. This policy is independent of the optional
leading-underscore check.
It also rejects redeclarations of methods and recognized properties from any
resolvable customer-owned non-contract ancestor, while allowing declarations supplied
only by local contracts. Each member receives one violation per class and source file.
Reviewed W9007 treats methods declared in explicitly inherited local Protocols
or abstract interfaces as contract-facing, including inherited aliases and `_`
names. Helpers outside those contracts can use the precise annotated Domain
Protocol return exception regardless of spelling. Bootstrap wiring is exempt
outside proven contract methods; unknown bases, unrelated same-named contracts
and `*Repository` suffixes grant no contract status. Disabling the naming job
does not change W9007; see [ADR 0006](docs/adr/0006-inherited-contract-return-boundaries.md).
The independent outer-class rule checks every discovered production and test `.py` and
`.pyi` file, even when LCOM4 is narrowed or skipped. A file may declare at most one
class outside another class; function-local classes count as outer classes.
The contract-folder job checks local Protocol and abstract-interface declarations and
their implementations across the same scope, including companion stubs. A contract
named `HTTPPort` belongs in `http_port/http_port.py` (or `.pyi`); implementations
belong in that folder or its nested folders. See [PROFILE.md](PROFILE.md#tools-and-scope).
In reviewed and both generated architecture versions, a base Envelope anywhere in an
`envelopes/` subtree must inherit one proven contract, store one constructor delegate
with either no other work or one preceding zero-argument `super().__init__()`,
and directly return at least one method call on that delegate with unchanged arguments,
or directly forward a property accessor. Proven `typing`/`typing_extensions.override`
decorators, including aliases, are optional. Every implemented member must forward this
way; async methods may directly await the delegate call. An assembly
Envelope inherits a proven base (including one outside `envelopes/`) or another
assembly Envelope, declares only a constructor, and calls `super().__init__(...)`
    exactly once as an unconditional statement reached on every normal constructor path.
    Class and file names do not select roles.
Every `envelopes/` subtree contains only these Envelopes, with technical
package initializers, matching stubs and mirrored test modules supported. Source
    proves behavior; companion stubs must agree with source ancestry, member kinds,
    property accessors and any declared constructor's parameter structure (a base stub
    may omit its constructor). Package initializers may contain imports (including
    `TYPE_CHECKING` imports), docstrings, `pass` and literal `__all__` exports,
    but not executable declarations or classes.
    External inherited contracts need no member enumeration.
Assembly rights for concrete imports and annotations are inherited at every depth;
ordinary modules, including stubs, remain restricted. The pattern intends to
compose implementations, but these checks prove only forwarding and parent
initialization, not which objects are composed at runtime.
With `contract_folders` enabled, Pyright omits only `reportMissingSuperCall` and
`reportImplicitOverride` inside discovered production `envelopes/` subtrees; outside
them both rules remain active. Production Pylint R0903 exempts only proven base and
assembly Envelopes, without changing the configured minimum or other diagnostics.
Turning off `contract_folders` restores both native rules for Envelopes. See
[ADR 0008](docs/adr/0008-scoped-envelope-native-exceptions.md).
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

The independent `leading-underscores` job checks discovered production, test and
companion-stub paths and Python bindings (including nested declarations, parameters,
assignments, import aliases, unchanged imports from discovered first-party modules,
and stored receiver fields). It reports leading `_`
in module and directory names once per discovered path component. Proven first-party
attribute writes, literal-name built-in setters and instance-dictionary writes
(including known dictionary aliases), plus literal string, sequence and mapping
`__slots__` (also when annotated), are declarations. The
standalone `_` and Python-defined special names such as `__init__`, `__slots__`,
`__all__` and
exception fields (`__traceback__`, `__cause__`, `__context__`,
`__suppress_context__`, `__notes__`) are allowed, as are required
`__init__.py`/`__main__.py` modules; invented `__custom__` names are not.
Unaliased imports from external libraries, external attributes and excluded
paths are not checked. Set `[checks] leading_underscores = false` to skip only this
job. It defaults to on for existing projects even before reinstall; reinstall adds
the explicit setting. Existing Quard source and test names are deliberately reported
by its own full check: migrate them when ready, or opt out explicitly. Enabled
violations exit 1; parse, read and invalid worker output exit 2, with findings
from other files retained. Source names have one-based positions; path components
have no source position.

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
uv run --locked --group dev python quard/check.py --lcom_files "src/example.py tests/test_example.py"
uv run --locked --group dev python quard/check.py --lcom_files ""
```

Pass project-relative `.py` paths under `src/` or `tests/`, separated by whitespace;
quote individual paths containing spaces inside the argument. Paths must exist and
belong to Quard's discovered, non-excluded source scope. Repeated paths are checked
once. Only LCOM4 is narrowed: Pylint, Pyright, pydoclint and Tach keep their normal
project-wide coverage. An empty argument marks LCOM4 **skipped**, not passed.
The report's `coverage.lcom4` records the requested value, selected paths and
paths successfully analyzed; `tools` records the LCOM4 job status.

Run the **full check without `--lcom_files` after changes and in CI**:

```bash
uv run --locked --group dev python quard/check.py
```

| Exit | Meaning |
|---:|---|
| 0 | All checks completed, no diagnostics |
| 1 | Code, documentation or architecture violations |
| 2 | Configuration/version/init failure, crash, timeout or parser failure |

Every native severity fails the check. Unknown failed output never becomes success.
Full diagnostics and per-tool statuses: `.quard/reports/latest.json`. Archived reports
and raw stdout/stderr/native JSON: `.quard/reports/<run-id>/`. The report contains
`quard.commit` and `quard.dirty` for the **running checkout** (null when unavailable).
Schemas: `quard-report/v1` in `report.schema.json`, events: `quard-event/v1`.
Positions are one-based; unavailable positions are null. Diagnostics retain native
rule IDs or explicit adapter categories, stable fingerprints and tool/profile identity.
For clone findings, `native_details` retains `kind`, both original `startLoc` and
`endLoc` positions, verbatim `fragment`, and `matchedUnits` for each side; source
`lines` and ranges are physical, not the eligible-unit minimum. Similar also
records native `unmatchedLines`, score and `gap` method. Inspect each clone job's
`status`, `exit_code` and archived `raw_output.native_json` independently;
`skipped` means no qualifying view or an explicitly disabled job, while `error` and infrastructure diagnostics
require investigation even if another pass found violations.

## Ownership and fresh clones

```text
consumer/
  quard/                    # immutable gitlink; tool code, pins, defaults, tests
  .quard/                   # versioned consumer settings and integration state
    config.toml              # editable check selection, readability and scope
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

Commit `.gitmodules`, the Quard gitlink, `.quard/` settings and consumer-owned native
configs. Generated `tach.toml` is ignored; commit its selection marker instead.
Ignore only runtime directories, not `.quard/` wholesale. Analyzer caches and logs
are consumer-owned; uv retains its normal package cache. Tach may use `.tach/` in
the consumer. A normal install/check/apply/accept leaves the tool checkout clean.

```bash
git clone --recurse-submodules git@github.com:OWNER/CONSUMER.git
# Or in an existing clone:
git submodule update --init --recursive
uv sync --locked --group dev
uv run --locked --group dev python quard/check.py
```

Legacy installations are migrated by reading their project settings into `.quard/`
when missing. Remove the legacy scripts and self-extracting delivery from the
consumer after verifying migration. See [ORIGIN.md](ORIGIN.md).

## Development and integration

Reproduce and fix in the Quard checkout. Add a standalone regression fixture
without consumer source dependencies. A separate Quard clone with `--project`
works identically. Run Quard tests and the consumer's checks/runtime tests. From
the consumer:

```bash
uv run --locked --group dev python -B -m unittest discover -s quard/tests
```

Tests also need `jsonschema`, supplied by Quard's own dev group.

When updating a consumer, pin a reachable Quard commit, reinstall if configs or
dependencies changed, and run the consumer checks. Keep Quard and consumer
histories separate.

Standalone development:

```bash
uv sync --locked --group dev
uv run --locked --group dev python -B -m unittest discover -s tests -v
uv run --locked --group dev python check.py
```

The checkout's dev-only `[tool.uv.sources].quard-clones` points to its local
Rust directory; `uv sync --locked --group dev` builds that source into the
checkout environment. The pinned jscpd CLI remains available for the historical
[CLI/Rust benchmark](docs/issue-126-clone-benchmark.md), not for regular checks.
After editing Rust sources, use
`uv sync --locked --group dev --reinstall-package quard-clones` to rebuild the
local wheel before running checks; uv may otherwise reuse the installed wheel.

For faster local feedback, run the Quard tests incrementally with the pinned
`pytest-testmon` plugin. The first run covers the whole suite and creates an
ignored `.testmondata` database; repeat the same command after edits to select
affected tests (including tests that failed last time):

```bash
uv run --locked --group dev python -B -m pytest tests -q \
  -p no:tach -o cache_dir=.quard/cache/pytest-testmon --testmon
```

Run the full `unittest` suite and `check.py` before finishing tool changes.
Testmon tracks executed Python code, not changes to static files or external
services. Its incremental result is not a full-suite result.

To profile the test suite with the pinned `pytest-durations` plugin:

```bash
uv run --locked --group dev python -B -m pytest tests -q -p no:tach -p no:cacheprovider \
  --pytest-durations=30
```

`-p no:tach` avoids the separate Tach pytest plugin, which cannot initialize
against this checkout's generated architecture; the normal `check.py` command
still runs the native Tach job. Pytest collects test files that the `unittest`
discovery hook does not include, so compare their test counts when interpreting
the profile. `-p no:cacheprovider` keeps pytest from writing a cache into the
checkout, which the source-install cleanliness test rejects. Testmon needs
the cache provider; its command above puts that cache under `.quard/cache/`.

Quard's implementation lives in `src/quard/`. Root Python commands are consumer
entry points. The checkout has generated
architecture under `.quard/`, so a full check from here analyzes its production modules
and tests; the resulting report is in this checkout's `.quard/reports/latest.json`.
Consumer checks still write reports and caches only inside the selected consumer.

CI runs offline orchestration tests and real compatibility regressions on isolated
public fixtures; `LifecycleTests` is always skipped. Access to `tai` is not needed.
Quard's lockfile fixes its test environment; consumer lockfiles fix theirs.

Profile/limits: [PROFILE.md](PROFILE.md). Compatibility rationale and provenance:
[COMPATIBILITY.md](COMPATIBILITY.md). Agent instructions: [AGENTS.md](AGENTS.md).
