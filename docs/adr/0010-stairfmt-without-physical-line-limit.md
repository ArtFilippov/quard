# Let Stairfmt own supported layout without a physical-line limit

Stairfmt formats composed calls, block indentation and eligible ordinary string
literals; its `inline_limit` measures supported protected inline and binary
expression text, not simple calls, and its `string_limit` measures visible literal
content. Protected inline expressions, comments, calls
inside f-strings, stubs and excluded literal kinds do not all receive a width
rewrite. A universal physical-line limit would flag these deliberately unchanged
lines and conflict with safe, AST-preserving formatting.

Disable Pylint C0301 (`line-too-long`) in both production and test native profiles.
Keep `max_module_lines`, other format checks, design checks and all unrelated native
findings. Remove `max_line_length` from Quard readability settings and generated
Pylint configuration. Existing `.quard/config.toml` files containing the retired
key fail validation (exit 2) until users remove it and reapply; do not silently
migrate it. The policy promises Stairfmt's documented scope, not a universal
source-width guarantee. See [PROFILE.md](../../PROFILE.md#tools-and-scope).
