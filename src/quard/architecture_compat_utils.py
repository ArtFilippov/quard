"""Semantic fixes for clean-architecture-linter-plugin 1.2.2.

Install before Pylint constructs its checkers. The patch changes checker inputs
and predicates, never the emitted diagnostic stream or numeric limits.
See COMPATIBILITY.md for the upstream defects and regression contract.
"""

from __future__ import annotations

from functools import wraps
from importlib.metadata import version
from pathlib import Path
import sysconfig

import astroid
from astroid import nodes
from astroid.objects import Super
from astroid.util import safe_infer


_installed = False
_pylint_protocol_installed = False

# These symbols supply values, synchronization or thread orchestration, not
# filesystem/network/process I/O. The containing modules are not allowlisted.
_STDLIB_SYMBOLS = {
    "types": {"TracebackType", "MappingProxyType"},
    "threading": {"Event", "Lock", "RLock", "Thread"},
    "urllib.parse": {"urlsplit", "urlunsplit"},
    "shlex": {"quote"},
    "copy": {"deepcopy"},
}

_PATH_WITH_NAME = {"pathlib.PurePath.with_name", "pathlib._abc.PurePathBase.with_name"}


def _definition(value):
    """Use the runtime type's definition, not a literal's lexical AST parent."""
    # In pinned Astroid, Const/List/Dict/Tuple/Set also inherit Instance.
    return value._proxied if isinstance(value, astroid.Instance) else value


def _is_protocol(node):
    """Recognize actual Protocol declarations, including aliases and generics."""
    for base in node.bases:
        if isinstance(base, nodes.Subscript):
            base = base.value
        inferred = safe_infer(base)
        if isinstance(inferred, nodes.ClassDef) and inferred.qname() in {
            "typing.Protocol", "typing_extensions.Protocol",
        }:
            return True
    return False


def _protocol_ancestors(node):
    """Include a Protocol[T] base when Astroid cannot infer its subscription."""
    ancestors = list(node.ancestors())
    for parent in [node, *ancestors]:
        for base in parent.bases:
            if isinstance(base, nodes.Subscript):
                candidate = safe_infer(base.value)
                if (isinstance(candidate, nodes.ClassDef) and _is_protocol(candidate)
                        and candidate not in ancestors):
                    ancestors.append(candidate)
    return ancestors


def _inherited_classes(node):
    """Walk only statically resolved base edges, including imported and generic aliases."""
    pending = [node]
    seen = {node}
    while pending:
        current = pending.pop()
        for base in current.bases:
            if isinstance(base, nodes.Subscript):
                base = base.value
            ancestor = safe_infer(base)
            if not isinstance(ancestor, nodes.ClassDef) or ancestor in seen:
                continue
            seen.add(ancestor)
            yield ancestor
            pending.append(ancestor)


def _declared_contract(node):
    """Recognize local Protocol and abstract-interface declarations, not concrete bases."""
    path = node.root().file
    if not path or not Path(path).resolve().is_relative_to(Path.cwd().resolve()):
        return False
    if _is_protocol(node):
        return True
    bases = {ancestor.qname() for ancestor in _inherited_classes(node)}
    meta = {safe_infer(keyword.value).qname() for keyword in node.keywords
            if keyword.arg == "metaclass" and isinstance(safe_infer(keyword.value), nodes.ClassDef)}
    if not (bases | meta) & {"abc.ABC", "abc.ABCMeta"}:
        return False
    return any(isinstance(member, nodes.FunctionDef) and member.decorators
               and any(isinstance(decorator, nodes.NodeNG) and
                       isinstance(inferred := safe_infer(decorator), nodes.FunctionDef)
                       and inferred.qname() in {"abc.abstractmethod", "abc.abstractproperty"}
                       for decorator in member.decorators.nodes)
               for member in node.body)


def _inherited_contract_member(cls, name):
    """A matching unrelated contract or unknown base cannot establish membership."""
    return any(_declared_contract(ancestor) and
               any(isinstance(member, nodes.FunctionDef) for member in ancestor.locals.get(name, ()))
               for ancestor in _inherited_classes(cls))


def _annotation_definitions(annotation):
    """Resolve a declared value type, without treating a name as a type by spelling."""
    if annotation is None:
        return [astroid.Uninferable]
    if isinstance(annotation, nodes.Const) and annotation.value is None:
        return [_definition(annotation)]
    if isinstance(annotation, nodes.Const) and isinstance(annotation.value, str):
        try:
            expression = astroid.extract_node(annotation.value)
        except astroid.AstroidError:
            return [astroid.Uninferable]
        if not isinstance(expression, nodes.NodeNG):
            return [astroid.Uninferable]
        expression.parent = annotation.parent
        return _annotation_definitions(expression)
    if isinstance(annotation, nodes.BinOp) and annotation.op == "|":
        return _annotation_definitions(annotation.left) + _annotation_definitions(annotation.right)
    if isinstance(annotation, nodes.Subscript):
        annotation = annotation.value
    inferred = safe_infer(annotation)
    return [inferred] if isinstance(inferred, nodes.ClassDef) else [astroid.Uninferable]


def _binding_annotation(binding):
    parent = binding.parent
    if isinstance(parent, nodes.Arguments):
        groups = ((parent.posonlyargs, parent.posonlyargs_annotations),
                  (parent.args, parent.annotations),
                  (parent.kwonlyargs, parent.kwonlyargs_annotations))
        for arguments, annotations in groups:
            for argument, annotation in zip(arguments, annotations):
                if argument is binding:
                    return annotation
    if isinstance(parent, nodes.AnnAssign):
        return parent.annotation
    return None


def _binding_definitions(binding, seen):
    annotation = _binding_annotation(binding)
    if annotation is not None:
        return _annotation_definitions(annotation)
    parent = binding.parent
    if isinstance(parent, nodes.Assign):
        return _value_definitions(parent.value, seen)
    return [astroid.Uninferable]


def _call_return_definitions(node, seen):
    function = safe_infer(node.func)
    if isinstance(function, (nodes.FunctionDef, astroid.BoundMethod, astroid.UnboundMethod)):
        if function.returns is None and function.qname() in _PATH_WITH_NAME:
            # The unannotated pathlib method returns a new path of the
            # receiver's type. Python 3.13 moved it to PurePathBase.
            if isinstance(node.func, nodes.Attribute):
                return _value_definitions(node.func.expr, seen)
        return _annotation_definitions(function.returns)
    if isinstance(node.func, nodes.Attribute):
        values = []
        for owner in _value_definitions(node.func.expr, seen):
            if not isinstance(owner, nodes.ClassDef):
                values.append(astroid.Uninferable)
                continue
            methods = [method for method in owner.methods() if method.name == node.func.attrname]
            for method in methods:
                if method.returns is None and method.qname() in _PATH_WITH_NAME:
                    values.append(owner)
                else:
                    values.extend(_annotation_definitions(method.returns))
            if not methods:
                values.append(astroid.Uninferable)
        return values
    return [astroid.Uninferable]


def _stdlib_pathlib_path(definition):
    """Recognize Python 3.13 path classes moved out of pathlib's safe root."""
    if not isinstance(definition, nodes.ClassDef) or definition.qname() not in {
            f"pathlib._local.{name}" for name in (
                "Path", "PurePath", "PosixPath", "WindowsPath", "PurePosixPath", "PureWindowsPath")
    }:
        return False
    module = definition.root()
    expected = Path(sysconfig.get_path("stdlib"), "pathlib", "_local.py")
    return bool(module.file and Path(module.file).resolve() == expected.resolve())


def _sequence_annotation_elements(annotation):
    if not isinstance(annotation, nodes.Subscript):
        return [astroid.Uninferable]
    container = safe_infer(annotation.value)
    if not isinstance(container, nodes.ClassDef):
        return [astroid.Uninferable]
    name = container.qname()
    if name not in {
        "builtins.tuple", "builtins.list", "typing.Tuple", "typing.List",
        *(f"{module}.{kind}" for module in ("typing", "collections.abc", "_collections_abc")
          for kind in ("Generator", "Iterator", "Iterable", "Sequence")),
    }:
        return [astroid.Uninferable]
    elements = annotation.slice.elts if isinstance(annotation.slice, nodes.Tuple) else [annotation.slice]
    if name in {"builtins.tuple", "typing.Tuple"}:
        if len(elements) == 2 and isinstance(elements[1], nodes.Const) and elements[1].value is Ellipsis:
            elements = elements[:1]
    else:
        # Generator's additional parameters describe send/return, not yield.
        elements = elements[:1]
    return [value for element in elements for value in _annotation_definitions(element)]


def _sequence_element_definitions(node, seen):
    """Recover typed iteration through tuple/list materialization and indexing."""
    if id(node) in seen:
        return [astroid.Uninferable]
    seen = seen | {id(node)}
    if isinstance(node, nodes.Call):
        function = safe_infer(node.func)
        if (isinstance(function, nodes.ClassDef) and function.qname() in {"builtins.tuple", "builtins.list"}
                and len(node.args) == 1 and not node.keywords):
            return _sequence_element_definitions(node.args[0], seen)
        if isinstance(function, (nodes.FunctionDef, astroid.BoundMethod, astroid.UnboundMethod)):
            return _sequence_annotation_elements(function.returns)
    if isinstance(node, nodes.Name):
        _scope, bindings = node.lookup(node.name)
        values = []
        for binding in bindings:
            annotation = _binding_annotation(binding)
            if annotation is not None:
                values.extend(_sequence_annotation_elements(annotation))
            elif isinstance(binding.parent, nodes.Assign):
                values.extend(_sequence_element_definitions(binding.parent.value, seen))
            else:
                values.append(astroid.Uninferable)
        return values
    return [astroid.Uninferable]


def _value_definitions(node, seen=None):
    """Use annotations only when value inference provides no concrete evidence.

    Astroid intentionally does not infer argument values from annotations. The
    Domain-value predicate nevertheless needs the declared type of injected
    parameters and annotated dependency fields. Concrete conflicting evidence
    wins, and an unknown/union alternative never grants the exemption.
    """
    seen = set() if seen is None else seen
    if id(node) in seen:
        return [astroid.Uninferable]
    seen = seen | {id(node)}
    try:
        inferred = list(node.infer())
    except astroid.InferenceError:
        inferred = []
    if any(value is not astroid.Uninferable for value in inferred):
        return [_definition(value) for value in inferred]
    if isinstance(node, nodes.Call):
        return _call_return_definitions(node, seen)
    if isinstance(node, nodes.Subscript) and not isinstance(node.slice, nodes.Slice):
        return _sequence_element_definitions(node.value, seen)
    if isinstance(node, nodes.Name):
        _scope, bindings = node.lookup(node.name)
        return [value for binding in bindings for value in _binding_definitions(binding, seen)]
    if isinstance(node, nodes.Attribute):
        values = []
        for owner in _value_definitions(node.expr, seen):
            if not isinstance(owner, nodes.ClassDef):
                values.append(astroid.Uninferable)
                continue
            for cls in [owner, *owner.ancestors()]:
                bindings = cls.locals.get(node.attrname, []) + cls.instance_attrs.get(node.attrname, [])
                if bindings:
                    values.extend(value for binding in bindings
                                  for value in _binding_definitions(binding, seen))
                    break
            else:
                values.append(astroid.Uninferable)
        return values
    return [astroid.Uninferable]


def _is_inherited_super_hook(node, receiver):
    """Prove a builtin super proxy resolves an actual method in this class's MRO."""
    if not isinstance(node.expr, nodes.Call):
        return False
    method = node.scope()
    if not isinstance(method, nodes.FunctionDef) or not isinstance(method.parent, nodes.ClassDef):
        return False
    cls = method.parent
    arguments = node.expr.args
    if arguments:
        parameters = method.args.posonlyargs + method.args.args
        if (len(arguments) != 2 or not parameters or not isinstance(arguments[1], nodes.Name)
                or arguments[1].name != parameters[0].name):
            return False
    if _definition(receiver.type) is not cls:
        return False
    try:
        ancestors = receiver.super_mro()
        hooks = list(receiver.igetattr(node.attrname))
    except astroid.AstroidError:
        return False
    for hook in hooks:
        if isinstance(hook, (astroid.BoundMethod, astroid.UnboundMethod)):
            hook = hook._proxied
        if not isinstance(hook, nodes.FunctionDef) or hook.parent not in ancestors:
            return False
    return bool(hooks)


def _import_names(node, names):
    """Resolve relative imports and imported submodules without executing code.

    A re-exported class still belongs to the imported public module boundary;
    an imported module belongs to its own (possibly more specific) boundary.
    """
    if not isinstance(node, nodes.ImportFrom):
        return names
    absolute = node.root().relative_to_absolute_name(node.modname or "", node.level)
    try:
        module = node.do_import_module()
    except astroid.AstroidError:
        module = None
    resolved = []
    for name, _alias in node.names:
        target = absolute
        if module is not None and name != "*":
            try:
                inferred = next(module.igetattr(name), astroid.Uninferable)
            except astroid.AstroidError:
                inferred = astroid.Uninferable
            if isinstance(inferred, nodes.Module):
                target = inferred.name
        resolved.append(target)
    return list(dict.fromkeys(resolved))


def _explicit_layer(loader, module_name):
    # Upstream startswith("pkg.core") also matches unrelated "pkg.core_io".
    layers = sorted(loader.config.get("layers", []),
                    key=lambda layer: len(layer.get("module", "")), reverse=True)
    for layer in layers:
        prefix = layer.get("module", "")
        if prefix and (module_name == prefix or module_name.startswith(prefix + ".")):
            return layer.get("name")
    return None


def _patch_layers(loader):
    def get_layer_for_module(self, module_name, file_path=""):
        return (_explicit_layer(self, module_name)
                or self.registry.resolve_layer("", file_path or module_name))

    loader.get_layer_for_module = get_layer_for_module


def _safe_stdlib_import(node, name):
    allowed = _STDLIB_SYMBOLS.get(name)
    if not allowed:
        return False
    try:
        module = node.do_import_module(name)
    except astroid.AstroidError:
        return False
    # Reject a project/third-party module shadowing a standard-library name.
    expected = Path(sysconfig.get_path("stdlib"), *name.split(".")).with_suffix(".py")
    if not module.file or Path(module.file).resolve() != expected.resolve():
        return False
    if isinstance(node, nodes.ImportFrom):
        return all(symbol in allowed for symbol, _alias in node.names)
    imported, alias = next(pair for pair in node.names if pair[0] == name)
    bound_name = alias or imported.split(".")[0]
    used = False
    for reference in node.root().nodes_of_class(nodes.Name):
        if reference.name != bound_name or node not in reference.lookup(bound_name)[1]:
            continue
        used = True
        qualified = imported if alias else bound_name
        expression = reference
        while isinstance(expression.parent, nodes.Attribute) and expression.parent.expr is expression:
            expression = expression.parent
            qualified += "." + expression.attrname
        if qualified not in {f"{name}.{symbol}" for symbol in allowed}:
            return False
    return used


def _patch_imports(resource, dependency):
    def check_resources(self, node, names):
        root = node.root()
        path = (root.file or "").replace("\\", "/")
        if ("tests" in path.split("/") or "test" in path.split("/")
                or ".tests." in root.name or root.name.startswith(("tests.", "test_"))):
            return
        layer = self.config_loader.get_layer_for_module(root.name, root.file or "")
        if layer not in {"Domain", "UseCase"}:
            return
        for name in _import_names(node, names):
            imported_layer = self.config_loader.get_layer_for_module(name)
            if imported_layer in {"Domain", "UseCase"}:
                continue
            # __future__ enables compiler features; it cannot perform raw I/O.
            if name == "__future__":
                continue
            if imported_layer is None and _safe_stdlib_import(node, name):
                continue
            if any(name == prefix or name.startswith(prefix + ".")
                   for prefix in self.allowed_prefixes):
                continue
            # Retain the native legacy convention only for unclassified imports.
            if imported_layer is None and set(name.split(".")) & {
                "domain", "dto", "use_cases", "protocols", "models",
            }:
                continue
            self.add_message("clean-arch-resources", node=node,
                             args=(f"import {name}", layer))
            return

    def check_dependency(self, node, import_name):
        root = node.root()
        path = root.file or ""
        if "tests" in path.split("/") or "test_" in path.split("/")[-1]:
            return
        current = self.config_loader.get_layer_for_module(root.name, path)
        if not current:
            return
        # An explicitly configured composition root may wire every layer. No
        # other native layer's allowed targets include Bootstrap.
        if current == "Bootstrap":
            return
        for name in _import_names(node, [import_name]):
            # Explicit, most-specific mappings must precede directory guesses.
            imported = self.config_loader.get_layer_for_module(name)
            if not imported or current == imported:
                continue
            if imported not in self.DEFAULT_RULES.get(current, set()):
                self.add_message("clean-arch-dependency", node=node, args=(current, imported))

    def visit_importfrom(self, node):
        self._check_import(node, [node.modname or ""])

    resource._check_import = check_resources
    resource.visit_importfrom = visit_importfrom
    dependency._check_import = check_dependency


def _patch_contracts(checker):
    def is_domain_protocol(self, ancestor):
        root = ancestor.root()
        return (self.config_loader.get_layer_for_module(root.name, root.file or "") == "Domain"
                and _is_protocol(ancestor))

    original = checker.visit_functiondef

    @wraps(original)
    def visit_functiondef(self, node):
        if isinstance(node.parent, nodes.ClassDef) and _is_protocol(node.parent):
            return
        if node.decorators:
            for decorator in node.decorators.nodes:
                inferred = safe_infer(decorator)
                if isinstance(inferred, nodes.FunctionDef) and inferred.qname() in {
                    "typing.overload", "typing_extensions.overload",
                }:
                    return
        return original(self, node)

    def has_domain_protocol_ancestor(self, node):
        return any(self._is_domain_protocol(ancestor) for ancestor in _protocol_ancestors(node))

    def get_protocol_methods(self, node):
        return {method.name for ancestor in _protocol_ancestors(node)
                if self._is_domain_protocol(ancestor)
                for method in ancestor.methods() if not method.name.startswith("_")}

    checker._is_domain_protocol = is_domain_protocol
    checker._has_domain_protocol_ancestor = has_domain_protocol_ancestor
    checker._get_protocol_methods = get_protocol_methods
    checker.visit_functiondef = visit_functiondef


def _patch_design(checker):
    def infrastructure_origin(self, definition):
        module = definition.root().name
        explicit = _explicit_layer(self.config_loader, module)
        if explicit:
            return explicit == "Infrastructure"
        return any(module == prefix or module.startswith(prefix + ".")
                   for prefix in self.infrastructure_modules)

    def is_infrastructure_inferred(self, inferred):
        if inferred is astroid.Uninferable:
            return False
        definition = _definition(inferred)
        if infrastructure_origin(self, definition):
            return True
        if isinstance(definition, nodes.ClassDef):
            return any(ancestor.name in self.raw_types or infrastructure_origin(self, ancestor)
                       for ancestor in definition.ancestors())
        return False

    def domain_protocol(self, cls):
        return (isinstance(cls, nodes.ClassDef) and _is_protocol(cls)
                and self.config_loader.get_layer_for_module(cls.root().name, cls.root().file or "")
                == "Domain")

    def domain_contracts(self, cls):
        return [ancestor for ancestor in _protocol_ancestors(cls) if domain_protocol(self, ancestor)]

    def contractual_return(self, node, value):
        # A meaningful declaration must also agree with the actual inferred
        # implementation. object/Any, annotations lying about raw values, and
        # external raw-type ancestry do not provide an abstraction boundary.
        if not isinstance(value, astroid.Instance):
            return False
        cls = _definition(value)
        method = node.scope()
        if not isinstance(cls, nodes.ClassDef) or not isinstance(method, nodes.FunctionDef):
            return False
        ancestors = _protocol_ancestors(cls)
        if any(ancestor.name in self.raw_types for ancestor in ancestors):
            return False
        declared = _annotation_definitions(method.returns)
        if not declared or any(not isinstance(item, nodes.ClassDef) or
                               not (item.qname() == "builtins.NoneType" or domain_protocol(self, item)
                                    or domain_contracts(self, item)) for item in declared):
            return False
        root = node.root()
        internal = (self.config_loader.get_layer_for_module(root.name, root.file or "") == "Infrastructure"
                    and (isinstance(method.parent, nodes.Module)
                         or isinstance(method.parent, nodes.ClassDef)
                         and not _inherited_contract_member(method.parent, method.name)))
        for contract in declared:
            if contract is cls or contract in ancestors:
                if domain_protocol(self, contract) or internal:
                    return True
        return False

    def context_manager_self_return(node):
        method = node.scope()
        if not isinstance(method, nodes.FunctionDef) or not isinstance(method.parent, nodes.ClassDef):
            return False
        cls = method.parent
        exits = {"__enter__": "__exit__", "__aenter__": "__aexit__"}
        if method.name not in exits or not any(member.name == exits[method.name] for member in cls.methods()):
            return False
        parameters = method.args.posonlyargs + method.args.args
        if not parameters or not isinstance(node.value, nodes.Name) or node.value.name != parameters[0].name:
            return False
        if _definition(safe_infer(node.value)) is not cls:
            return False
        annotation = safe_infer(method.returns) if method.returns else None
        return (_annotation_definitions(method.returns) == [cls]
                or annotation is not None and annotation is not astroid.Uninferable
                and annotation.qname() in {"typing.Self", "typing_extensions.Self"})

    def domain_facing_method(self, node):
        method = node.scope()
        return (isinstance(method, nodes.FunctionDef) and
                isinstance(method.parent, nodes.ClassDef) and
                _inherited_contract_member(method.parent, method.name))

    def visit_return(self, node):
        if node.value is None:
            return
        root = node.root()
        if (self.config_loader.get_layer_for_module(root.name, root.file or "") == "Bootstrap"
                and not domain_facing_method(self, node)):
            return
        if context_manager_self_return(node):
            return
        try:
            inferred = list(node.value.infer())
        except astroid.InferenceError:
            inferred = []
        for value in inferred:
            if value is astroid.Uninferable:
                continue
            type_name = getattr(value, "name", None)
            raw = type_name in self.raw_types
            if raw or (self._is_infrastructure_inferred(value) and not contractual_return(self, node, value)):
                if type_name:
                    self.add_message("naked-return-violation", node=node, args=(type_name,))
                    return
        if not inferred or all(value is astroid.Uninferable for value in inferred):
            type_name = self._get_inferred_type_name(node.value)
            if type_name in self.raw_types:
                self.add_message("naked-return-violation", node=node, args=(type_name,))

    checker._is_infrastructure_inferred = is_infrastructure_inferred
    checker.visit_return = visit_return


def _patch_visibility(checker):
    original = checker.visit_attribute

    @wraps(original)
    def visit_attribute(self, node):
        if node.attrname.startswith("_") and not node.attrname.startswith("__"):
            receiver = safe_infer(node.expr)
            if isinstance(receiver, Super):
                if _is_inherited_super_hook(node, receiver):
                    return
                # Super's lexical root is this module, not the hook definition.
                return original(self, node)
            root = node.root()
            layer = self.config_loader.get_layer_for_module(root.name, root.file or "")
            try:
                owners = list(node.expr.infer())
            except astroid.InferenceError:
                owners = []
            same_boundary = bool(owners)
            for owner in owners:
                if owner is astroid.Uninferable:
                    same_boundary = False
                    break
                definition = _definition(owner).root()
                owner_layer = self.config_loader.get_layer_for_module(
                    definition.name, definition.file or "")
                if definition.name != root.name and (not layer or owner_layer != layer):
                    same_boundary = False
                    break
            if same_boundary:
                return
        return original(self, node)

    checker.visit_attribute = visit_attribute


def _patch_coupling(checker):
    from clean_architecture_linter.config import ConfigurationLoader

    original_chain = checker._is_method_chain_violation
    original_assign = checker.visit_assign

    @wraps(original_chain)
    def is_method_chain_violation(self, node):
        if isinstance(node.func, nodes.Attribute):
            receiver = safe_infer(node.func.expr)
            # Package namespaces and class methods are not object traversal.
            if isinstance(receiver, (nodes.Module, nodes.ClassDef)):
                return False
            # The first owned field can itself be an injected Domain bundle.
            # Do not grant this exemption for an arbitrary stranger's field.
            head = node.func.expr
            while isinstance(head, nodes.Attribute) and isinstance(head.expr, nodes.Attribute):
                head = head.expr
            if (isinstance(head, nodes.Attribute) and isinstance(head.expr, nodes.Name)
                    and head.expr.name in {"self", "cls"}
                    and self._is_allowed_by_inference(head, ConfigurationLoader(), domain_only=True)):
                return False
        return original_chain(self, node)

    @wraps(original_assign)
    def visit_assign(self, node):
        if isinstance(node.value, nodes.Call):
            constructor = safe_infer(node.value.func)
            if isinstance(constructor, nodes.ClassDef):
                # An object constructed here is owned here, not a method result.
                for target in node.targets:
                    if isinstance(target, nodes.AssignName):
                        self._locals_map.pop(target.name, None)
                return
        return original_assign(self, node)

    def is_allowed_by_inference(self, node, config_loader, *, domain_only=False):
        definitions = _value_definitions(node)
        if not definitions:
            return False
        for definition in definitions:
            if definition is astroid.Uninferable:
                return False
            root = definition.root()
            # Operations on a proved builtin scalar value (e.g. bytes.decode)
            # cannot traverse a stranger's object graph. Subclasses do not qualify.
            if (not domain_only and isinstance(definition, nodes.ClassDef) and definition.qname() in {
                    "builtins.str", "builtins.bytes", "builtins.int", "builtins.bool",
                    "builtins.float", "builtins.complex"}):
                continue
            if (not domain_only and (root.name in config_loader.allowed_lod_roots
                                     or "pathlib" in config_loader.allowed_lod_roots
                                     and _stdlib_pathlib_path(definition))):
                continue
            layer = config_loader.get_layer_for_module(root.name, root.file or "")
            if layer and ("domain" in layer.lower() or "dto" in layer.lower()):
                continue
            return False
        return True

    checker._is_method_chain_violation = is_method_chain_violation
    checker.visit_assign = visit_assign
    checker._is_allowed_by_inference = is_allowed_by_inference


def install_pylint_protocol_compat() -> None:
    """Recognize Protocol interfaces and ellipsis return declarations in Pylint."""
    global _pylint_protocol_installed
    if _pylint_protocol_installed:
        return
    installed_version = version("pylint")
    if installed_version != "4.0.8":
        raise RuntimeError(f"Review Protocol compatibility for Pylint {installed_version}")
    from pylint.checkers import design_analysis
    from pylint.checkers.typecheck import TypeChecker

    original = design_analysis._is_exempt_from_public_methods

    @wraps(original)
    def is_exempt_from_public_methods(node):
        return _is_protocol(node) or original(node)

    original_ignored = TypeChecker._is_ignored_function

    @wraps(original_ignored)
    def is_ignored_function(function):
        # A Protocol's ellipsis body declares a signature, not an implicit None
        # return. Keep checking concrete overrides and default implementations.
        owner = function.parent
        body = function.body
        if (isinstance(owner, nodes.ClassDef) and _is_protocol(owner)
                and len(body) == 1 and isinstance(body[0], nodes.Expr)
                and isinstance(body[0].value, nodes.Const) and body[0].value.value is Ellipsis):
            return True
        return original_ignored(function)

    design_analysis._is_exempt_from_public_methods = is_exempt_from_public_methods
    TypeChecker._is_ignored_function = staticmethod(is_ignored_function)
    _pylint_protocol_installed = True


def install_pinned_plugin() -> None:
    """Patch the native checkers using their private APIs inside this module.

    Raises:
        RuntimeError: The installed plugin version has not been reviewed.
    """
    global _installed
    if _installed:
        return
    installed_version = version("clean-architecture-linter-plugin")
    if installed_version != "1.2.2":
        raise RuntimeError(f"Review architecture compatibility for plugin {installed_version}")
    from clean_architecture_linter.config import ConfigurationLoader
    from clean_architecture_linter.checks.boundaries import ResourceChecker, VisibilityChecker
    from clean_architecture_linter.checks.contracts import ContractChecker
    from clean_architecture_linter.checks.dependencies import DependencyChecker
    from clean_architecture_linter.checks.design import DesignChecker
    from clean_architecture_linter.checks.patterns import CouplingChecker

    _patch_layers(ConfigurationLoader)
    _patch_imports(ResourceChecker, DependencyChecker)
    _patch_contracts(ContractChecker)
    _patch_design(DesignChecker)
    _patch_visibility(VisibilityChecker)
    _patch_coupling(CouplingChecker)
    _installed = True


def install_architecture_compat() -> None:
    """Install once per worker, before ``pylint.lint.Run`` loads plugins."""
    from quard.architecture_compatibility.pinned_architecture_compatibility import (
        PinnedArchitectureCompatibility,
    )

    PinnedArchitectureCompatibility().install()
