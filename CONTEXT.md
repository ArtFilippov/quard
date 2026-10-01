# Eryx domain context

## Customer class

A **customer class** is a class declared in a consumer's discovered production or
test `.py` or `.pyi` scope, including nested and function-local declarations.
Its explicitly inherited contract describes the interface it implements or the
external type it extends. A locally declared Protocol or abstract interface is
itself a contract declaration. An external base is opaque: Eryx does not inspect
its source. An implicit inheritance from `object` is not an explicit contract;
writing `class Name(object)` is explicit.
