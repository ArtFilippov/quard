# Exclude named gateway files from source analysis

A production file under `src/` ending exactly in `_gw.py`, and its `_gw.pyi`
counterpart, remains importable by Python but is outside Quard's analyzed source
graph. The filename is an explicit opt-out for a thin bridge to an external
dependency. Quard does not prove that the bridge is thin or safe: any code in
the file bypasses its diagnostics. Reports list the excluded paths separately.

Generated Tach excludes the discovered files natively; reviewed Tach receives
the same file exclusions through apply without accepting or changing its
declared dependencies. Pyright uses both `exclude` and `ignore` so an import
cannot restore diagnostics *inside* a gateway. Other analyzers omit gateway
files from their source input. Diagnostics in ordinary importers and adjacent
source files, including existing contract-only and external-dependency rules,
remain active. A nonexistent `_gw` module gains no exemption.

This deliberately trades strict source-boundary coverage for an explicit,
visible escape hatch at a single import site. Prefer keeping bridges small and
reviewing their code separately; Quard cannot claim to have checked them.
