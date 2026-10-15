# Ship qraft as an optional runtime distribution

Quard's analyzer is dev-only, but application code needs a typed, read-only
cached property at runtime. We ship `qraft` from a subdirectory of the same
Quard Git revision so consumers can pin and lock it independently without
installing analyzer tools into production; unused runtime APIs impose no
dependency on other consumers. `cached_property` inherits `property` so pinned
Pyright recognizes property-contract overrides and keeps its native override
diagnostics, while the descriptor itself caches in each instance's mutable
dictionary and rejects assignment and deletion. This deliberate inheritance
ties the runtime API to Python's property protocol instead of treating it as
an arbitrary descriptor.
