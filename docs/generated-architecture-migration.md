# Adopting generated contract-only architecture

Use the full check from the consumer root after installing or updating Quard:

```bash
uv sync --locked --group dev
uv run --locked --group dev python quard/check.py
```

Inspect `.quard/reports/latest.json` for every job's status, exit code, and
positioned diagnostics. Exit 1 means violations; exit 2 means an analysis or
configuration failure. Read the archived raw Tach, Pylint, and worker logs
before changing source. Do not sync Tach to accept existing imports.

1. **Split concrete facades.** Replace `from .sender import Sender` in package
   `__init__.py` (and companion `__init__.pyi`) with a re-export of a proven
   Protocol or abstract interface, such as
   `from .service import Service as PublicService`. Update `__all__` to name
   only contracts. Check ancestor and aliased re-exports too. Code that
   constructs `Sender` imports it from its defining `sender.py` module inside
   an assembly area, rather than from the facade. An extra import package may
   remain empty; it needs no invented interface.
2. **Give values explicit shapes.** A `NamedTuple` factory produces a concrete
   class, even if its fields look like a contract. Put each factory-created
   class in its own file. Define a local Protocol for consumers to annotate,
   e.g. `IdentityShape` with a `code: str` property and `EntryShape` with an
   `identity: IdentityShape` property. Annotate nested factory fields with
   these shapes and use the shapes in other groups' source and stubs. Keep
   construction of the concrete values in assembly or tests. Do not export
   factories as contracts through facades.
3. **Move cross-group wiring.** Ordinary source modules, including type-only
   imports and stubs, depend on contracts rather than peer concrete classes.
   Put group-specific assembly in that group's direct `envelopes/` directory,
   or global assembly in one directory named `main/` anywhere under `src/`.
   Nested directories inside `envelopes/` are ordinary groups. A `main.py`
   file outside `main/` is also ordinary. Neither assembly area imports a
   Bootstrap module; Bootstrap modules do not import one another. Tests can
   compose implementations while keeping mirrored test-class placement.
4. **Consolidate bootstrap directories.** Move all wiring into one `main/`
   directory and update imports; a second `main/` is rejected even when empty.
   Keep direct Python files out of `src/` and preserve separate package roots
   for additional import packages. For framework bases, inherit the exact
   external type (including its generic specialization) where it is needed;
   this does not make its package, other types, or local concrete subclasses
   contracts. Run the full check again and address all remaining native and
   Quard findings without relaxing checks.

The public temporary consumer lives in
`tests/quard/architecture_draft/adoption_fixture_utils.py`; its full-report
mutation matrix lives in `tests/quard/architecture_draft/test_adoption_consumer.py`.
They require no access to the motivating private consumer or its dependencies.
