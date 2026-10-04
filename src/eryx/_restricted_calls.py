"""Compatibility imports for restricted-call traversal and target resolution."""
from eryx.ancestry_traversal.ancestry import Ancestry
from eryx.contract_analysis.contracts import Contracts
from eryx.dispatch_site_model.dispatch_site import DispatchSite
from eryx.field_ownership.bindings import Binding, PROTOCOL_BUILTINS, assigned_names, module_name
from eryx.field_ownership.ownership import Ownership
from eryx.field_type_analysis.type_evidence import UNKNOWN, field_evidence
from eryx.marker_analysis.marker_index import FUNCTIONS, local_names
from eryx.restricted_calls.calls import Calls
from eryx.restricted_targets.resolution import Restrictions
from eryx.restricted_targets.restricted_targets import RestrictedTargets

__all__ = ["Ancestry", "Binding", "Calls", "Contracts", "DispatchSite", "FUNCTIONS", "Ownership",
           "PROTOCOL_BUILTINS", "RestrictedTargets", "Restrictions", "UNKNOWN", "assigned_names",
           "field_evidence", "local_names", "module_name"]
