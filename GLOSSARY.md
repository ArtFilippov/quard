# Quard analysis vocabulary

Quard distinguishes source evidence from the units used to decide whether two
pieces of consumer code are duplicated. See [domain context](CONTEXT.md) for
the detailed policy.

## Source layout

**Physical line length**:
The width of one source line, regardless of the expression it contains. Pylint
C0301 is disabled in both profiles; Quard does not impose a universal width.
_Avoid_: Treating Stairfmt's expression or string limits as a line limit

**Stairfmt expression limit**:
The maximum visible expression text kept inline in Stairfmt's supported layout
scope (`inline_limit`, default 240). Over-limit protected expressions require
simplification; ordinary string tokens have their own `string_limit` (default
160). Neither limit measures all physical source lines. See
[ADR 0010](docs/adr/0010-stairfmt-without-physical-line-limit.md).
_Avoid_: Pylint max-line-length

## Clone analysis

**Clone pass**:
One independently reported kind of duplication: exact, renamed or similar.
_Avoid_: A single combined clone score

**Detection unit**:
A module, class, function or supported declaration group whose contents may be
compared for clones without joining its neighboring units.
_Avoid_: File-wide clone window

**Eligible construct**:
A complete behavioral operation counted by a clone pass's minimum; a physical
continuation line or partial expression is not another construct.
_Avoid_: Source line

**Declaration unit**:
A separately compared exact schema or ordered contract declaration. Its eligible
fields or signature lines do not become renamed or similar behavior.
_Avoid_: Executable behavior

**Source fragment**:
The verbatim original text between the reported clone positions, including any
non-eligible intervening source lines.
_Avoid_: Prepared text

## Envelope structure

**Read-only property contract**:
A getter-only `@property` declaration promises a public read and return type, not
identical decorator spelling in its implementation. A proven `qraft.cached_property`
implements that read with per-instance caching; it rejects writes and deletion.
Writable contracts require an ordinary setter/deleter property. Class contracts,
field ownership and restricted access recognize decorated and constructor-call
forms; base Envelopes recognize directly forwarded decorated getters.
_Avoid_: Assuming a cached property supplies a setter

**Base Envelope**:
An object that directly implements a local or explicitly imported external contract
by forwarding methods and property accessors to one stored delegate. Its constructor
may first call the parent without arguments; forwarding does not depend on an
`@override` annotation. This proves declared forwarding, not the complete contract
interface or the composition of particular objects.
_Avoid_: Class identified by an Envelope suffix

**Assembly Envelope**:
An object descended from a base Envelope, directly or through other assembly
Envelopes, that calls its parent constructor and inherits its behavior. Its intended
delegate composition is not statically proved. Its `envelopes/` subtree inherits
assembly import and concrete-type rights at every depth.
_Avoid_: Factory
