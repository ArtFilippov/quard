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
* **Constructor purity**: the independent `CONSTRUCTOR_PURITY` rule checks eager
  constructor syntax in discovered production and test `.py` files, including
  nested classes. It is unaffected by `--lcom_files`; stubs have no checked body.
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
  `CUSTOMER_METHOD_OVERRIDE` reports a directly declared sync or async method or
  recognized property that redeclares a member on any resolvable customer-owned
  non-contract ancestor, even when a contract or other base comes first in MRO.
  Imported aliases, re-exports, nested classes and discovered stubs resolve as
  customer ancestry. A stub-only concrete ancestor establishes its methods and
  properties; when source and stub declare the same ancestor, source establishes
  its ancestry and members. Each descendant source and stub is checked separately.
  Contract declarations (local Protocols and abstract interfaces) may provide
  overridable members with or without bodies; another inherited contract naming
  the same member does not authorize overriding a concrete customer member.
  Matching is by name: a setter alone can override a getter, and a method can
  replace a property (or vice versa). Decorated properties and assignments using
  built-in `property` or `functools.cached_property` count, but assigned callables
  do not. A getter/setter pair receives one violation at its first declaration
  in each class and source file. Ordinary private members and built-in static/class
  methods count. Python-recognized data-model hooks and name-mangled `__private`
  methods are exempt; arbitrary `__custom__` methods are not. External and
  unresolved ancestry provides no proof of a customer-owned implementation.
  This preserves a single owner for implemented behavior while allowing explicit
  interface implementation. Overrides are normal source-positioned violations
  (exit 1); parse and report failures remain errors (exit 2) without suppressing
  other diagnostics.
* **Instance fields**: `EXTERNAL_INSTANCE_FIELD` rejects proven direct reads,
  assignments and deletions of consumer-defined stored fields outside the declaring
  class, in production and tests, including literal-name builtin attribute calls and
  known keys in `vars(obj)`/`obj.__dict__` accesses. A first assignment to an undeclared
  field on a known consumer instance is also rejected. Same-class methods may access
  another instance's fields; properties and methods remain available to callers,
  including mutable values returned by properties.
  `MISSING_FIELD_INITIALIZATION` reports a stored receiver field at its first
  declaration (annotation or literal slot) or write unless every normal-return
  path through its owning `__init__` directly assigns it. Source Protocols and
  abstract classes follow the same rule. Standard dataclass-generated constructors
  count only fields they assign: `field(init=False)` requires initialization
  unless it has a default factory, or a default with `slots=True`. Literal-name
  built-in `setattr` and known-key `vars(self)`/`self.__dict__` writes through
  the receiver or a proven local alias also count on every normal-return path. `init=False`
  decorators and custom constructors follow the ordinary constructor rule.
  `ClassVar`, `InitVar` and `KW_ONLY` are not stored dataclass fields. Stub-only
  declarations establish ownership and type without requiring a constructor.
  Alternative branches, early returns, matches and exception handlers are
  considered; paths that raise do not create usable instances. Loop-only writes
  cannot prove initialization, and deferred nested bodies, helper calls and
  computed names and unproven indirect writes do not count.
  `UNKNOWN_FIELD_TYPE` reports an unresolvable or conflicting stored-field type
  at its declaration or first receiver write. Exact static field annotations take
  priority over runtime values (including subclasses); absent annotations, literal
  values and resolved return annotations provide evidence. Conflicting source/stub
  annotations and unknown expressions without a qualifying annotation are violations.
  `NON_PRIMITIVE_FIELD_REBINDING` reports a recognized receiver reassignment that can
  follow an earlier write, including successive writes in a constructor or loop.
  Only exact resolved `bool`, `int`, `float`, `complex`, `str` and `bytes` field
  types may be rebound after initialization. `NoneType`, unions, subclasses and
  other known types may not. `DELETED_INSTANCE_FIELD` reports deletion of a bound
  receiver field, including literal-name builtin and instance-dictionary deletion.
  `+=` is allowed after initialization for every type and never establishes the
  initial binding. Mutating the existing object is not rebinding; a
  primitive first assigned outside the owner constructor still fails initialization.
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

## Encapsulated-only explicit calls and property access

The `encapsulated-only` job scans every discovered production and test `.py` and
`.pyi` file independently of LCOM4 selection. `ENCAPSULATED_ONLY_CALL` identifies
the qualified dispatch-owner method at the explicit call's one-based position;
`ENCAPSULATED_ONLY_PROPERTY` identifies a restricted property at the attribute or
literal-name builtin operation's position.
Consumer source is parsed, never imported or executed. Raw worker output retains
separate method/property-accessor/class evidence for source, companion stubs and overload
signatures, plus effective restricted targets and marking origins. The normalized
report uses the ordinary report schema and archived raw logs.

Consumers supply the marker; Eryx has no runtime decorator API. Use a generic
identity function so Pyright retains class identity, callable signatures and binding:

```python
def encapsulated_only[T](value: T) -> T:
    return value
```

For Python 3.12+, this works for methods and classes. A stub may declare the same
signature with `...`. Source recognition requires a synchronous function named
`encapsulated_only` whose only executable statement returns its sole positional
argument (an optional docstring is allowed). Explicit imports of that named marker
from opaque external modules use the same identity convention; Eryx cannot verify
their runtime implementation. Local bindings, imported module references, simple
aliases and consumer re-exports resolve at declaration time. Shadowed, reassigned,
conditional and unresolved customer bindings do not establish the marker. A name
or attribute spelling alone is insufficient. Recognized markers preserve method
receiver checks, field/method classification and decorated class identity in the
existing Eryx workers; they grant no exemption from native or other Eryx diagnostics.

An explicit restricted invocation qualifies only through the proven current
method's first positional instance receiver (regardless of its spelling), an
unreassigned alias of that instance, one direct stored field of such a receiver,
or proven builtin `super()` / `super(CurrentClass, receiver)`. Ordinary fields,
literal slots and standard dataclass instance fields qualify. Aliases of stored
fields, properties, cached properties and class values do not confer permission.
Taking a method reference is permitted, but calling an extracted restricted
callable or a class-qualified unbound method is prohibited. Literal-name builtin
`getattr` invocation (including proven aliases and explicit imports) follows the
direct-call boundary; lexical shadowing and reassignment remove builtin evidence.
Nested functions retain proven enclosing-instance aliases, whereas nested classes
do not inherit that permission. Established calls through peers, constructor
results, property results, class values and deeper field chains are violations.
Class marking restricts declared and inherited methods,
including those introduced by an unmarked ancestor. Names beginning and ending in
`__` are excluded from class-wide marking unless explicitly marked on the method.
An unmarked descendant keeps inherited restrictions without restricting new methods.
Resolvable customer bases use conservative C3 dispatch order; an opaque or unresolved
base preceding the candidate prevents a guessed target. Source declarations establish
ancestry and members when a companion stub exists; separate source/stub marker
evidence remains available. Explicitly inherited marked Protocol or abstract contract
members constrain implementations without requiring a redeclaration of inherited
concrete methods. Structural resemblance alone does not propagate Protocol marking;
calls through a marked Protocol-typed receiver still use the Protocol declaration.
Mutable objects returned by ordinary properties remain subject to the independent
field and contract policies.

Targets resolve through explicit customer imports, class aliases, re-exports,
unambiguous class annotations and straightforward constructor bindings. Stored
field types use exact annotations or consistent initializer evidence, including
annotated constructor parameters. Property/method return annotations can establish
the type of a produced receiver without granting it permission. Builtin super
identity respects imports, aliases and shadowing; proven `super()` calls resolve one
direct customer base, without guessing multiple-inheritance super dispatch.
Unknown, conflicting or dynamic evidence alone is not a violation. Conditional
object and callable bindings retain established restricted alternatives; a possible
restricted target is sufficient even alongside unknown or unrestricted alternatives.
Straightforward assignments, if/else branches, loops and exception handlers
conservatively invalidate receiver proof on reassignment. This is syntactic
provenance, not general interprocedural alias or dynamically computed-name analysis.

Proven builtin calls also check the following dispatch (positional arguments only):

| Helper | Statically checked receiver protocol |
|---|---|
| `len(x)` | `x.__len__` |
| `str(x)` | `x.__str__`, or `x.__repr__` via the default string conversion when `__str__` is absent |
| `repr(x)`, `ascii(x)` | `x.__repr__` |
| `format(x[, spec])` | `x.__format__`; when absent, empty-spec default formatting calls `__str__` (or its `__repr__` fallback) |
| `int(x)`, `float(x)` | `__int__` / `__float__`, otherwise `__index__` if the preceding slot is proven absent |
| `complex(x)` | `__complex__`, otherwise `__float__`, otherwise `__index__` if earlier slots are proven absent |
| `bin(x)`, `oct(x)`, `hex(x)` | `x.__index__` |
| `abs(x)`, `round(x[, ndigits])`, `hash(x)` | `__abs__`, `__round__`, `__hash__`, respectively |
| `iter(x)` | `x.__iter__` |
| `next(iterator[, default])` | `iterator.__next__` |
| `list(x)`, `tuple(x)` | `x.__len__` when declared, `x.__iter__`, then the returned iterator's `__next__` |
| `any(x)`, `all(x)` | `x.__iter__`, the returned iterator's `__next__`, then truth testing of a typed yielded value |

These forms require an unshadowed builtin or a proven explicit `builtins` import,
qualified reference or binding alias. Lexical shadowing and reassignment remove
the corresponding proof. Each protocol receiver keeps its own origin: a generated
iterator is not privileged, whereas a proven direct `return self` preserves the
original iterator's identity. Conditional restricted targets count; an unknown
target alone does not. Conversion fallbacks require proven absence of the earlier
slot, and a known override excludes an inherited fallback. Positional diagnostics
use `ENCAPSULATED_ONLY_CALL` at the helper call. Only these proven slots are
checked: `iter(callable, sentinel)`, `reversed`, `bytes`, keyword forms, iterator
length hints, builtins' internal optimizations and arbitrary callback/library
dispatch are outside this matrix. It does not claim universal runtime coverage.

The job restricts directly marked and class/inheritance-restricted explicit method
calls and ordinary/cached property access, including literal-name reflection. A marker
on any recognized getter, setter or deleter restricts all three property operations,
even after a cached value is stored. Proven `@property`, `@functools.cached_property`,
accessor aliases and static descriptor constructors count; shadowed lookalikes do not.
Property results never grant stored-field permission. Implicit dispatch checks
statically resolved `__new__`/`__init__` on construction, `__call__` on callable
instances, `__getitem__`/`__setitem__`/`__delitem__` on subscriptions, and
`__getattribute__`/`__getattr__`/`__setattr__`/`__delattr__` on instance attribute
syntax and literal-name builtin operations. The new construction receiver is never
privileged. Protocol methods resolve through the receiver's type, not an instance
attribute; a known class override can exclude an inherited restriction.
`__getattr__` is checked when default lookup cannot prove the attribute exists, or
when a custom `__getattribute__` may raise `AttributeError`. Each proven
invocation is reported once at the operation, even with multiple receiver origins.
A typed subscription result may establish a subsequent protocol target but does not
inherit permission from the subscribed receiver. Dynamic metaclasses, descriptor
behavior beyond recognized properties, computed attribute names and unproven types
remain outside this syntactic inference. Synchronous and asynchronous `with` check
proven entry and exit hooks at each manager expression, including multiple managers;
exit uses the manager, not its `as` value. `for`/`async for`, list/set/dict
comprehensions and generator expressions check proven `__iter__`/`__aiter__`
acquisition and `__next__`/`__anext__` advancement at each iterable expression.
`await` checks proven `__await__` and `__next__` on its returned iterator. These
checks also apply inside nested functions; comprehension targets shadow only within
their own scope. Inherited dispatch, aliases, conditional alternatives and stored-field
permissions follow explicit calls. Return annotations (including stubs and inherited
methods) or a single unannotated direct constructor return establish intermediate
types. A single direct `return self` proves that an iterator is the original receiver;
otherwise acquisition does not pass its permission to advancement. Resolved `with as`
and loop/comprehension values keep their type but do not inherit permission. A possible
restricted intermediate target fails even if another branch is unknown. Without a
proven intermediate type, advancement is unresolved. Runtime-inferred return types,
coroutine machinery around async hooks,
awaitable objects returned by `__anext__` and `__aenter__`, and operations within
unresolved external iterators are not inferred. Unary `+`, `-`, `~`, all binary
arithmetic/bitwise operators and their augmented forms check type-level normal,
reflected and in-place methods. Rich comparisons (`<`, `<=`, `==`, `!=`, `>`, `>=`)
check both dispatch sides; the default `__ne__` delegates to `__eq__` (and tests
its result) when its absence is proven. Identity comparisons do not call methods. A distinct
subclass's overriding reflected method runs first. Same-type operands do not call
reflected methods twice. In-place dispatch falls back to ordinary binary dispatch;
a direct, unshadowed `return NotImplemented` establishes fallback, while an
unambiguous direct literal or receiver return proves a later target unreachable.
Other return paths retain possible fallback targets, including marked methods
mixed with unresolved results. A reflected target requires established types for
both operands; unresolved operands alone supply no dispatch proof.

Membership checks `__contains__` on the container; when absent, it checks
`__iter__` and the produced iterator's `__next__`, or indexed `__getitem__`
if iteration is absent. Known element types also check equality comparison
with the needle. A typed containment result undergoes truth testing. Augmented
subscriptions check both the read and write hooks and the element's operator.
Conditionals, assertions, boolean expressions, `not`, comprehension filters,
match guards and builtin `bool(x)` check `__bool__`, falling back to `__len__`
only when `__bool__` is absent. Chained comparisons test intermediate results.
Known operator return types can establish later protocol targets, but their
produced objects do not inherit operand permission. Receiver origins for each
operand are checked independently. No claim is made about unknown runtime
results, dynamic slot replacement, external operator implementations or
arbitrary expression/type inference. Other implicit protocols remain later
slices of [ADR 0003](docs/adr/0003-encapsulated-only-members.md).
`ENCAPSULATED_ONLY_DECLARATION` reports inconsistent marking on newly declared
implementations of explicitly inherited Protocol or abstract contracts, conflicting
inherited contract requirements, companion source/stub members and overload groups.
The implementation's own member or class marker must match each contract; inheriting
a marked requirement does not count as marking a new declaration. Inherited concrete
members need no redeclaration, and structural Protocol resemblance creates no
obligation. Class and member markers (including resolved aliases) are equivalent;
any marked property accessor restricts the entire property, including cached
properties. Agreement violations do not weaken established access restrictions.
This does not claim universal runtime coverage. Source read/parse errors, execution failures,
timeouts and invalid/inconsistent reports are infrastructure errors (exit 2);
valid clean output exits 0 and forbidden calls exit 1. Other jobs finish and retain
their diagnostics. Ordinary reports, manifests and logs stay in the chosen consumer.

## Constructor purity

The `constructor-purity` job examines every directly declared `__init__` body in
discovered production and test `.py` files, including nested classes. Excluded
directories and stub-only files have no checked bodies. The receiver is the first
mandatory positional instance argument, whatever its name; its aliases do not
qualify. Local names and direct receiver attributes are the only permitted
assignment targets, including for annotations, augmented, chained, starred and
unpacked assignments. Rebinding the receiver is prohibited. Match captures may
not rebind it either.

Constructors may have an initial docstring, assignments, if/elif/else, match/case,
raise, assert and pass. Constants, names, attributes, subscriptions/slices,
containers/unpacking, operators, comparisons, conditional expressions and f-strings
recursively check their eager children. Lambdas and nested function definitions
defer their bodies (including async bodies and lambda expressions); their defaults,
annotations and decorator expressions are checked at definition time, even with
`from __future__ import annotations`. A proven project decorator's application is
also eager: its statements follow the constructor whitelist, except that it may
declare nested deferred functions and return a function. Project decorator
factories check both the factory call and the returned decorator's application;
project callable instances check their `__call__` application. Applying a proven
project class constructs it; its constructor is checked independently. A returned lambda
used as a decorator is checked when applied, not when declared. Each eager stage
checks its own calls and nested arguments; unresolved application fails closed.
Proven external decorators and external decorator factories are opaque, while their
eager expression arguments still follow the whitelist. Async decorator bodies stay
deferred on invocation. Project decorator diagnostics point to the offending source
node (which may be in another project module) and retain the constructor symbol.
Explicit returns, loops, comprehensions, try/with,
imports, deletion, global/nonlocal declarations, nested classes, assignment
expressions, await, yield and otherwise unknown eager AST forms are rejected.
Standalone calls are allowed only for `super().__init__(...)`,
`super(CurrentClass, receiver).__init__(...)`, and a declared direct base's
`Base.__init__(receiver, ...)`. Shadowed `super` and incorrectly bound receivers
do not qualify.

Calls inside permitted expressions may target a statically established project
class, builtin type, or validation builtin (`isinstance`, `issubclass`, `hasattr`,
`callable`). Simple aliases, explicit imports, customer re-exports and if/match
branch joins preserve origins; **every** possible target must qualify. A project
helper/factory or unknown alternative disqualifies the call. Shadowed builtin
names, including `super`, have no inherited privilege. Explicit external imports
and receivers with exact external-type annotations establish an opaque call
boundary; unknown imports and untyped receivers do not. Calls through external
modules and imported symbols are opaque regardless of their unknown callable kind.
Eager arguments still follow the same whitelist. Ordinary non-exempt builtin
functions remain denied. No external source, stub or runtime code is consulted;
these syntactic origins do not prove runtime purity or decorator behavior. See
[ADR 0004](docs/adr/0004-constructor-external-provenance.md).
Each forbidden node has its own one-based location and `CONSTRUCTOR_PURITY`
diagnostic. Parse/read/worker failures are infrastructure errors (exit 2),
while violations exit 1; raw output is archived and other jobs remain independent.

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
