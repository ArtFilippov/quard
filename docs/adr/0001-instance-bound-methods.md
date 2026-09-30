# Favor instance-bound behavior over class-level APIs

Eryx rejects explicitly declared built-in static and class methods in consumer
production and test code, including stubs, because behavior should belong to
objects with instance-bound responsibilities rather than class-level APIs. This
choice trades the convenience of static factories and class utilities for an
object-oriented design whose dependencies and behavior can be expressed on an
instance. The rule does not penalize inherited library methods, instance-bound
properties or abstract instance contracts; it applies only when a built-in
non-instance decorator can be established in the consumer's source.
