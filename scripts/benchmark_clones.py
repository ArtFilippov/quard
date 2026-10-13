"""Repeat the complete three-job CLI/Rust clone path on a public, fixed scope.

Run after the checkout's locked dev sync has built its local quard-clones wheel.
The pinned jscpd CLI is used here for historical comparison, not regular checks.
Outputs and the generated consumer live in a temporary directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quard.clone_engine_utils import installed_engine  # noqa: E402
from quard.report_utils import normalize  # noqa: E402

PUBLIC_SOURCES = (
    "src/quard/duplication_utils.py",
    "src/quard/clone_engine_utils.py",
    "src/quard/check_runner/runner_utils.py",
    "tests/quard/check_runner/contract_clone_utils.py",
)
KINDS = ("exact", "renamed", "similar")


def corpus(root: Path) -> list[str]:
    """Copy real public sources and add cross-profile behavior/declaration/gap cases."""
    paths = []
    for path in PUBLIC_SOURCES:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / path).read_bytes())
        paths.append(path)
    examples = {
        "src/sample/exact.py": """from typing import Protocol, TypedDict
class Order(Protocol):
    def total(self, value: int) -> int: ...
    def cancel(self, reason: str) -> None: ...
class Invoice(TypedDict):
    amount: int
    currency: str
    active: bool
def calculate(value: int) -> int:
    first = value + 17
    second = first * 3
    third = second - 4
    fourth = third / 2
    return int(fourth) + 10
""",
        "tests/sample/exact.py": """from typing import Protocol, TypedDict
class Receipt(Protocol):
    def total(self, value: int) -> int: ...
    def cancel(self, reason: str) -> None: ...
class Record(TypedDict):
    amount: int
    currency: str
    active: bool
def calculate(value: int) -> int:
    first = value + 17
    second = first * 3
    third = second - 4
    fourth = third / 2
    return int(fourth) + 10
""",
        "src/sample/renamed.py": """def transform(number: int) -> int:
    start = number + 17
    middle = start * 3
    end = middle - 4
    result = end / 2
    return int(result) + 10
""",
        "src/sample/gap.py": """def merge(value: int) -> int:
    alpha = value + 91
    beta = alpha * 8
    gamma = beta - 6
    delta = gamma / 9
    extra = 1
    left = value + 71
    right = left * 8
    total = right - 6
    adjusted = total / 9
    return int(adjusted)
""",
        "tests/sample/gap.py": """def merge(value: int) -> int:
    alpha = value + 91
    beta = alpha * 8
    gamma = beta - 6
    delta = gamma / 9
    extra = 2
    left = value + 71
    right = left * 8
    total = right - 6
    adjusted = total / 9
    return int(adjusted)
""",
        "src/sample/clean.py": """\"\"\"One ordinary source with no copied declaration or body.\"\"\"
import math
from collections import deque

class Tracker:
    \"\"\"A small unrelated source.\"\"\"
    note: str

    def scale(self, values: list[float]) -> float:
        \"\"\"Scale only nonzero values.\"\"\"
        selected = deque(number for number in values if number != 0.0)
        if not selected:
            return math.nan
        product = math.prod(selected)
        return product ** (1 / len(selected))
""",
    }
    for path, text in examples.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        paths.append(path)
    paths.sort()
    (root / ".quard").mkdir()
    (root / ".quard/config.toml").write_text("[duplication]\nmin_units = 3\n", encoding="utf-8")
    return paths


def invoke(command: list[str], root: Path) -> dict[str, object]:
    result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                            check=False, timeout=180)
    return {"exit_code": result.returncode, "stdout": result.stdout,
            "stderr": result.stderr, "error": None}


def measure(engine: str, root: Path, paths: list[str], directory: Path
            ) -> tuple[float, float, dict[str, int]]:
    """Time process startup, native reports, parsing, normalization and report writes."""
    directory.mkdir()
    config = directory / "config.json"
    config.write_text("{}\n", encoding="utf-8")
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps({"paths": paths, "min_units": 3}), encoding="utf-8")
    start = time.perf_counter()
    process_seconds = 0.0
    statuses = {}
    findings = {}
    if engine == "rust":
        command = [sys.executable, str(ROOT / "_clone_engine_worker.py"), "--manifest",
                   str(manifest), "--output", str(directory / "exact/jscpd-report.json"),
                   "--kind", "all"]
        process_start = time.perf_counter()
        batch = invoke(command, root)
        process_seconds += time.perf_counter() - process_start
        if batch["exit_code"] != 0:
            raise RuntimeError(str(batch))
        statuses = json.loads(str(batch["stdout"]))
        (directory / "batch.stdout.log").write_text(str(batch["stdout"]), encoding="utf-8")
        (directory / "batch.stderr.log").write_text(str(batch["stderr"]), encoding="utf-8")
    for kind in KINDS:
        output = directory / kind
        output.mkdir(exist_ok=True)
        if engine == "cli":
            flags = {"exact": [], "renamed": ["--ignore-identifiers", "--ignore-literals"],
                     "similar": ["--max-gap-lines", "2"]}[kind]
            process_start = time.perf_counter()
            result = invoke([str(ROOT / ".venv/bin/jscpd"), "--config", str(config),
                             "--no-gitignore", "--absolute", "--kind", kind, *flags,
                             "--mode", "strict", "--min-lines", "3", "--min-tokens", "1",
                             "--fail-on-empty", "--reporters", "json", "--output", str(output),
                             "--exit-code", "1", *paths], root)
            process_seconds += time.perf_counter() - process_start
        else:
            state = statuses[kind]
            result = {"exit_code": state["exit_code"], "stdout": "", "stderr": state["stderr"],
                      "error": None}
        if result["exit_code"] not in (0, 1):
            raise RuntimeError(f"{engine}/{kind}: {result}")
        native = (directory / "exact/jscpd-report.json" if engine == "rust" and kind == "exact"
                  else output / "jscpd-report.json")
        if engine == "rust" and kind != "exact":
            native = directory / "exact" / f"jscpd-{kind}" / "jscpd-report.json"
        parsed = normalize("jscpd", result, root, native, paths, engine == "rust", kind)
        if parsed["status"] == "error":
            raise RuntimeError(f"{engine}/{kind}: {parsed['diagnostics']}")
        findings[kind] = len(json.loads(native.read_text(encoding="utf-8"))["duplicates"])
        if not findings[kind] or parsed["status"] != "violations":
            raise RuntimeError(f"{engine}/{kind}: missing expected native findings")
        (output / "normalized.json").write_text(json.dumps(parsed), encoding="utf-8")
        (output / "stdout.log").write_text(str(result["stdout"]), encoding="utf-8")
        (output / "stderr.log").write_text(str(result["stderr"]), encoding="utf-8")
    return time.perf_counter() - start, process_seconds, findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    if args.runs < 3:
        parser.error("use at least three measured runs")
    versions = installed_engine()
    with tempfile.TemporaryDirectory(prefix="quard-clone-benchmark-") as scratch:
        root = Path(scratch) / "consumer"
        root.mkdir()
        paths = corpus(root)
        timings: dict[str, list[float]] = {"cli": [], "rust": []}
        processes: dict[str, list[float]] = {"cli": [], "rust": []}
        findings: dict[str, dict[str, int]] = {}
        # Warm both paths and alternate the measured order to reduce order bias.
        for index in range(args.runs + 1):
            for engine in (("cli", "rust") if index % 2 == 0 else ("rust", "cli")):
                duration, process, findings[engine] = measure(
                    engine, root, paths, Path(scratch) / f"{engine}-{index}")
                if index:
                    timings[engine].append(duration)
                    processes[engine].append(process)
        cli = statistics.median(timings["cli"])
        rust = statistics.median(timings["rust"])
        cpu = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text(
            encoding="utf-8").splitlines() if line.startswith("model name")), platform.processor())
        print(json.dumps({"host": platform.platform(), "cpu": cpu,
                          "python": sys.version.split()[0], "node": subprocess.check_output(
                              ["node", "--version"], text=True).strip(),
                          "jscpd": subprocess.check_output(
                              [str(ROOT / ".venv/bin/jscpd"), "--version"], text=True).strip(),
                          "native": versions,
                          "scope": paths, "files": len(paths), "bytes": sum(
                              (root / path).stat().st_size for path in paths),
                          "sha256": {path: hashlib.sha256((root / path).read_bytes()).hexdigest()
                                     for path in paths},
                          "minimum": 3, "seconds": {key: [round(value, 3) for value in samples]
                                                     for key, samples in timings.items()},
                          "process_seconds": {key: [round(value, 3) for value in samples]
                                              for key, samples in processes.items()},
                          "native_clones": findings,
                          "median_seconds": {"cli": round(cli, 3), "rust": round(rust, 3)},
                          "rust_over_cli": round(rust / cli, 3)}, indent=2))


if __name__ == "__main__":
    main()
