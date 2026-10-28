"""Standalone temporary-consumer and mocked-native fixtures for offline suites."""
from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import shutil
from typing import Any

import tomlkit

from quard import _common, _policy, init_architecture

QUARD = Path(__file__).resolve().parents[1]
CATALOG = {
    "message_ids": ["R0915", "R1260", "W9001", "W9003", "W9101", "W9102", "W9201",
                    "W9301", "W9501"],
    "plugins": ["pylint.extensions.mccabe", "pylint.extensions.broad_try_clause",
                "clean_architecture_linter"],
}

WORKERS = {
    "_lcom4_worker.py": "lcom4",
    "_method_decorators_worker.py": "method-decorators",
    "_instance_fields_worker.py": "instance-fields",
    "_class_contracts_worker.py": "class-contracts",
    "_dependency_types_worker.py": "dependency-types",
    "_contract_folders_worker.py": "contract-folders",
    "_test_placement_worker.py": "test-placement",
    "_outer_classes_worker.py": "outer-classes",
    "_free_function_placement_worker.py": "free-function-placement",
    "_leading_underscores_worker.py": "leading-underscores",
    "_tuple_signatures_worker.py": "tuple-signatures",
    "_stairfmt_worker.py": "stairfmt",
    "_cached_property_migration_worker.py": "cached-property-migration",
    "_encapsulated_only_worker.py": "encapsulated-only",
    "_constructor_purity_worker.py": "constructor-purity",
    "_lambda_injection_worker.py": "lambda-injection",
}

EMPTY_OUTPUT = {
    "pyright": '{"generalDiagnostics": []}',
    **{name: '{"quard_' + name.replace("-", "_") + '_adapter": 1, ' +
              '"diagnostics": [], "execution_error": null}'
       for name in WORKERS.values() if name != "encapsulated-only"},
    "encapsulated-only": '{"quard_encapsulated_only_adapter": 1, "diagnostics": [], ' +
                         '"execution_error": null, "declarations": [], "classes": [], ' +
                         '"effective_targets": {}}',
}


def create_repo(root: Path, configured: bool = True, ready: bool = True) -> None:
    (root / ".quard").mkdir()
    shutil.copy2(QUARD / "defaults/config.toml", root / ".quard/config.toml")
    setting = root / ".quard/config.toml"
    # Most offline fixtures exercise other jobs with explicit opt-outs.
    setting.write_text(setting.read_text(encoding="utf-8").replace(
        "dependency_types = true", "dependency_types = false").replace(
        "constructor_purity = true", "constructor_purity = false").replace(
        "tuple_signatures = true", "tuple_signatures = false").replace(
        "encapsulated_only = false", "encapsulated_only = true"), encoding="utf-8")
    (root / "src/demo/domain").mkdir(parents=True)
    (root / "src/demo/__init__.py").write_text(
        '"""Autonomous fixture.\n\nUsed by isolated checks.\nNo application imports.\n"""\n',
        encoding="utf-8")
    (root / "src/demo/domain/api.py").write_text(
        "def value() -> int:\n    number = 1\n    result = number + 1\n"
        "    output = result - 1\n    return output\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests/test_value.py").write_text(
        "def test_value() -> None:\n    assert 1 == 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\nrequires-python = ">=3.12,<3.15"\n' +
        'dependencies = []\n\n[tool.unrelated]\nvalue = "retain"\n', encoding="utf-8")
    _common.write_json(root / ".quard/pylint-catalog.json", CATALOG)
    if configured:
        _policy.configure(root, initial=True, plugins=CATALOG["plugins"], catalog=CATALOG)
    if ready:
        draft, _ = init_architecture.draft_configuration(root)
        _common.atomic_write(root / "tach.toml", tomlkit.dumps(draft))
        _common.write_json(root / ".quard/architecture.ready.json", {"accepted": True})


def native_result(argv: list[str], *, stdout: str = "", stderr: str = "",
                  code: int | None = 0, error: str | None = None) -> dict[str, Any]:
    return {"argv": argv, "stdout": stdout, "stderr": stderr, "exit_code": code,
            "error": error, "duration_seconds": 0.01}


def managed_bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and not any(part in {"logs", "cache", "reports", ".venv", ".git"}
                                          for part in path.relative_to(root).parts)}


def fake_tools(failures: dict[str, dict[str, Any]] | None = None
               ) -> tuple[Callable[[list[str], Path, int], dict[str, Any]], list[str]]:
    failures = failures or {}
    seen: list[str] = []

    def fake(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
        name = next((job for script, job in WORKERS.items() if str(QUARD / script) in argv), None)
        if name is None:
            if "--manifest" in argv:
                name = ("pylint-tests" if "tests" in argv[argv.index("--manifest") + 1]
                        else "pylint-production")
                output = Path(argv[argv.index("--output") + 1])
                _common.write_json(output, {"messages": []})
            elif "pyright" in argv:
                name = "pyright"
            elif Path(argv[0]).name == "pydoclint":
                name = "pydoclint"
            elif Path(argv[0]).name == "jscpd":
                kind = argv[argv.index("--kind") + 1]
                name = {"renamed": "jscpd-renamed", "similar": "jscpd-similar"}.get(
                    kind, "jscpd")
                output = Path(argv[argv.index("--output") + 1]) / "jscpd-report.json"
                min_lines = int(argv[argv.index("--min-lines") + 1])
                sources = sum(len((cwd / path).read_text(encoding="utf-8").splitlines()) >= min_lines
                              for path in argv[argv.index("--exit-code") + 2:])
                output.write_text(json.dumps({"duplicates": [], "statistics": {
                    "total": {"sources": sources, "clones": 0}}}), encoding="utf-8")
            else:
                name = "tach-external" if "check-external" in argv else "tach"
        seen.append(name)
        if name in failures:
            return native_result(argv, **failures[name])
        return native_result(argv, stdout=EMPTY_OUTPUT.get(name, ""))

    return fake, seen
