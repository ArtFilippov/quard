# Constructor calls across an opaque external boundary

## Decision

The constructor-purity check follows static call origins through straightforward
assignments, branch joins, explicit imports and customer re-exports. A project
class or builtin type (including proven aliases) is a constructor target; project
functions and factories are not. Every alternative at a join must qualify. A
missing or conflicting origin remains unknown and fails closed.

An explicitly imported external symbol or a member of an external module has
external provenance. A member called through a receiver with a proven external
type annotation also crosses that boundary. Such calls are opaque: Quard cannot
determine whether an external symbol is a class, function or callable instance
without inspecting its implementation or declarations. They qualify as eager
calls while their arguments and keyword values are checked recursively. An
unknown receiver, a customer import that cannot be resolved, and an unqualified
unknown name do not establish external provenance. Ordinary builtin functions
other than the four approved validators remain explicitly denied.

Quard reads discovered customer source to establish origins but does not import
or execute it. It never reads external source, stubs or type declarations to
classify external callable kind. Exact external-type annotations on parameters
and local or direct receiver fields provide receiver evidence; arbitrary return
values from external calls do not gain an inferred type.

## Consequences

This boundary accepts external work in a constructor, including possible side
effects. It is a deliberate provenance rule, **not a runtime purity claim**.
Unknowns and mixed targets may require clearer imports or annotations. Dynamic
rebinding, indirect inference and unresolved flow remain outside
the static guarantee. Proven external decorator applications are opaque as well;
project decorators, including returned project decorators and callable instances,
are checked at each eager application stage without inspecting external source.
The independent constructor rule retains positioned
violations and the normal check/report exit semantics; native diagnostics still
apply independently.
