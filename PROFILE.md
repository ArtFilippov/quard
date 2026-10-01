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
* **LCOM4**: Eryx's basic AST-based class cohesion check, run as its own job.
* **Non-instance methods**: Eryx's source-positioned `NON_INSTANCE_METHOD` rule rejects
  built-in `@staticmethod` and `@classmethod` declarations and recognizable conversions
  on consumer-owned classes, independently of the native analyzers and LCOM4.
  `MISSING_INSTANCE_RECEIVER` rejects plain class-body methods without a mandatory
  positional instance receiver, regardless of its name or use in the body.
* **Class contracts**: `MISSING_CLASS_CONTRACT` requires each customer class to
  explicitly inherit a contract. Local Protocol and abstract-interface declarations
  qualify; implementations may inherit these or an opaque external base (including
  an explicit `object`). Implicit `object` does not qualify. This independent job
  scans discovered production, test and stub files even when LCOM4 is narrowed
  or skipped. It resolves customer ancestry across discovered modules, imported
  aliases and re-exports (including stub declarations) without inspecting
  external source. Unknown bare names and unresolved customer bases do not
  prove a contract; explicit imports outside customer package roots remain
  opaque external bases. Source failures and malformed worker reports are
  infrastructure errors.
  `UNCOVERED_CLASS_MEMBER` points to each public instance method or property
  declared on a customer implementation class that no inherited local Protocol or
  abstract-interface declaration names. Multiple local contracts may cover
  different members; contracts through customer ancestors, aliases and stubs
  count. Decorated and statically assigned built-in or `functools.cached_property`
  properties count; Python data-model hooks, private members, inherited methods
  and non-instance methods do not require fresh declarations. The check compares
  names, leaving signatures to Pyright. A proven explicitly inherited opaque
  external base (directly or via customer ancestry) may supply otherwise uncovered
  behavior, so the entire class is exempt from member coverage; its source is never
  inspected. Unknown and unresolved customer bases do not grant this exemption.
  Explicit `object` still satisfies the inheritance requirement but supplies no
  public API exemption. This trade-off permits extra local methods on classes that also
  inherit an external framework base.
* **Instance fields**: `EXTERNAL_INSTANCE_FIELD` rejects proven direct reads,
  assignments and deletions of consumer-defined stored fields outside the declaring
  class, in production and tests, including literal-name builtin attribute calls and
  known keys in `vars(obj)`/`obj.__dict__` accesses. A first assignment to an undeclared
  field on a known consumer instance is also rejected. Same-class methods may access
  another instance's fields; properties and methods remain available to callers,
  including mutable values returned by properties.
* **Class state**: `NO_CLASS_STATE` rejects stored class values at their declarations,
  including immutable `Final` constants, and statically named writes through known
  consumer classes after creation (direct, builtin `setattr`, or class dictionary).
  `EXTERNAL_CLASS_FIELD` also rejects proven external reads,
  writes and deletes of declared class values through either the class or an instance,
  including literal-name builtin calls and class dictionary access.
  Initialized dataclass instance fields, Enum members and `__slots__` declarations
  remain allowed.

Production is `src/`; tests are `tests/` and the test directory basenames configured
in `.eryx/scope.toml`. Exclusions match **exact directory basenames**, not substrings:
`generated_models` is not `generated`. Discovered excluded directories appear in
reports. `.pyi` files are checked by Pyright, both method rules and class contracts;
other tools use their native scope. `__init__.py` is included. Root scripts and
notebooks are outside the consumer's source scope. In the Eryx checkout, its own
`src/eryx/` and `tests/` are analyzed; its autonomous test suite verifies execution.
`check.py --lcom_files` narrows only LCOM4 to explicitly selected, existing `.py`
files in that scope; `--lcom_files ""` skips LCOM4. Without the option, full
LCOM4 coverage is retained. Other analyzers always use their normal scopes.

The method job always scans discovered production and test `.py` and
`.pyi` files, even with `--lcom_files ""` or a selected LCOM4 subset. It checks
directly declared sync and async methods in nested, Protocol and abstract classes.
Plain methods (including abstract declarations) need a mandatory first positional
parameter: keyword-only, variadic-only and defaulted positions do not qualify.
Python's implicitly class-bound undecorated `__new__`, `__init_subclass__` and
`__class_getitem__` are exempt. Other decorators may alter binding, so their
undecorated receiver signature is not inferred; recognizable non-instance
decorators and conversions remain violations in their own right.
Built-in names, explicit `builtins` imports and their aliases are recognized;
shadowed names and unrelated qualified decorators are not treated as built-ins.
Properties, abstract instance methods, inherited methods and undecorated Python
hooks are unaffected. It also recognizes `staticmethod(function)` and
`classmethod(function)` assigned in a class body or to a known consumer class
attribute after creation, and `setattr(KnownClass, "name", conversion)`.
Module/class/function-local aliases, explicit `builtins` references and class
aliases are tracked; the converted value must be a statically known function
definition, class method reference or lambda. Inherited library methods, unknown
functions, computed attribute names, decorated or dynamic class owners and unprovable
runtime-generated conversions are outside this syntactic guarantee. Each
violation identifies its class attribute and points to the decorator or conversion
call. Source read/parse errors and invalid worker output are infrastructure
errors, not silently skipped files.

The instance-field job independently scans those discovered `.py` and `.pyi` files,
including when LCOM4 is narrowed or skipped. It recognizes receiver assignments
inside instance methods, class-body annotations, literal `__slots__`, explicit
standard-library dataclass decorators, local constructor bindings, explicit
consumer imports, static class aliases and unambiguous class annotations (including
when runtime inference is unavailable). Fields inherited through resolvable
consumer bases retain their declaring owner; subclass and nested-class methods
have only their own class's access privilege. Discovered `.pyi` declarations also
establish fields and types. Ambiguous bases, conflicting or union annotations,
unknown or third-party owners, dynamic attributes and unresolved flow are not
inferred. Literal-name builtin `getattr`, `setattr`, `delattr` (including explicit
`builtins` imports) and literal-key subscripts or dictionary methods on `vars(obj)`
and `obj.__dict__` use the same owner resolution. Instance dictionaries do not expose
class fields. Calls to arbitrary methods/constructors do not establish fields.
Source/worker/report failures are infrastructure errors.
See [ADR 0002](docs/adr/0002-instance-field-boundaries.md).

Class assignments and initialized class annotations establish prohibited class state;
receiver assignments, slots, uninitialized annotations and annotated dataclass defaults
establish instance fields (except explicit `ClassVar`). Plain methods, properties and
explicitly imported `functools.cached_property` are public interfaces; stored callables
and unrecognized descriptors are fields. Enum members from explicitly imported standard
Enum bases are exempt. The rule is syntactic: dynamic owners and computed attribute
names cannot be proved. Same-class reads of prohibited declarations are not separately
reported.

## Basic LCOM4

Every eligible concrete class in discovered production and test `.py` files is checked,
including fixtures, data-only classes and concrete ABCs; `.pyi` stubs are skipped.
Protocol declarations and ABCs with outstanding abstract methods are skipped.
Concrete subclasses of Protocol declarations are checked. Abstract status follows
statically identifiable local inheritance and explicit `abc`/`typing` imports;
unresolved third-party abstract bases do not exempt a class. An abstractmethod
decorator outside an ABC does not by itself exempt a class.

An eligible method is a function or async function declared directly in the class
body, excluding Python-recognized special methods (not arbitrary user-defined
dunders), `typing`/`typing_extensions` overload signatures, methods with `abc`
abstract decorators even with substantive bodies, and no-op methods. No-op means `pass`,
`...`, a docstring alone, `return None`, or `raise NotImplementedError` (including
an instance), with an optional leading docstring. Individual substantive property
getters, setters and deleters remain distinct vertices. No separate diagnostic is
issued for excluded methods; a concrete class with none scores 0 and fails.
The score is the number of connected method groups. Direct calls through the bound receiver
or lexical class name connect to methods declared in that class. Access to the same
direct instance field connects methods, as does access to the same direct class
field; instance and class fields remain distinct even when their names match.
The first positional parameter is the instance receiver (whatever its name), or
the class receiver for a directly decorated `@classmethod`. Directly decorated
`@staticmethod` functions have no bound receiver, but can access fields through
the class name. Property access alone does not create a getter/setter call edge;
accessors with the same name are distinct vertices. Comprehensions in method bodies
contribute references; nested function, lambda and class bodies do not. Nested
classes are scored independently. A class with no eligible methods scores 0; a
connected class scores 1. Scores other than 1 produce class-location diagnostics with the
score and disconnected groups, without a baseline or exemptions.

This pass is syntactic: it does not resolve receiver aliases, external inheritance, indirect
calls, dynamic attributes (`getattr`), decorators beyond explicit `abc`/`typing`
imports, or runtime dispatch.
An undeclared receiver call is not assumed to be a field access (it may be an
inherited method); callable fields invoked this way do not create field edges.
Methods with the same name are treated as distinct declarations. Parser and worker
failures are infrastructure errors (exit 2), not passing results.

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
