# Scope native exceptions to Envelope evidence

Base Envelopes may assign their one delegate directly or precede that assignment
with a single zero-argument `super().__init__()`; assembly Envelopes still prove
their own parent-constructor call. Direct forwarding with or without a proven
`typing`/`typing_extensions.override` has the same meaning. We keep strict source
and stub proofs rather than requiring incidental initialization or decorator
spelling to satisfy native heuristics. When contract folders are enabled, Pyright's
missing-super and implicit-override rules are disabled only in discovered production
`envelopes/` subtrees. Pylint's R0903 exemption is narrower: only proven base and
assembly classes qualify. Elsewhere these native checks retain their configured
behavior; other native diagnostics and numerical limits remain active. This proves
delegation and parent initialization, never runtime composition of particular objects.
