# Keep stored instance fields behind their owner

Eryx rejects a proven direct read, assignment or deletion of a consumer-defined
stored instance field outside the class that declares it, in production and tests.
This also covers literal-name built-in `getattr`, `setattr` and `delattr` calls,
and literal-key access through a proven instance's `__dict__` or `vars` result,
including dictionary reads, writes and deletions. The same boundary applies to
known class fields through a class dictionary; new class writes remain prohibited.
Methods of that class may access fields on any of its instances. Properties and
methods are public behavior, not stored fields. The check recognizes fields
assigned through a method receiver or declared in the class body (including
annotations, literal slots and dataclass fields), including declarations in
discovered stubs. It also catches first assignments to new fields on proven
consumer instances, and resolves explicit consumer class imports, static aliases,
unambiguous annotations and constructor-created objects. Inherited fields
belong to their declaring class: subclasses and nested classes are outside it.
Unknown, ambiguous and third-party owners do not trigger this rule.
Computed names and fields supposedly produced by arbitrary method or constructor
calls are not inferred. A property returning a mutable value remains public even
when callers mutate that value.

Source receiver fields, annotations and literal slots must be initialized by the
owning constructor on every normal-return path, including in Protocol and abstract
classes. A standard dataclass-generated constructor counts only assignments it
actually makes; a custom constructor follows the ordinary rule. Stub-only
declarations establish ownership without requiring executable initialization.
Alternative branches count independently; an always-raising path creates no
instance. Loop-only writes, deferred nested functions and lambdas, helper calls
and indirect writes cannot prove initialization.
Every stored field needs a single resolved type. An exact static annotation of
`bool`, `int`, `float`, `complex`, `str` or `bytes` permits direct reassignment
after initialization, even if the runtime value is a subclass. A subclass
annotation, union, `NoneType` or any other known type does not. Unannotated
fields use reliable assignments such as literals and resolved return annotations;
unknown expressions or conflicting source/stub evidence yield
`UNKNOWN_FIELD_TYPE` at the declaration or first write. For a known non-primitive
type, `NON_PRIMITIVE_FIELD_REBINDING` points to the later direct receiver write,
even inside the constructor. Alternative first writes in exclusive branches do
not count as rebinding; possible repeated loop writes do. Mutating the object
already held in a field leaves its binding intact. A primitive written for the
first time outside its owner constructor still violates initialization.

Stored class values are prohibited at declaration, regardless of their name, value
or `Final` annotation. Direct writes and built-in `setattr` calls with literal names
through a known consumer class after creation are prohibited too. Proven external
reads, writes and deletes of declared class values remain violations, including
accesses through an instance. Initialized
dataclass instance fields, Enum members and `__slots__` declarations are not class
state. Methods, properties and recognized cached-property descriptors are public
interfaces; storing a callable or an unrecognized descriptor does not turn a field
into one.

This favors encapsulation: callers use behavior or properties instead of coupling
themselves to an object's storage layout. The cost is extra accessors for plain
data containers, even when direct field access would be simpler. The rule is
deliberately conservative rather than guessing ownership from field names.
