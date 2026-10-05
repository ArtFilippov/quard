#!/usr/bin/env python3
"""Installation-time probes against the actual installed native tools."""
from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import re
import sys
sys.dont_write_bytecode = True
import tempfile
from typing import Any

import tomlkit

from eryx._common import (EryxError, atomic_write, capture, emit_event, error_exit,
                      load_toml, read_json, require_runtime, project_dir, tool_binary,
                      tool_dir, write_json)
from eryx.consumer_policy.configured_consumer_policy import ConfiguredConsumerPolicy
from eryx._reports import normalize
from eryx.init_architecture import sync_clean_arch


def probe(argv: list[str], cwd: Path, label: str, log_root: Path) -> dict[str, Any]:
    result = capture(argv, cwd, timeout=300)
    atomic_write(log_root / f"{label}.stdout.log", result["stdout"])
    atomic_write(log_root / f"{label}.stderr.log", result["stderr"])
    if result["error"]:
        raise EryxError(result["error"], f"{label} failed; see .eryx/logs/install-probes/{label}.*.log")
    return result


def discover_plugins(root: Path, log_root: Path) -> tuple[list[str], dict[str, Any]]:
    base = [sys.executable, "-m", "pylint", "--rcfile=/dev/null"]
    result = probe(base + ["--list-extensions"], root, "pylint-extensions", log_root)
    if result["exit_code"] != 0:
        raise EryxError("PYLINT_DISCOVERY_FAILED", "Could not list native Pylint extensions.")
    extensions = sorted(set(re.findall(r"\bpylint\.extensions\.[A-Za-z][A-Za-z0-9_]*", result["stdout"])))
    if not {"pylint.extensions.mccabe", "pylint.extensions.broad_try_clause"}.issubset(extensions):
        raise EryxError("PYLINT_DISCOVERY_FAILED", "Required mccabe/broad_try_clause extensions were not discovered.")
    plugins = [name for name in extensions if name != "pylint.extensions.docparams"] + ["clean_architecture_linter"]
    result = probe(base + ["--load-plugins=" + ",".join(plugins), "--list-msgs"], root, "pylint-catalog", log_root)
    text = result["stdout"] + "\n" + result["stderr"]
    if result["exit_code"] != 0 or "Traceback (most recent call last)" in text:
        raise EryxError("PYLINT_PLUGIN_INCOMPATIBILITY", "The complete plugin set did not load. No checker has been silently dropped. See .eryx/logs/install-probes/pylint-catalog.*.log")
    pairs = re.findall(r"^:([a-z][a-z0-9-]+) \(([A-Z]\d{4})\)", result["stdout"], re.M)
    # Format tolerates indentation/leading colons in the native catalog.
    if not pairs:
        pairs = re.findall(r"([a-z][a-z0-9-]+) \(([A-Z]\d{4})\)", result["stdout"])
    ids = sorted({message_id for _, message_id in pairs})
    if not {"R0915", "R1260", "W9001", "W9201", "W9301"}.issubset(ids):
        raise EryxError("INCOMPLETE_CHECKER_CATALOG", "Required standard/architecture checker IDs are missing; inspect the installed plugin release.")
    catalog = {"plugins": plugins, "message_ids": ids,
               "messages": [{"id": code, "symbol": symbol} for symbol, code in sorted(set(pairs), key=lambda pair: pair[1])],
               "excluded_extension": "pylint.extensions.docparams", "excluded_reason": "message-ID collisions with clean_architecture_linter"}
    return plugins, catalog


def smoke_tests(root: Path, log_root: Path) -> dict[str, Any]:
    # These are installation probes, not a project baseline. Violations are intentional.
    with tempfile.TemporaryDirectory(prefix="eryx-probe-", dir=log_root) as tmp:
        fixture = Path(tmp)
        package = fixture / "src/eryxprobe"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "second.py").write_text("VALUE = 1\n", encoding="utf-8")
        source = package / "first.py"
        source.write_text('''import collections
from eryxprobe.second import VALUE


def identity(value: int) -> int:
    return value


def mismatched(value: int) -> int:
    """Return a number.

    Args:
        incorrect_name (int): Input value.

    Returns:
        int: The resulting value.
    """
    return "wrong"


class Probe:
    def __init__(self, value: int) -> None:
        self.value = value

    def get(self) -> int:
        return self.value + VALUE
''', encoding="utf-8")
        project = {"project": {"name": "eryxprobe", "version": "0.0.0", "requires-python": ">=3.12", "dependencies": []},
                   "tool": load_toml(root / "pyproject.toml")["tool"]}
        atomic_write(fixture / "pyproject.toml", tomlkit.dumps(project))
        manifest = fixture / "inputs.json"
        write_json(manifest, [source.relative_to(fixture).as_posix()])
        pylint_json = fixture / "pylint.json"
        result = probe([sys.executable, str(tool_dir() / "_pylint_worker.py"), "--manifest", str(manifest),
                        "--config", str(fixture / "pyproject.toml"), "--output", str(pylint_json)], fixture, "pylint-smoke", log_root)
        normalized = normalize("pylint", result, fixture, pylint_json)
        if normalized["status"] == "error" or not any(d["rule"] == "W0611" for d in normalized["diagnostics"]):
            raise EryxError("PYLINT_SMOKE_FAILED", "Configured Pylint/plugins failed on a small fixture. See .eryx/logs/install-probes/pylint-smoke.*.log")
        if any(d["rule"] in {"C0114", "C0115", "C0116"} for d in normalized["diagnostics"]):
            raise EryxError("DOCSTRING_POLICY_MISMATCH", "Pylint unexpectedly requires docstrings.")
        pyright = load_toml(root / "pyproject.toml")["tool"]["pyright"]
        pyright["include"] = ["src"]
        pyright["executionEnvironments"] = [{"root": "src", "extraPaths": ["src"]}]
        write_json(fixture / "pyrightconfig.json", pyright)
        result = probe([sys.executable, "-m", "pyright", "--project", str(fixture / "pyrightconfig.json"),
                        "--pythonpath", sys.executable, "--outputjson"], fixture, "pyright-smoke", log_root)
        normalized = normalize("pyright", result, fixture)
        if normalized["status"] == "error" or not any(d["rule"] == "reportReturnType" for d in normalized["diagnostics"]):
            raise EryxError("PYRIGHT_SMOKE_FAILED", "Pyright did not correctly report the intentional return-type mismatch, or rejected its config. See install-probes.")
        result = probe([tool_binary("pydoclint"), "--config", str(fixture / "pyproject.toml"), "--quiet",
                        "--show-filenames-in-every-violation-message=True", source.relative_to(fixture).as_posix()], fixture, "pydoclint-smoke", log_root)
        normalized = normalize("pydoclint", result, fixture)
        if normalized["status"] != "violations":
            raise EryxError("PYDOCLINT_SMOKE_FAILED", "pydoclint did not report the intentional docstring/signature mismatch, or rejected its config. See install-probes.")
        clone_text = ("def repeat(value):\n    first = value + 17\n    second = first * 3\n"
                      "    third = second - 4\n    fourth = third / 2\n    return fourth + 10\n")
        clone_paths = ["src/eryxprobe/clone_one.py", "src/eryxprobe/clone_two.py"]
        for path in clone_paths:
            (fixture / path).write_text(clone_text, encoding="utf-8")
        write_json(fixture / "jscpd-config.json", {})
        version = probe([tool_binary("jscpd"), "--version"], fixture, "jscpd-version", log_root)
        required = read_json(tool_dir() / "tools.json")["dependencies"]["jscpd"]
        if version["exit_code"] != 0 or version["stdout"].strip() != f"jscpd {required}":
            raise EryxError("JSCPD_VERSION_MISMATCH", "jscpd executable version does not match its installed package.")
        clone_json = fixture / "jscpd-report.json"
        result = probe([tool_binary("jscpd"), "--config", str(fixture / "jscpd-config.json"),
                        "--no-gitignore", "--absolute", "--kind", "exact", "--mode", "strict",
                        "--min-lines", "5", "--min-tokens", "1", "--fail-on-empty",
                        "--reporters", "json",
                        "--output", str(fixture), "--exit-code", "1", *clone_paths],
                       fixture, "jscpd-smoke", log_root)
        normalized = normalize("jscpd", result, fixture, clone_json, clone_paths)
        if normalized["status"] != "violations" or len(normalized["diagnostics"]) != 2:
            raise EryxError("JSCPD_SMOKE_FAILED", "jscpd did not detect both exact-clone fragments. See install-probes.")
        modules = [{"path": path, "depends_on": []} for path in ("eryxprobe", "eryxprobe.first", "eryxprobe.second")]
        tach = {"source_roots": ["src"], "root_module": "forbid", "exact": True,
                "respect_gitignore": False, "ignore_type_checking_imports": False, "modules": modules}
        atomic_write(fixture / "tach.toml", tomlkit.dumps(tach))
        result = probe([sys.executable, str(tool_dir() / "_tach_worker.py"), "check"], fixture, "tach-smoke", log_root)
        normalized = normalize("tach", result, fixture)
        if normalized["status"] != "violations":
            raise EryxError("TACH_SMOKE_FAILED", "Tach did not reject the intentionally undeclared module dependency, or its output could not be parsed. See install-probes.")
        result = probe([sys.executable, str(tool_dir() / "_tach_worker.py"), "check-external"], fixture, "tach-external-smoke", log_root)
        normalized = normalize("tach-external", result, fixture)
        if normalized["status"] != "passed":
            raise EryxError("TACH_EXTERNAL_SMOKE_FAILED", "The native external-dependency command failed on a project with no external imports. See install-probes.")
    return {"pylint_plugin_load": "passed", "pylint_config_and_fixture": "passed",
            "pyright_type_error": "detected", "pydoclint_signature_error": "detected",
            "tach_undeclared_dependency": "detected", "tach_external_command": "passed",
            "jscpd_exact_clone": "detected"}


def audit(root: Path) -> dict[str, Any]:
    require_runtime()
    log_root = root / ".eryx/logs/install-probes"
    log_root.mkdir(parents=True, exist_ok=True)
    pins = read_json(tool_dir() / "tools.json")["dependencies"]
    versions = {}
    for name, expected in pins.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise EryxError("MISSING_DEPENDENCY", f"{name} is not installed in the project environment.") from exc
        if actual != expected:
            raise EryxError("VERSION_MISMATCH", f"{name}: expected {expected}, installed {actual}")
        versions[name] = actual
    plugins, catalog = discover_plugins(root, log_root)
    write_json(root / ".eryx/pylint-catalog.json", catalog, root)
    changed = ConfiguredConsumerPolicy(root).apply(initial=True, plugins=plugins, catalog=catalog)
    if (root / ".eryx/architecture.ready.json").exists() and (root / "tach.toml").exists():
        sync_clean_arch(root, load_toml(root / "tach.toml"))
    results = smoke_tests(root, log_root)
    report = {"status": "passed", "versions": versions, "plugins": plugins, "message_count": len(catalog["message_ids"]),
              "smoke_tests": results, "modified_files": changed}
    write_json(root / ".eryx/logs/install-probes/result.json", report, root)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = audit(project_dir(args.project))
        emit_event("audited", json_output=args.json, **{key: value for key, value in result.items() if key != "status"}, exit_code=0)
        return 0
    except Exception as exc:
        return error_exit(exc, args.json)
