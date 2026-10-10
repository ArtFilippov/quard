"""Exercise historical CLI fixture reports without changing consumer selection."""

from unittest.mock import patch


def historical_cli(test):
    """Keep older offline analyzer fixtures on their existing CLI test seam."""
    test.enterContext(patch("quard.check_runner.native_check_runner.require_source_pin"))
    test.enterContext(patch("quard.check_runner.runner_utils.require_source_pin"))
    test.enterContext(patch("quard.check_runner.runner_utils.clone_engine_enabled",
                            return_value=False))
