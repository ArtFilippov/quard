# Handoff: prohibit overriding customer-owned implementations

## Goal and agreed scope

Add an Eryx diagnostic when a customer class declares a method already defined by a customer-owned ancestor that is **not** a contract declaration. This applies even when the method name also appears in an inherited contract. Include private methods and property accessors; the user explicitly exempted **Python magic methods**. External or unresolved ancestors do not establish a violation. Cover discovered production and test `.py` and `.pyi` files.

The user requested a plan, then requested this handoff at the repository root. No implementation has been started; the working tree was clean when this document was created.

## Where to work

- `src/eryx/_class_contracts_worker.py`: existing `Contracts` resolver, `declaration()` for local Protocol/abstract-interface classification, `inherited_api()` and `diagnostics()`; add ancestry/override detection and a source-positioned rule without suppressing existing diagnostics. Handle direct and transitive customer ancestors, aliases/re-exports, and source/stub declarations consistently.
- `src/eryx/_reports.py`: register the new rule in `parse_class_contracts()` as a normal violation, preserving malformed-report/error behavior.
- `tests/test_class_contracts.py`: standalone end-to-end worker/report regressions for concrete and abstract contract bases, transitive inheritance, methods also listed in a contract, private methods, property accessors, magic-method exemption, aliases, stubs, and unknown/external bases. Existing tests may need updated diagnostic expectations where they intentionally override a concrete customer class.
- `PROFILE.md` and `README.md`: document the rule and its scope alongside the existing class-contract policy.

## Relevant existing policy

Read `README.md`, `PROFILE.md`, and `COMPATIBILITY.md` before changing analyzer policy, per `AGENTS.md`. The class-contract job already runs over discovered production/test sources and stubs independently of LCOM4 selection. `Contracts.declaration()` identifies local contract declarations; `Contracts.nodes` prefers `.py` to `.pyi` for shared names, while `Contracts.declarations` retains both paths. `src/eryx/_lcom4_worker.py` has a `SPECIAL_METHODS` list, but confirm how to interpret the user's “Python magic methods” exemption (e.g., all `__dunder__` methods) before reusing a narrower list. Do not relax other checks or filter native diagnostics.

## Verification

After implementation, run focused `tests/test_class_contracts.py`, then from the Eryx checkout:

```bash
uv sync --locked --group dev
uv run --locked --group dev python -B -m unittest discover -s tests -v
uv run --locked --group dev python check.py
```

Inspect `.eryx/reports/latest.json` for each tool's status, actual exit codes, and unresolved diagnostics; do not call tool errors passes. Run the affected consumer's full static and relevant runtime checks if a consumer is available, and check that consumer submodules remain clean. Do not use `tach sync` routinely to make checks green.

## Suggested skills

- `implement`: use when moving from this agreed plan into the code change.
