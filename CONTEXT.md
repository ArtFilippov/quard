# Quard domain context

## Required Pylint profile

The **required Pylint profile** is the always-on production and test analysis
configured by Quard. Its native diagnostics remain active regardless of optional
check selection. Consumer-owned native options are retained when settings are applied.

## Selectable check

A **selectable check** is an independent Quard job that a consumer can enable in
`.quard/config.toml` under `[checks]`. The three clone jobs are selected together
by `duplicate_detection`; other independent jobs have their own switches.
`constructor_purity` is enabled by default and can be explicitly disabled.
`stairfmt` is likewise default-on for consumers but explicitly disabled in the
Quard checkout while its source layout is migrated. Its separate fix command
uses the discovered scope and writes a fix report without replacing the check report.
Disabled jobs are reported as skipped, not passed; the required Pylint profile
remains independent of these switches.

## Source scope

**Source scope** is the discovered production and test Python files beneath
`src/` and `tests/`, classified and excluded by exact directory basenames in
`[scope]`. It applies to the native tools and independent jobs, regardless of
optional check selection or per-change LCOM4 selection.

## In-memory source session

An **in-memory source session** owns Quard's discovered original Python source
text and the pinned native engine's prepared token sources for a check run.
The session does not execute or import consumer code or create prepared files.
The pinned source-built Rust session parses each file once for all three clone
jobs, then tokenizes eligible units for exact, renamed and gap-similar detection.
Older gitlinks retain their own historical CLI behavior. The updated Quard
requires a source pin in consumers; without it, check fails with exit 2 and
points to the migration guide. The Quard checkout keeps the CLI in dev for
benchmarking and its own full check.

## Prepared view and eligible material

A **prepared view** is the in-memory sequence of eligible original source
lines in one module, class or function unit. File-level and class-level imports,
actual docstrings, attribute documentation, ordinary definition headers,
decorator syntax, annotation-only fields and type aliases do not contribute.
Type aliases recognized here use `type Name = ...` or an annotation with
`typing.TypeAlias` / `typing_extensions.TypeAlias` through an explicit import,
including aliases. Function-local imports, control flow and value assignments,
including assigned strings and annotated values, do contribute. Ordinary
standalone `pass`, `...` and bare or literal-argument `raise NotImplementedError`
are placeholders, even alongside a docstring, and do not contribute to the
shared behavior view. Other real statements, including `return None`, executable
decorator implementations and real contract/special-method bodies remain eligible.
The exact view also contains consecutive class-based and supported functional
TypedDict fields, keeping each schema separate; functional fields need one
unambiguous original line apiece. Field names and types must match exactly.
Exact contract-declaration units include ordered signatures (including
supported multiline signatures) and eligible placeholder lines of local
Protocol and abstract-interface methods. Property declarations use the public
property name and accessor kind.
Contract names and special double-underscore declarations do not count. Its
placeholders can satisfy the physical line minimum without entering behavior;
executable contract and special-method bodies remain behavioral. TypedDict
fields and contract declarations never enter the renamed/similar behavior
view. Each clone job retains its own native kind and report.
The effective clone minimum counts **eligible material** within a unit. Removing lines does not
allow neighboring independent classes or functions to form one clone.

## Source mapping

A **source mapping** connects each prepared line and native token position to
its original file, line and byte column. The archived clone fragment uses the
verbatim original range, even when that range contains omitted documentation.
The native report records the discovered paths, their source digests,
eligible-line totals and per-unit lengths; a view without any qualifying unit
is explicitly skipped. A changed original or invalid mapping is an error.

## Customer class

A **customer class** is a class declared in a consumer's discovered production or
test `.py` or `.pyi` scope, including nested and function-local declarations.
Its explicitly inherited contract describes the interface it implements or the
external type it extends. A locally declared Protocol or abstract interface is
itself a contract declaration. An external base is opaque: Quard does not inspect
its source. An implicit inheritance from `object` is not an explicit contract;
writing `class Name(object)` is explicit.

## Customer method override

A **customer method override** is a method or recognized property declared directly
on a customer class with the same name as a method or recognized property declared
by any reachable customer-owned ancestor that is not a contract declaration. A
contract or another base preceding that ancestor does not erase its ownership.
The member name identifies the override: a setter can replace a getter, and a
method can replace a property. A getter/setter pair is one member per class.
Local Protocol and abstract-interface declarations are contracts, even when a
method has a body; they supply overridable members, not forbidden implementations.
An external or unresolved base cannot establish a customer method override.

## Override-marked method

An **override-marked method** is a directly declared customer class method with
a proven `typing.override` or `typing_extensions.override` decorator, including
imported aliases. The optional leading-underscore job exempts its method name,
not its parameters, fields or path, without resolving external member declarations.
Each property accessor and source or stub declaration is considered separately.
Pyright independently validates the override claim during a full check; an
unmarked method does not receive this naming exception. A customer method override
remains a separate, customer-owned ancestry finding.

## Contract-facing method

A **contract-facing method** is a customer method whose name is declared by a
local Protocol or abstract interface reachable through its class's explicitly
inherited, resolvable bases. A method on an unrelated contract with the same name
does not qualify. An implementation-only method is not contract-facing even when
its name lacks a leading underscore; see [ADR 0006](docs/adr/0006-inherited-contract-return-boundaries.md).

## Restricted member

A **restricted member** is a method or property whose invocation or access is
reserved for the current instance, its directly stored dependencies, or its
current-instance superclass behavior. Restriction concerns the access boundary,
not whether the member name is public or private.

## Declared restriction

A **declared restriction** is the restriction expressed by a member's own marking
or its declaring class's marking. A newly declared implementation has its own
declared restriction, distinct from the requirement imposed by an inherited contract.

## Effective restriction

An **effective restriction** is a member's restriction after declaration, class
marking, inherited members and inherited contract requirements are considered.
Conflicting declarations do not erase an established restriction.

## Assembly-site lambda injection

An **assembly site** is a discovered production module beneath `main/` or anywhere
in a contract group's `envelopes/` subtree. **Lambda injection** is a
lambda supplied to a proven project-class or External call there, directly or through
local aliases, branch alternatives, enclosing-function captures or simple containers.
Its body, nested lambdas and definition-time defaults must use the supported
declarative expression shapes; forbidden shapes point to the expression and suggest
a wrapper class. **Injected value provenance** is the separate proof that each
argument source comes from a parameter, local assembly or an explicit External
value. An unresolved branch or project global read inside a function supplies no
local proof and receives an origin diagnostic at the argument or alias.
