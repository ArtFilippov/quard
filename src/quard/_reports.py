"""Compatibility imports for the native reporting utilities."""

from quard.report_utils import (
    ANSI,
    clean,
    diagnostic,
    find_json,
    normalize,
    parse_class_contracts,
    parse_constructor_purity,
    parse_contract_folders,
    parse_free_function_placement,
    parse_leading_underscores,
    parse_instance_fields,
    parse_lcom4,
    parse_method_decorators,
    parse_outer_classes,
    parse_pydoclint,
    parse_pylint,
    parse_pyright,
    parse_tach,
    parse_test_placement,
    relative_path,
    sorted_diagnostics,
    tach_rule,
)

__all__ = [
    "ANSI", "clean", "diagnostic", "find_json", "normalize", "parse_class_contracts",
    "parse_constructor_purity", "parse_contract_folders", "parse_free_function_placement",
    "parse_leading_underscores",
    "parse_instance_fields", "parse_lcom4", "parse_method_decorators", "parse_outer_classes",
    "parse_pydoclint", "parse_pylint", "parse_pyright", "parse_tach", "parse_test_placement",
    "relative_path", "sorted_diagnostics", "tach_rule",
]
