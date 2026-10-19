# Exempt explicitly override-marked method names

The leading-underscore job exempts the name of each directly declared method
decorated with a proven `typing.override` or `typing_extensions.override`, including
aliases and re-exports. It checks each property accessor and source/stub declaration
independently. Quard does not inspect external code to enumerate inherited members;
Pyright checks override existence and compatibility independently during a full
check. This accepts a marker-based exception rather than claiming that the naming
job proved an external override. Unmarked methods remain subject to the naming rule,
including external overrides without a marker; unrelated decorators cannot claim
the exception. The separate customer-method-override rule is unchanged.
