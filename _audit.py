#!/usr/bin/env python3
"""Installation-time probes against the actual installed native tools."""
from __future__ import annotations

import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import re
import sys
sys.dont_write_bytecode = True
import tempfile
from typing import Any

import tomlkit

from _common import (EryxError, atomic_write, capture, command_env, emit_event, error_exit,
                     load_toml, read_json, require_runtime, project_dir, tool_dir, write_json)
from _checks import ANONYMITY_DEPENDENCIES, PRESIDIO_DEPENDENCIES, ANONYMITY_PYLINT_CONFIG, read_checks
from _policy import configure
from _reports import normalize, normalize_presidio
from init_architecture import sync_clean_arch, tool_binary


def probe(argv: list[str], cwd: Path, label: str, log_root: Path,
          env: dict[str, str] | None = None, sensitive: bool = False) -> dict[str, Any]:
    result = capture(argv, cwd, timeout=300, env=env)
    if sensitive:
        result["stdout"] = "[Secret probe output withheld.]" if result["stdout"] else ""
        result["stderr"] = "[Secret probe output withheld.]" if result["stderr"] else ""
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
    plugins = [name for name in extensions if name != "pylint.extensions.docparams"]
    plugins += ["clean_architecture_linter", "_notebook_checker"]
    if "anonymity" in read_checks(root)["groups"]:
        plugins.append("_secret_checker")
    env = command_env(root)
    env["PYTHONPATH"] = str(tool_dir()) + os.pathsep + env["PYTHONPATH"]
    result = probe(base + ["--load-plugins=" + ",".join(plugins), "--list-msgs"],
                   root, "pylint-catalog", log_root, env=env)
    text = result["stdout"] + "\n" + result["stderr"]
    if result["exit_code"] != 0 or "Traceback (most recent call last)" in text:
        raise EryxError("PYLINT_PLUGIN_INCOMPATIBILITY", "The complete plugin set did not load. No checker has been silently dropped. See .eryx/logs/install-probes/pylint-catalog.*.log")
    pairs = re.findall(r"^:([a-z][a-z0-9-]+) \(([A-Z]\d{4})\)", result["stdout"], re.M)
    # Format tolerates indentation/leading colons in the native catalog.
    if not pairs:
        pairs = re.findall(r"([a-z][a-z0-9-]+) \(([A-Z]\d{4})\)", result["stdout"])
    ids = sorted({message_id for _, message_id in pairs})
    if not {"R0915", "R1260", "W9001", "W9201", "W9301", "W9801", "F9802"}.issubset(ids):
        raise EryxError("INCOMPLETE_CHECKER_CATALOG", "Required standard/architecture checker IDs are missing; inspect the installed plugin release.")
    if "anonymity" in read_checks(root)["groups"] and not {"W9803", "F9804"}.issubset(ids):
        raise EryxError("INCOMPLETE_CHECKER_CATALOG", "Required secret checker IDs are missing; inspect the installed plugin release.")
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
        notebook = fixture / "smoke.ipynb"
        notebook.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {},
                                        "cells": [{"cell_type": "code", "id": "smoke", "metadata": {},
                                                   "source": "print(1)", "execution_count": 1,
                                                   "outputs": [{"output_type": "stream", "name": "stdout",
                                                                "text": "1\n"}]}]}), encoding="utf-8")
        original_notebook = notebook.read_bytes()
        notebook_manifest = fixture / "notebooks.json"
        write_json(notebook_manifest, [notebook.name])
        secret = "AKIAIOSFODNN7EXAMPLE"
        secret_file = fixture / "settings.txt"
        secret_file.write_text(f"label = 'safe'\naws_access_key_id = '{secret}'\n", encoding="utf-8")
        original_secret = secret_file.read_bytes()
        secret_manifest = fixture / "secrets.json"
        secret_coverage = fixture / "secret-coverage.json"
        write_json(secret_manifest, [secret_file.name])
        check_secrets = "anonymity" in read_checks(root)["groups"]
        pylint_json = fixture / "pylint.json"
        result = probe([sys.executable, str(tool_dir() / "_pylint_worker.py"), "--manifest", str(manifest),
                          "--config", str(fixture / "pyproject.toml"), "--output", str(pylint_json),
                          "--notebooks", str(notebook_manifest),
                          *(["--secrets", str(secret_manifest), "--secret-coverage", str(secret_coverage)]
                            if check_secrets else [])],
                       fixture, "pylint-smoke", log_root, sensitive=check_secrets)
        normalized = normalize("pylint", result, fixture, pylint_json)
        if normalized["status"] == "error" or not any(d["rule"] == "W0611" for d in normalized["diagnostics"]):
            raise EryxError("PYLINT_SMOKE_FAILED", "Configured Pylint/plugins failed on a small fixture. See .eryx/logs/install-probes/pylint-smoke.*.log")
        if (not any(d["rule"] == "W9801" and d["path"] == notebook.name for d in normalized["diagnostics"])
                or notebook.read_bytes() != original_notebook):
            raise EryxError("PYLINT_SMOKE_FAILED", "Notebook checker did not diagnose the unstripped notebook without changing it. See install-probes.")
        if check_secrets:
            try:
                covered = read_json(secret_coverage)
                native = pylint_json.read_text(encoding="utf-8")
            except (EryxError, OSError):
                covered, native = None, ""
            if (not any(d["rule"] == "W9803" and d["path"] == secret_file.name and d["line"] == 2
                        for d in normalized["diagnostics"]) or
                    covered != {"scanned": [secret_file.name], "skipped": []} or
                    secret in json.dumps(normalized) + native or result["stdout"] or result["stderr"] or
                    secret_file.read_bytes() != original_secret):
                raise EryxError("PYLINT_SECRET_SMOKE_FAILED",
                                "Configured secret checker failed its fixture or exposed its value; see install-probes.")
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
            "pylint_notebook": "detected",
            **({"pylint_secret": "detected"} if check_secrets else {}),
            "pyright_type_error": "detected", "pydoclint_signature_error": "detected",
            "tach_undeclared_dependency": "detected", "tach_external_command": "passed"}


def smoke_anonymity(log_root: Path) -> dict[str, str]:
    """Probe the actual notebook checker without loading code-style plugins."""
    with tempfile.TemporaryDirectory(prefix="eryx-anonymity-probe-", dir=log_root) as tmp:
        fixture = Path(tmp)
        notebook = fixture / "unstripped.ipynb"
        notebook.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {},
                                        "cells": [{"cell_type": "code", "id": "fixture", "metadata": {},
                                                   "source": "print(1)", "execution_count": 1,
                                                   "outputs": [{"output_type": "stream", "name": "stdout", "text": "1\n"}]}]}),
                            encoding="utf-8")
        original = notebook.read_bytes()
        (fixture / "input.py").write_text("", encoding="utf-8")
        write_json(fixture / "inputs.json", ["input.py"])
        write_json(fixture / "notebooks.json", [notebook.name])
        atomic_write(fixture / "anonymity-pylint.toml", ANONYMITY_PYLINT_CONFIG)
        output = fixture / "pylint.json"
        result = probe([sys.executable, str(tool_dir() / "_pylint_worker.py"),
                        "--manifest", str(fixture / "inputs.json"),
                        "--config", str(fixture / "anonymity-pylint.toml"),
                        "--output", str(output), "--notebooks", str(fixture / "notebooks.json"),
                        "--notebook-only"],
                       fixture, "pylint-anonymity-smoke", log_root)
        normalized = normalize("pylint", result, fixture, output)
        if (normalized["status"] != "violations" or
                not any(d["rule"] == "W9801" and d["path"] == notebook.name for d in normalized["diagnostics"]) or
                notebook.read_bytes() != original):
            raise EryxError("PYLINT_SMOKE_FAILED", "Notebook checker failed its independent fixture; see install-probes.")
    return {"pylint_notebook": "detected", **smoke_secret(log_root)}


def smoke_secret(log_root: Path) -> dict[str, str]:
    """Require the real installed checker to detect a secret without disclosing it."""
    with tempfile.TemporaryDirectory(prefix="eryx-secret-probe-", dir=log_root) as tmp:
        fixture = Path(tmp)
        secret = "AKIAIOSFODNN7EXAMPLE"
        source = fixture / "settings.txt"
        source.write_text(f"label = 'safe'\naws_access_key_id = '{secret}'\n", encoding="utf-8")
        original = source.read_bytes()
        (fixture / "input.py").write_text("", encoding="utf-8")
        write_json(fixture / "inputs.json", ["input.py"])
        write_json(fixture / "secrets.json", [source.name])
        atomic_write(fixture / "anonymity-pylint.toml", ANONYMITY_PYLINT_CONFIG)
        output = fixture / "pylint.json"
        coverage = fixture / "secret-coverage.json"
        result = probe([sys.executable, str(tool_dir() / "_pylint_worker.py"),
                        "--manifest", str(fixture / "inputs.json"),
                        "--config", str(fixture / "anonymity-pylint.toml"),
                        "--output", str(output), "--secrets", str(fixture / "secrets.json"),
                        "--secret-coverage", str(coverage), "--notebook-only"],
                       fixture, "pylint-secret-smoke", log_root, sensitive=True)
        normalized = normalize("pylint", result, fixture, output)
        try:
            covered = read_json(coverage)
            native = output.read_text(encoding="utf-8")
        except (EryxError, OSError):
            covered, native = None, ""
        if (normalized["status"] != "violations" or result["exit_code"] != 4 or
                [(item["rule"], item["path"], item["line"]) for item in normalized["diagnostics"]]
                != [("W9803", source.name, 2)] or
                covered != {"scanned": [source.name], "skipped": []} or
                secret in json.dumps(normalized) + native or result["stdout"] or result["stderr"] or
                source.read_bytes() != original):
            raise EryxError("PYLINT_SECRET_SMOKE_FAILED",
                            "Secret checker failed its independent fixture or exposed its value; see install-probes.")
    return {"pylint_secret": "detected"}


def smoke_presidio(log_root: Path) -> dict[str, str]:
    """Probe both English entities with the installed model and no input rewrites."""
    with tempfile.TemporaryDirectory(prefix="eryx-presidio-probe-", dir=log_root) as tmp:
        fixture = Path(tmp)
        person = "Dr. John Smith"
        email = "john.smith@example.com"
        source = fixture / "sample.txt"
        source.write_text(f"Contact: {person}\nEmail: {email}\n", encoding="utf-8")
        original = source.read_bytes()
        write_json(fixture / "manifest.json", [source.name])
        output = fixture / "presidio.json"
        result = probe([sys.executable, str(tool_dir() / "_presidio_worker.py"),
                        "--manifest", str(fixture / "manifest.json"), "--output", str(output)],
                       fixture, "presidio-smoke", log_root, sensitive=True)
        normalized = normalize_presidio(result, fixture, output, [source.name])
        try:
            native = output.read_text(encoding="utf-8")
        except OSError:
            native = ""
        found = {(d["rule"], d["line"]) for d in normalized["diagnostics"]}
        if (normalized["status"] != "violations" or result["exit_code"] != 1 or
                not {("PERSON", 1), ("EMAIL_ADDRESS", 2)} <= found or
                normalized["coverage"] != {"scanned": [source.name], "skipped": []} or
                person in native + json.dumps(normalized) or email in native + json.dumps(normalized) or
                source.read_bytes() != original):
            raise EryxError("PRESIDIO_SMOKE_FAILED", "Presidio did not safely detect both English entities; see install-probes.")
    return {"presidio_person": "detected", "presidio_email": "detected"}


def audit(root: Path) -> dict[str, Any]:
    require_runtime()
    checks = read_checks(root)
    log_root = root / ".eryx/logs/install-probes"
    log_root.mkdir(parents=True, exist_ok=True)
    pins = read_json(tool_dir() / "tools.json")["dependencies"]
    pins = {key: value for key, value in pins.items()
            if ("codestyle" in checks["groups"] or key in ANONYMITY_DEPENDENCIES) and
            ("anonymity" in checks["groups"] or key not in PRESIDIO_DEPENDENCIES)}
    versions = {}
    for name, expected in pins.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise EryxError("MISSING_DEPENDENCY", f"{name} is not installed in the project environment.") from exc
        if actual != expected:
            raise EryxError("VERSION_MISMATCH", f"{name}: expected {expected}, installed {actual}")
        versions[name] = actual
    if "codestyle" in checks["groups"]:
        plugins, catalog = discover_plugins(root, log_root)
        write_json(root / ".eryx/pylint-catalog.json", catalog, root)
        changed = configure(root, initial=True, plugins=plugins, catalog=catalog)
        if (root / ".eryx/architecture.ready.json").exists() and (root / "tach.toml").exists():
            sync_clean_arch(root, load_toml(root / "tach.toml"))
        results = smoke_tests(root, log_root)
    else:
        plugins = ["_notebook_checker", "_secret_checker"]
        catalog, changed = {"message_ids": ["W9801", "F9802", "W9803", "F9804"]}, []
        results = smoke_anonymity(log_root)
    if "anonymity" in checks["groups"]:
        results.update(smoke_presidio(log_root))
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


if __name__ == "__main__":
    sys.exit(main())
