# Restrict marked behavior to encapsulated receivers

Consumers use an identity decorator named `encapsulated_only` to reserve selected
behavior for an instance and its directly stored dependencies. We choose a
receiver-shaped access boundary and contract-relative restrictions over
ordinary public-call semantics; the cost is reduced callable extraction and
reflective convenience. This accepted policy is
implemented for explicit calls, properties, selected implicit protocols (including
context managers, iteration and awaiting) and declaration agreement; other implicit
dispatch remains pending.

## Marking and agreement

- A directly marked method is restricted. A marked class restricts its declared
  and inherited methods and properties, except methods whose names begin and end
  with double underscores. Explicit method marking overrides that exception.
- An unmarked subclass retains inherited member restrictions. Its newly
  introduced members do not inherit class-wide marking.
- An inherited member retains its marking without redeclaration. This preserves
  the existing prohibition on overriding concrete customer methods.
- Contracts and implementations may mark members independently. A marked contract
  restricts calls through that contract and any implementing or combined type,
  whether or not the member is redeclared there. An implementation-only marker
  restricts its concrete type without marking an unrelated unmarked contract.
  Conflicting inherited contracts produce no declaration disagreement: a proven
  receiver type with a marked contract ancestor is restricted, even if an unmarked
  contract supplies the dispatched member. Access through the unmarked contract
  type alone is unrestricted.
- Source/stub member declarations and overloads of the same owner must agree on
  restriction status; source/stub class marks are compared for inherited members.
  Equivalent class-level and member-level marking and resolved decorator aliases
  count; identical spelling or decorator placement is unnecessary. An agreement
  finding does not weaken established access restrictions.
- Structural resemblance alone does not propagate a Protocol's markers to an
  unrelated implementation. Access through a marked Protocol annotation is
  checked against that contract.
- Consumers define or import the identity marker. Quard supplies no runtime
  decorator API. Marker resolution respects aliases, re-exports, shadowing and
  reassignment; recognition must preserve class identity and instance-method
  binding in existing checks.

## Receiver boundary

- Allow invocation or property access through the proven current instance,
  regardless of the receiver parameter's name, including unreassigned aliases.
- Allow invocation or property access through one direct stored instance field
  of that receiver. Ordinary stored fields, slots and dataclass instance fields
  qualify; properties and class attributes do not.
- Allow proven builtin `super()` access on the current instance.
- Reject proven restricted access through other-instance parameters, constructor
  results, deeper object chains and aliases of stored fields. An object produced
  during implicit dispatch does not automatically inherit access permission.
- Reject invocation of extracted callable aliases, even if extraction used an
  allowed receiver. Taking an ordinary method reference alone is permitted.
  Class-qualified unbound invocation does not qualify, even when its first
  argument is the current instance or a direct stored field.
- Proven literal-name builtin attribute operations use the same boundary as
  direct attribute syntax. Computed names are outside this inference guarantee.
- Nested functions may retain their enclosing method's proven receiver identity.
  Nested classes have their own receiver and do not inherit the enclosing
  instance's privilege. Reassignment invalidates receiver-alias evidence.

## Properties and implicit invocation

- Marking any property accessor restricts the whole property: reads, writes and
  deletions. Class-wide marking covers properties in the same way as methods.
- A restricted cached property remains restricted after caching. Its reads,
  writes and deletions follow the same boundary regardless of cache state.
- Check statically established implicit method dispatch, including context
  managers and operators, using the same receiver boundary. Explicitly marked
  constructor hooks restrict ordinary construction; class-wide marking alone
  excludes those magic methods.
- A statically established possible restricted target suffices for a violation,
  including mixed alternatives and conditional reflected/fallback dispatch.
  Dispatch rules that prove a target unreachable exclude that target.

## Scope and coexistence

The independent rule checks discovered production and test `.py` and `.pyi`
files, including when LCOM4 is narrowed or skipped. It emits positioned access
and declaration-agreement violations, rather than treating agreement failures
as permission. Document implemented inference boundaries and implicit-dispatch
forms precisely; this policy is not a claim of universal runtime coverage.

Unresolved evidence alone does not prove a violation. Source, execution and
malformed-report failures are infrastructure errors, not successful checks.
Existing field, method, contract and native diagnostics remain independent:
permission under this rule grants no exemption from another rule. In particular,
methods and properties remain behavior rather than stored fields as described in
ADR 0002, but marked behavior now has this additional access boundary.
