"""Compatibility imports for identity marker analysis."""
from quard.marker_analysis.marker_index import (FUNCTIONS, MARKER, MarkerIndex,
                                               identity_definition, import_base, local_names,
                                               module_name)
from quard.marker_binding_analysis.marker_bindings import MarkerBindings

__all__ = ["FUNCTIONS", "MARKER", "MarkerIndex", "identity_definition", "import_base",
           "local_names", "module_name", "MarkerBindings"]
