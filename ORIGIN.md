# Origin and license

Eryx starts a new Git history on 2026-09-22; it does not transplant consumer history.
It was extracted from `ArtFilippov/tai` at
`7bff128` and the installed Python Quality 1.0.0 tool (MIT).
The original MIT notice is retained unchanged in `LICENSE`.

The installed runner, native adapters, compatibility patches and 42 regression
tests are the authoritative source. The earlier self-extracting distribution's
installer, default templates and 44 infrastructure tests were recovered from its
payload (SHA-256 `e22aab6554c883c1a2020d83a1b7211b16b6a86b7dcbe781e07a4edc6067ce79`).
They were adapted to consumer-owned state and separate tool paths; no analyzer
distributions are vendored. Dependencies retain their upstream licenses.

The former active names `quality/`, `.quality/`, Python Quality and self-extracting
delivery have been replaced. Those identifiers remain only where needed to read
legacy installation state or explain historical provenance. Consumer-specific
limits, accepted architecture and garak policy belong to the consumer, not Eryx.

The original build reported 44 mocked infrastructure tests on Linux/Python 3.13.5,
uv 0.10.0 (2026-09-21), without network/native end-to-end validation. That historical
result is not an Eryx release test. Current verification is performed by `tests/`
and `.github/workflows/ci.yml`, including real installation and native probes.
