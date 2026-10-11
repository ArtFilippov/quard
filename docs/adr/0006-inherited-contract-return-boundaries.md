# Use inherited contracts for return boundaries

Reviewed W9007 treats a method as contract-facing only when a resolvable, explicitly
inherited local Protocol or abstract interface declares that member. A renamed helper,
an unrelated contract with the same method name, an unknown base, and a class name
ending in `Repository` do not establish a contract boundary. This keeps Bootstrap
composition and annotated infrastructure helpers possible while continuing to check
real contract returns (including methods named with `_`) for raw and infrastructure
values. We prefer readable implementation names over Python's conventional `_private`
signal: privacy here is determined by inherited API ownership, not spelling. The
optional leading-underscore job remains independent of reviewed Pylint and generated
projects do not run the legacy plugin.
