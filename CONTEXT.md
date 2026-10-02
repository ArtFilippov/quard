# Eryx domain context

## Customer class

A **customer class** is a class declared in a consumer's discovered production or
test `.py` or `.pyi` scope, including nested and function-local declarations.
Its explicitly inherited contract describes the interface it implements or the
external type it extends. A locally declared Protocol or abstract interface is
itself a contract declaration. An external base is opaque: Eryx does not inspect
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
