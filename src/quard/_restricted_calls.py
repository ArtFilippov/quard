"""Compatibility imports for restricted-call traversal and target resolution."""
from quard.ancestry_traversal.ancestry import Ancestry
from quard.contract_analysis.contracts import Contracts
from quard.dispatch_site_model.dispatch_site import DispatchSite
from quard.field_ownership.bindings import Binding, PROTOCOL_BUILTINS, assigned_names, module_name
from quard.field_ownership.ownership import Ownership
from quard.field_type_analysis.type_evidence import UNKNOWN, field_evidence
from quard.marker_analysis.marker_index import FUNCTIONS, local_names
from quard.restricted_calls.calls import Calls
from quard.restricted_targets.resolution import Restrictions
from quard.restricted_targets.restricted_targets import RestrictedTargets

__all__ = ["Ancestry", "Binding", "Calls", "Contracts", "DispatchSite", "FUNCTIONS", "Ownership",
           "PROTOCOL_BUILTINS", "RestrictedTargets", "Restrictions", "UNKNOWN", "assigned_names",
           "field_evidence", "local_names", "module_name"]
