"""Operate on consumer-owned Tach configuration without approving current imports."""

from __future__ import annotations

from pathlib import Path
import re
import sys
from typing import Any, override

import tomlkit

from quard._common import (QuardError, atomic_write, capture, load_toml, read_json,
                          tool_binary, tool_dir, write_json)
from quard._policy import (ALLOWED_LAYERS, LAYER_DIRS, LAYERS, configure, discover_files,
                          exclude_globs, read_scope)
from quard._reports import normalize
from quard.architecture_draft.architecture_draft import ArchitectureDraft
from quard.architecture_draft.contract_boundary_utils import contract_boundaries
from quard.clean_architecture_layers.configured_clean_architecture_layers import (
    ConfiguredCleanArchitectureLayers,
)


class ConfiguredArchitectureDraft(ArchitectureDraft):
    """Draft, validate and accept architecture in the selected consumer root.

    Args:
        root (Path): Selected consumer project.
    """

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._root = root

    @override
    def package_name(self) -> str:
        files = discover_files(self._root, read_scope(self._root))["production"]
        names = {Path(path).parts[1] for path in files if len(Path(path).parts) > 2}
        direct = [path for path in files if len(Path(path).parts) == 2]
        if direct or len(names) != 1:
            raise QuardError(
                "UNSUPPORTED_LAYOUT", "Draft generation expects one import package beneath src/, "
                + "e.g. src/shop/. Existing reviewed multi-package configurations are supported.")
        name = next(iter(names))
        if not name.isidentifier():
            raise QuardError("INVALID_PACKAGE", f"Invalid Python package name: {name}")
        return name

    @override
    def draft(self, *, contract_only: bool = False,
              validate_ownership: bool = True) -> tuple[dict[str, Any], list[str]]:
        scope = read_scope(self._root)
        files = discover_files(self._root, scope)["production"]
        # Infer only conventional directory labels, never communities from a graph.
        modules: dict[str, str] = {self.package_name(): "Domain"}
        notes: list[str] = []
        for filename in files:
            parts = Path(filename).parts[1:]
            for index, part in enumerate(parts[:-1]):
                if index == 0:
                    continue
                if part in LAYER_DIRS:
                    modules[".".join(parts[:index + 1])] = LAYER_DIRS[part]
                    for depth in range(2, index + 1):
                        modules.setdefault(".".join(parts[:depth]), "Domain")
            if len(parts) >= 3 and parts[1] not in LAYER_DIRS:
                modules.setdefault(".".join(parts[:2]), "Domain")
            if parts[-1] in {"__main__.py", "bootstrap.py", "main.py"}:
                modules[".".join((*parts[:-1], Path(parts[-1]).stem))] = "Bootstrap"
        module_list: list[dict[str, Any]] = []
        interfaces: list[dict[str, Any]] = []
        for name, layer in sorted(modules.items()):
            forbidden = [other for other, other_layer in sorted(modules.items())
                         if other != name and other_layer not in ALLOWED_LAYERS[layer]]
            module_list.append({"path": name, "layer": layer, "depends_on": [],
                                "cannot_depend_on": forbidden})
            is_file = (self._root / "src" / (name.replace(".", "/") + ".py")).is_file()
            expose = [] if layer == "Bootstrap" else ([r".*"] if is_file else [r"api(?:\..*)?"])
            interfaces.append({"from": ["^" + re.escape(name) + "$"], "expose": expose})
        config: dict[str, Any] = {
            "source_roots": ["src"], "exclude": exclude_globs(scope) + ["tests/**"] +
            [f"**/{name}/**" for name in scope["test_directory_names"]] + discover_files(self._root, scope)["excluded_gateways"],
            "exact": True, "forbid_circular_dependencies": True,
            "ignore_type_checking_imports": False, "layers_explicit_depends_on": True,
            "root_module": "forbid", "respect_gitignore": False,
            "layers": LAYERS, "modules": module_list, "interfaces": interfaces,
            "rules": {"unused_ignore_directives": "error",
                      "require_ignore_directive_reasons": "error",
                      "unused_external_dependencies": "error"},
        }
        if not any(layer == "UseCase" for layer in modules.values()):
            notes.append("No application/use_cases package found. Review the default Domain "
                         + "assignments; initialization never moves source files.")
        notes.append("Review public API patterns: by default each package exposes api.py "
                     + "only. Write actual allowed exports in tach.toml.")
        notes.append("tach sync records current imports, including undesirable ones. "
                     + "cannot_depend_on and layer checks remain active.")
        return (self._contract_draft(config, files, validate_ownership=validate_ownership)
                if contract_only else (config, notes))

    def _contract_draft(self, config: dict[str, Any], files: list[str], *, validate_ownership: bool
                         ) -> tuple[dict[str, Any], list[str]]:
        contract_boundaries(self._root, files, config, validate_ownership=validate_ownership)
        return config, ["Contract-only boundaries come from discovered local contracts and " +
                        "folder ownership. Review tach.toml before acceptance; " +
                        "do not run tach sync."]

    @override
    def validate(self) -> dict[str, Any]:
        path = self._root / "tach.toml"
        if not path.exists():
            raise QuardError("ARCHITECTURE_NOT_INITIALIZED",
                            "Run uv run --locked --group dev python quard/init_architecture.py; "
                            + "review tach.toml; then use --accept.")
        config = load_toml(path)
        if not config.get("modules") or not isinstance(config["modules"], list):
            raise QuardError("ARCHITECTURE_NOT_INITIALIZED",
                            "tach.toml needs explicit [[modules]] entries.")
        if config.get("source_roots") != ["src"]:
            raise QuardError("INVALID_ARCHITECTURE_CONFIG",
                            "This profile requires source_roots = ['src'].")
        if not config.get("interfaces"):
            raise QuardError("INVALID_ARCHITECTURE_CONFIG",
                            "Define public [[interfaces]] in tach.toml before accepting.")
        for module in config["modules"]:
            if not isinstance(module, dict) or not isinstance(module.get("path"), str):
                raise QuardError("INVALID_ARCHITECTURE_CONFIG",
                                "Every Tach module needs one explicit dotted path.")
            if module.get("layer") not in LAYERS:
                raise QuardError("INVALID_ARCHITECTURE_CONFIG",
                                f"Assign a layer to {module.get('path')}: {LAYERS}")
            if "depends_on" not in module or not isinstance(module["depends_on"], list):
                raise QuardError("INVALID_ARCHITECTURE_CONFIG",
                                f"Explicit depends_on required for {module['path']}.")
            if module.get("unchecked") or module.get("utility"):
                raise QuardError("INVALID_ARCHITECTURE_CONFIG",
                                "unchecked/utility modules bypass the agreed "
                                + "dependency restrictions.")
        required = {"exact": True, "forbid_circular_dependencies": True,
                    "ignore_type_checking_imports": False,
                    "layers_explicit_depends_on": True, "root_module": "forbid"}
        for key, value in required.items():
            if config.get(key) != value:
                raise QuardError("INVALID_ARCHITECTURE_CONFIG",
                                f"tach.toml must set {key} = {value!r}.")
        return config

    @override
    def initialize(self, *, accept: bool = False, force: bool = False,
                   contract_only: bool = False
                   ) -> tuple[str, dict[str, Any]]:
        root = self._root
        path = root / "tach.toml"
        ready = root / ".quard/architecture.ready.json"
        if accept:
            config = self.validate()
            result = capture([sys.executable, str(tool_dir() / "_tach_worker.py"), "check"], root)
            normalized = normalize("tach", result, root)
            raw = root / ".quard/logs/init-check.log"
            atomic_write(raw, result["stdout"] + "\n" + result["stderr"], root)
            if normalized["status"] == "error":
                raise QuardError("TACH_CONFIG_OR_EXECUTION_ERROR",
                                f"Cannot validate Tach config. See {raw.relative_to(root)}.")
            ConfiguredCleanArchitectureLayers().sync(root, config)
            _ = configure(root)
            write_json(ready, {
                "accepted": True, "modules": len(config["modules"]),
                "note": "Acceptance of architecture configuration, NOT a violations baseline.",
            }, root)
            installation_path = root / ".quard/installation.json"
            if installation_path.exists():
                installation = read_json(installation_path)
                installation["architecture_initialization_required"] = False
                write_json(installation_path, installation, root)
            return "initialized", {
                "module_count": len(config["modules"]),
                "architecture_violations": len(normalized["diagnostics"]),
                "next_command": "uv run --locked --group dev python quard/check.py",
            }
        if path.exists() and not force:
            return "review_required", {
                "configuration": "tach.toml",
                "message": "Existing configuration retained. Review it and accept, "
                           + "or use --force to replace.",
                "next_command": (
                    "uv run --locked --group dev python quard/init_architecture.py --accept"),
            }
        config, notes = self.draft(contract_only=contract_only)
        atomic_write(path, "# Review before accepting; module boundaries are "
                     + "convention-based, not inferred communities.\n"
                     + tomlkit.dumps(config), root)
        if ready.exists():
            ready.unlink()
        if not contract_only:
            result = capture([tool_binary("tach"), "sync"], root)
            atomic_write(root / ".quard/logs/init-sync.log",
                         result["stdout"] + "\n" + result["stderr"], root)
            if result["error"] or result["exit_code"] != 0:
                raise QuardError("TACH_SYNC_FAILED", "Draft tach.toml exists but sync failed. "
                                + "See .quard/logs/init-sync.log. Do not mark it ready "
                                + "until corrected.")
        config = load_toml(path)
        ConfiguredCleanArchitectureLayers().sync(root, config)
        _ = configure(root)
        return "review_required", {
            "configuration": "tach.toml", "notes": notes,
            "next_command": "uv run --locked --group dev python quard/init_architecture.py --accept",
        }
