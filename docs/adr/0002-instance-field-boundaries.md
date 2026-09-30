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

This favors encapsulation: callers use behavior or properties instead of coupling
themselves to an object's storage layout. The cost is extra accessors for plain
data containers, even when direct field access would be simpler. The rule is
deliberately conservative rather than guessing ownership from field names.
