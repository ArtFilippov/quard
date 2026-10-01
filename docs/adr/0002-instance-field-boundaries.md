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
