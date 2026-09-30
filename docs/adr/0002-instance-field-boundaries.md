# Keep stored instance fields behind their owner

Eryx rejects a proven direct read, assignment or deletion of a consumer-defined
stored instance field outside the class that declares it, in production and tests.
Methods of that class may access fields on any of its instances. Properties and
methods are public behavior, not stored fields. The check recognizes fields
assigned through a method receiver or declared in the class body (including
annotations, literal slots and dataclass fields), including declarations in
discovered stubs. It also catches first assignments to new fields on proven
consumer instances, and resolves explicit consumer class imports, static aliases,
unambiguous annotations and constructor-created objects. Inherited fields
belong to their declaring class: subclasses and nested classes are outside it.
Unknown, ambiguous and third-party owners do not trigger this rule.

Class-level stored values also belong to their declaring class, even when named in
uppercase or accessed through an instance. Direct external reads, writes and deletes
are rejected. A class value explicitly declared `typing.Final` (or
`typing_extensions.Final`) and initialized with a statically immutable literal may
be read externally, but cannot be assigned or deleted. A mutable Final value, an
unknown expression and a Final instance field remain protected. Methods, properties
and recognized cached-property descriptors are public interfaces; storing a callable
or an unrecognized descriptor does not turn a field into one.

This favors encapsulation: callers use behavior or properties instead of coupling
themselves to an object's storage layout. The cost is extra accessors for plain
data containers, even when direct field access would be simpler. The rule is
deliberately conservative rather than guessing ownership from field names.
