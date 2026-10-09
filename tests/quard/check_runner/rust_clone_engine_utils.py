"""Build a real wheel and inspect prepared full-check results."""
from __future__ import annotations

import atexit
from collections.abc import Callable
from functools import cache
from importlib import util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import mkdtemp
from typing import Any, cast
import unittest
from unittest.mock import patch

from offline_fixtures import fake_tools
from quard._common import capture, read_json
from quard.check_runner.native_check_runner import NativeCheckRunner
from quard.report_utils import normalize

QUARD = Path(__file__).resolve().parents[3]
SAMPLE = ("def repeat(value):\n    first = value + 17\n    second = first * 3\n"
          "    third = second - 4\n    fourth = third / 2\n    return fourth + 10\n")


def adoption_consumer(root: Path) -> None:
    """Load the shared public consumer fixture from its own test directory.

    Args:
        root (Path): Selected isolated consumer root.

    Raises:
        RuntimeError: The shared public fixture is unavailable.
    """
    spec = util.spec_from_file_location("adoption_fixture_utils", QUARD /
                                        "tests/quard/architecture_draft/adoption_fixture_utils.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Public adoption fixture could not be loaded")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    create = cast(Callable[[Path], None], getattr(module, "adoption_consumer"))
    create(root)


@cache
def wheel() -> tuple[Path, Path]:
    """Build and install one real wheel for the current test process.

    Returns:
        tuple[Path, Path]: Isolated Python interpreter and built wheel artifact.

    Raises:
        RuntimeError: No Rust developer toolchain is available.
        AssertionError: Wheel build or linked version evidence is incomplete.
    """
    cargo = shutil.which("cargo") or "/tmp/opencode/cargo/bin/cargo"
    if not Path(cargo).is_file():
        raise RuntimeError("Rust developer toolchain is required for real wheel verification")
    base = Path(mkdtemp(prefix="quard-real-wheel-", dir="/tmp/opencode"))
    _ = atexit.register(shutil.rmtree, base)
    env = os.environ.copy()
    env["PATH"] = str(Path(cargo).parent) + os.pathsep + env.get("PATH", "")
    if cargo.startswith("/tmp/opencode/"):
        env["CARGO_HOME"] = "/tmp/opencode/cargo"
        env["RUSTUP_HOME"] = "/tmp/opencode/rustup"
    env["CARGO_TARGET_DIR"] = str(QUARD / "rust/quard-clones/target")
    build = subprocess.run(["uvx", "--from", "maturin==1.9.6", "maturin", "build",
                     "--locked", "--release", "--manifest-path",
                     str(QUARD / "rust/quard-clones/Cargo.toml"), "--interpreter",
                     sys.executable, "--out", str(base)], env=env, check=False,
                    capture_output=True, text=True, timeout=300)
    if build.returncode:
        raise AssertionError(build.stderr)
    artifacts = list(base.glob("quard_clones-*.whl"))
    if len(artifacts) != 1:
        raise AssertionError(f"Expected one real wheel, got {artifacts}")
    python = base / "venv/bin/python"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(base / "venv")],
                   check=True, capture_output=True, text=True)
    subprocess.run(["uv", "pip", "install", "--python", str(python), str(artifacts[0])],
                   check=True, capture_output=True, text=True)
    installed = subprocess.run([str(python), "-c",
                                "import quard_clones; print(dict(quard_clones.versions()))"],
                               check=True, capture_output=True, text=True)
    if "cpd-core" not in installed.stdout:
        raise AssertionError("The real wheel did not expose linked engine versions")
    return python, artifacts[0]


def versions(_root: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Supply empty version evidence only for mocked peer jobs in focused tests.

    Args:
        _root (Path): Ignored temporary consumer root.

    Returns:
        tuple[dict[str, str], list[dict[str, Any]]]: Empty mock version evidence.
    """
    return {}, []


def report(root: Path) -> dict[str, Any]:
    """Run the real wheel exact pass at the ordinary check-runner seam.

    Args:
        root (Path): Discovered fixture consumer root.

    Returns:
        dict[str, Any]: Full check report with other jobs mocked.
    """
    fake, _ = fake_tools()
    python, _ = wheel()

    def dispatch(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
        if Path(argv[1]).name == "_clone_engine_worker.py":
            return capture([str(python), *argv[1:]], cwd, timeout)
        return fake(argv, cwd, timeout)

    with patch.dict(os.environ, {"QUARD_CLONE_ENGINE": "rust"}):
        result = NativeCheckRunner().run(root, lcom_files="", serial=True, timeout=60,
                                         capture_fn=dispatch, versions_fn=versions)
    for job in result["tools"]:
        if job["name"] in {"jscpd", "jscpd-renamed", "jscpd-similar"}:
            batch = root / job["raw_output"]["batch_stdout"]
            if (not batch.is_file() or
                    (job["status"] != "error" and
                     "exact" not in batch.read_text(encoding="utf-8"))):
                raise AssertionError("Missing archived Rust batch status stream")
    return result


def prepared_native(test: unittest.TestCase, root: Path, result: dict[str, Any], minimum: int,
                kind: str = "exact"
                ) -> dict[str, Any]:
    """Check prepared native output and discovered source accounting.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Fixture consumer root.
        result (dict[str, Any]): Full check report.
        minimum (int): Effective configured minimum.
        kind (str, default="exact"): Independent native clone kind.

    Returns:
        dict[str, Any]: Archived native Rust JSON data.
    """
    name = {"exact": "jscpd", "renamed": "jscpd-renamed",
            "similar": "jscpd-similar"}[kind]
    job = next(item for item in result["tools"] if item["name"] == name)
    native = read_json(root / job["raw_output"]["native_json"])
    manifest = read_json((root / result["report_path"]).parent / "clone-engine-files.json")
    test.assertEqual(manifest["min_lines"], minimum)
    test.assertEqual(native["sourcePaths"], [str(root / path) for path in manifest["paths"]])
    test.assertEqual(native["statistics"]["total"]["sources"], len(manifest["paths"]))
    test.assertEqual(native["statistics"]["total"]["clones"], len(native["duplicates"]))
    test.assertEqual(job["exit_code"], int(bool(native["duplicates"])))
    return native


def verify_prepared_similar(test: unittest.TestCase, root: Path) -> None:
    """Real prepared gap and clean scans keep one function's behavior.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Isolated fixture consumer root.
    """
    top = ("    alpha = value + 17\n    beta = alpha * 3\n"
           "    gamma = beta - 4\n    delta = gamma / 2\n")
    bottom = ("    left = value + 71\n    right = left * 8\n"
              "    total = right - 6\n    adjusted = total / 9\n")
    first = root / "src/demo/first.py"
    second = root / "tests/second.py"
    for gap in (0, 1, 2, 5):
        with test.subTest(gap=gap):
            first.write_text("def calculate(value):\n" + top +
                             bottom + "    return adjusted\n", encoding="utf-8")
            second.write_text("def calculate(value):\n" + top +
                              "".join(f"    inserted_{n} = {n}\n" for n in range(gap)) +
                              bottom + "    return adjusted\n",
                              encoding="utf-8")
            result = report(root)
            job = next(j for j in result["tools"] if j["name"] == "jscpd-similar")
            native = prepared_native(test, root, result, 3, "similar")
            test.assertIn("_clone_engine_worker.py", job["argv"][1])
            test.assertEqual(job["status"], "violations" if 1 <= gap <= 2 else "passed")
            test.assertEqual(bool(native["duplicates"]), 1 <= gap <= 2)
            if 1 <= gap <= 2:
                test.assertTrue(all(item["kind"] == "similar" and item["method"] == "gap"
                                    and 0 < item["similarity"] < 1
                                    for item in native["duplicates"]))
                test.assertEqual({d["path"] for d in result["diagnostics"]
                                  if d["rule"] == "SIMILAR_CLONE"},
                                 {"src/demo/first.py", "tests/second.py"})
                test.assertTrue(all(d["native_details"] in native["duplicates"]
                                    and d["line"] >= 1 and d["column"] >= 1
                                    for d in result["diagnostics"]
                                    if d["rule"] == "SIMILAR_CLONE"))
    setting = root / ".quard/config.toml"
    setting.write_text(setting.read_text(encoding="utf-8").replace(
        "min_lines = 3", "min_lines = 6"), encoding="utf-8")
    second.write_text("def calculate(value):\n" + top + "    inserted = 91\n" +
                      bottom + "    return adjusted\n", encoding="utf-8")
    insufficient = report(root)
    test.assertFalse(prepared_native(test, root, insufficient, 6, "similar")["duplicates"])
    test.assertEqual(next(j for j in insufficient["tools"] if j["name"] == "jscpd-similar")[
        "status"], "passed")
    setting.write_text(setting.read_text(encoding="utf-8").replace(
        "min_lines = 6", "min_lines = 3"), encoding="utf-8")
    second.unlink()
    first.write_text("def unique(value):\n    return value + 53\n", encoding="utf-8")
    clean = report(root)
    test.assertEqual(next(j for j in clean["tools"] if j["name"] == "jscpd-similar")[
        "status"], "passed", clean["diagnostics"])
    test.assertFalse(prepared_native(test, root, clean, 3, "similar")["duplicates"])


def verify_similar_failure(test: unittest.TestCase, root: Path) -> None:
    """Malformed native gap data or failed execution cannot hide peer results.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Isolated fixture consumer root.
    """
    (root / "src/demo/first.py").write_text(SAMPLE, encoding="utf-8")
    (root / "tests/second.py").write_text(SAMPLE, encoding="utf-8")
    fake, _ = fake_tools({"pydoclint": {"stderr": "src/demo/first.py:1: DOC101: Issue\n",
                                        "code": 1}})
    python, _ = wheel()

    def dispatch(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
        if Path(argv[1]).name != "_clone_engine_worker.py":
            return fake(argv, cwd, timeout)
        result = capture([str(python), *argv[1:]], cwd, timeout)
        if argv[-1] == "all" and result["exit_code"] == 0:
            statuses = json.loads(result["stdout"])
            statuses["similar"] = {"exit_code": 2, "stderr": "native similar failure"}
            return {**result, "stdout": json.dumps(statuses)}
        return result

    with patch.dict(os.environ, {"QUARD_CLONE_ENGINE": "rust"}):
        result = NativeCheckRunner().run(root, lcom_files="", serial=True, timeout=60,
                                         capture_fn=dispatch, versions_fn=versions)
    jobs = {j["name"]: j for j in result["tools"]}
    test.assertEqual((result["exit_code"], jobs["jscpd"]["status"],
                      jobs["jscpd-renamed"]["status"], jobs["jscpd-similar"]["status"]),
                     (2, "violations", "passed", "error"))
    test.assertTrue({"EXACT_CLONE", "DOC101"} <= {d["rule"] for d in result["diagnostics"]})
    test.assertTrue(any(d["is_infrastructure_error"] and d["job"] == "jscpd-similar"
                        for d in result["diagnostics"]))
    native = read_json(root / jobs["jscpd-similar"]["raw_output"]["native_json"])
    test.assertEqual(native["sourcePaths"], [str(root / path) for path in read_json(
        (root / result["report_path"]).parent / "clone-engine-files.json")["paths"]])
    # A real gap finding supplies valid metadata and locations before corruption.
    first = root / "src/demo/first.py"
    second = root / "tests/second.py"
    top = ("def calculate(value):\n    alpha = value + 17\n    beta = alpha * 3\n"
           "    gamma = beta - 4\n    delta = gamma / 2\n")
    bottom = ("    left = value + 71\n    right = left * 8\n"
              "    total = right - 6\n    adjusted = total / 9\n")
    first.write_text(top + "    extra = 1\n" + bottom + "    return adjusted\n",
                     encoding="utf-8")
    second.write_text(top + "    extra = 2\n" + bottom + "    return adjusted\n",
                      encoding="utf-8")
    valid = report(root)
    similar = next(j for j in valid["tools"] if j["name"] == "jscpd-similar")
    payload = read_json(root / similar["raw_output"]["native_json"])
    test.assertTrue(payload["duplicates"], payload)
    paths = read_json((root / valid["report_path"]).parent / "clone-engine-files.json")["paths"]
    output = root / "invalid-gap.json"
    for key, bad_value in (("similarity", None), ("similarity", math.nan),
                           ("similarity", -0.1), ("similarity", 1.5),
                           ("method", "ast"), ("unmatchedLines", [999, 0])):
        with test.subTest(key=key, value=bad_value):
            altered = json.loads(json.dumps(payload))
            altered["duplicates"][0][key] = bad_value
            output.write_text(json.dumps(altered), encoding="utf-8")
            parsed = normalize("jscpd", {"exit_code": 1, "stdout": "", "stderr": "",
                                         "error": None}, root, output, paths, True, "similar")
            test.assertEqual(parsed["status"], "error")
            test.assertTrue(any(d["is_infrastructure_error"] for d in parsed["diagnostics"]))
    altered = json.loads(json.dumps(payload))
    altered["duplicates"][0]["secondFile"]["endLoc"]["position"] += 1
    output.write_text(json.dumps(altered), encoding="utf-8")
    test.assertEqual(normalize("jscpd", {"exit_code": 1, "stdout": "", "stderr": "",
                                         "error": None}, root, output, paths, True,
                               "similar")["status"], "error")


def verify_prepared_scope(test: unittest.TestCase, root: Path) -> None:
    """Check cross-profile clones, clean scans, ignored folders and stubs.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Isolated consumer root.
    """
    setting = root / ".quard/config.toml"
    setting.write_text(setting.read_text(encoding="utf-8").replace(
        "min_lines = 3", "min_lines = 5"), encoding="utf-8")
    (root / "src/demo/first.py").write_text(SAMPLE, encoding="utf-8")
    (root / "tests/second.py").write_text(SAMPLE, encoding="utf-8")
    (root / "src/demo/companion.pyi").write_text(SAMPLE, encoding="utf-8")
    excluded = root / "src/demo/generated"
    excluded.mkdir()
    (excluded / "hidden.py").write_text(SAMPLE, encoding="utf-8")
    for clean in (False, True):
        with test.subTest(clean=clean):
            if clean:
                (root / "tests/second.py").unlink()
                (root / "src/demo/first.py").write_text("unique = object()\n", encoding="utf-8")
            result = report(root)
            exact = next(job for job in result["tools"] if job["name"] == "jscpd")
            test.assertEqual((result["exit_code"], exact["status"], exact["exit_code"]),
                             (0, "skipped", None) if clean else (1, "violations", 1),
                             result["diagnostics"])
            if not clean:
                _ = prepared_native(test, root, result, 5)
                _ = prepared_native(test, root, result, 5, "renamed")
            test.assertEqual({d["path"] for d in result["diagnostics"] if d["tool"] == "jscpd"},
                             set() if clean else {"src/demo/first.py", "tests/second.py"})


def verify_corrupt_report(test: unittest.TestCase, root: Path) -> None:
    """A real engine scan with broken accounting fails without hiding a peer.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Isolated consumer root.
    """
    text = ("def repeat(value):\n    first = value + 17\n"
            "    second = first * 3\n    third = second - 4\n")
    (root / "src/demo/first.py").write_text(text, encoding="utf-8")
    (root / "tests/second.py").write_text(text, encoding="utf-8")
    fake, _ = fake_tools({"pydoclint": {"stderr": "src/demo/first.py:1: DOC101: Native issue\n",
                                       "code": 1}})
    python, _ = wheel()

    def corrupt(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
        if Path(argv[1]).name == "_clone_engine_worker.py":
            native_result = capture([str(python), *argv[1:]], cwd, timeout)
            test.assertEqual(native_result["exit_code"], 0, native_result["stderr"])
            output = Path(argv[argv.index("--output") + 1])
            payload = read_json(output)
            payload["statistics"]["total"]["sources"] = 0
            output.write_text(json.dumps(payload), encoding="utf-8")
            return native_result
        return fake(argv, cwd, timeout)

    with patch.dict(os.environ, {"QUARD_CLONE_ENGINE": "rust"}):
        result = NativeCheckRunner().run(root, lcom_files="", serial=True, timeout=30,
                                         capture_fn=corrupt, versions_fn=versions)
    test.assertEqual(result["exit_code"], 2)
    test.assertEqual(next(j for j in result["tools"] if j["name"] == "jscpd")["status"], "error")
    test.assertIn("UNREADABLE_TOOL_OUTPUT", {d["rule"] for d in result["diagnostics"]})
    test.assertIn("DOC101", {d["rule"] for d in result["diagnostics"]})


def verify_missing_wheel(test: unittest.TestCase, root: Path) -> None:
    """A consumer interpreter without the wheel gets an explicit failure.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Isolated consumer root.
    """
    clean = root / ".quard/without-wheel"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(clean)],
                   check=True, capture_output=True, text=True)
    (root / "src/demo/first.py").write_text("one = 1\ntwo = one + 2\nthree = two * 3\n",
                                             encoding="utf-8")
    manifest = root / ".quard/clone-manifest.json"
    manifest.write_text(json.dumps({"paths": ["src/demo/first.py"], "min_lines": 3}),
                        encoding="utf-8")
    for kind in ("exact", "renamed"):
        result = capture([str(clean / "bin/python"), str(QUARD / "_clone_engine_worker.py"),
                          "--manifest", str(manifest), "--output",
                          str(root / ".quard/native.json"), "--kind", kind], root, 30)
        test.assertEqual(result["exit_code"], 2)
        test.assertIn("CLONE_ENGINE_ERROR", result["stderr"])
        test.assertFalse((root / ".quard/native.json").exists())


def verify_mismatched_engine(test: unittest.TestCase, root: Path) -> None:
    """Reject real wheel bindings when the required linked version changes.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Isolated consumer root.
    """
    (root / "src/demo/first.py").write_text(SAMPLE, encoding="utf-8")
    manifest = root / ".quard/clone-manifest.json"
    manifest.write_text(json.dumps({"paths": ["src/demo/first.py"], "min_lines": 3}),
                        encoding="utf-8")
    output = root / ".quard/native.json"
    python, _ = wheel()
    script = ("import sys\nsys.path.insert(0, sys.argv[1])\n"
              "from quard import clone_engine_utils as engine\n"
              "engine.EXPECTED['cpd-core'] = '0.0.0'\n"
               "sys.argv = ['engine', '--manifest', sys.argv[2], '--output', sys.argv[3], "
               "'--kind', sys.argv[4]]\n"
              "try:\n    engine.main()\n"
              "except RuntimeError as exc:\n    print(exc, file=sys.stderr)\n"
              "    raise SystemExit(2) from None\n")
    for kind in ("exact", "renamed"):
        result = capture([str(python), "-c", script, str(QUARD / "src"), str(manifest),
                          str(output), kind], root, 30)
        test.assertEqual(result["exit_code"], 2)
        test.assertIn("linked engine library version mismatch", result["stderr"])
        test.assertFalse(output.exists())
        parsed = normalize("jscpd", result, root, output, ["src/demo/first.py"], True, kind)
        test.assertEqual(parsed["status"], "error")
        test.assertTrue(any(d["is_infrastructure_error"] for d in parsed["diagnostics"]))


def verify_same_file_and_mapping(test: unittest.TestCase, root: Path) -> None:
    """Same-file prepared fragments preserve originals; invalid mappings fail.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Isolated consumer root.
    """
    (root / "src/demo/first.py").write_text(SAMPLE + "\n# separation\n" + SAMPLE,
                                             encoding="utf-8")
    result = report(root)
    exact = next(job for job in result["tools"] if job["name"] == "jscpd")
    test.assertEqual((exact["status"], exact["exit_code"]), ("violations", 1),
                      (result["diagnostics"], (root / exact["raw_output"]["stderr"]).read_text()))
    test.assertEqual({d["path"] for d in result["diagnostics"] if d["tool"] == "jscpd"},
                     {"src/demo/first.py"})
    native = prepared_native(test, root, result, 3)
    test.assertEqual(native["statistics"]["total"]["clones"], len(native["duplicates"]))
    native["duplicates"][0]["fragment"] = "invented source"
    bad = root / "altered.json"
    bad.write_text(json.dumps(native), encoding="utf-8")
    run_dir = root / result["report_path"].replace("/report.json", "")
    paths = read_json(run_dir / "clone-engine-files.json")["paths"]
    invalid = normalize("jscpd", {"exit_code": 1, "stdout": "", "stderr": "", "error": None},
                        root, bad, paths, True)
    test.assertEqual(invalid["status"], "error")
    test.assertTrue(any(d["is_infrastructure_error"] for d in invalid["diagnostics"]))
    native["duplicates"][0]["fragment"] = SAMPLE.strip()
    native["duplicates"][0]["firstFile"]["startLoc"]["position"] += 1
    bad.write_text(json.dumps(native), encoding="utf-8")
    wrong_location = normalize("jscpd", {"exit_code": 1, "stdout": "", "stderr": "",
                                          "error": None}, root, bad, paths, True)
    test.assertEqual(wrong_location["status"], "error")
    for field in ("unitLines", "scannedUnitLines", "sourceDigests"):
        with test.subTest(field=field):
            altered = read_json(root / exact["raw_output"]["native_json"])
            altered[field][str(root / "src/demo/first.py")] = (
                [] if field != "sourceDigests" else "wrong")
            bad.write_text(json.dumps(altered), encoding="utf-8")
            invalid = normalize("jscpd", {"exit_code": 1, "stdout": "", "stderr": "",
                                           "error": None}, root, bad, paths, True)
            test.assertEqual(invalid["status"], "error")
            test.assertTrue(any(d["is_infrastructure_error"] for d in invalid["diagnostics"]))


def verify_prepared_renamed(test: unittest.TestCase, root: Path) -> None:
    """Check changed identifiers/literals, same-file and exact peer findings."""
    first = ("def compute(value):\n    alpha = value + 17\n    beta = alpha * 3\n"
             "    gamma = beta - 4\n    delta = gamma / 2\n    return delta + 10\n")
    other = ("def compute(number):\n    start = number + 17\n    middle = start * 3\n"
             "    end = middle - 4\n    result = end / 2\n    return result + 10\n")
    (root / "src/demo/first.py").write_text(first + "\n" + other,
                                                encoding="utf-8")
    (root / "tests/second.py").write_text("unique = object()\n", encoding="utf-8")
    for selection in (None, "", "src/demo/first.py"):
        with test.subTest(lcom_files=selection):
            fake, _ = fake_tools()
            python, _ = wheel()

            def dispatch(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
                if Path(argv[1]).name == "_clone_engine_worker.py":
                    return capture([str(python), *argv[1:]], cwd, timeout)
                return fake(argv, cwd, timeout)

            with patch.dict(os.environ, {"QUARD_CLONE_ENGINE": "rust"}):
                result = NativeCheckRunner().run(root, lcom_files=selection, serial=True,
                                                 timeout=60, capture_fn=dispatch,
                                                 versions_fn=versions)
            jobs = {item["name"]: item for item in result["tools"]}
            test.assertEqual((result["exit_code"], jobs["jscpd"]["status"],
                              jobs["jscpd-renamed"]["status"]),
                             (1, "passed", "violations"), result["diagnostics"])
            exact = prepared_native(test, root, result, 3)
            renamed = prepared_native(test, root, result, 3, "renamed")
            test.assertFalse(exact["duplicates"])
            test.assertTrue(renamed["duplicates"])
            test.assertEqual({item["kind"] for item in renamed["duplicates"]}, {"renamed"})
            test.assertIn("src/demo/first.py", {d["path"] for d in result["diagnostics"]
                                                if d["rule"] == "RENAMED_CLONE"})
            test.assertFalse([d for d in result["diagnostics"] if d["job"] == "jscpd"])
            test.assertEqual(jobs["lcom4"]["status"] == "skipped", selection == "")
            test.assertEqual(result["coverage"]["lcom4"]["mode"],
                             "skipped" if selection == "" else
                             "selected" if selection else "full")
    (root / "src/demo/first.py").write_text(SAMPLE, encoding="utf-8")
    (root / "tests/second.py").write_text(SAMPLE.replace("value", "number")
                                          .replace("first", "alpha")
                                          .replace("second", "beta"), encoding="utf-8")
    cross = report(root)
    _ = prepared_native(test, root, cross, 3, "renamed")
    test.assertEqual({d["path"] for d in cross["diagnostics"]
                      if d["rule"] == "RENAMED_CLONE"},
                     {"src/demo/first.py", "tests/second.py"})
    changed = SAMPLE
    for old, new in (("17", "23"), (" * 3", " * 8"), (" - 4", " - 6"),
                     (" / 2", " / 7"), (" + 10", " + 20")):
        changed = changed.replace(old, new)
    (root / "tests/second.py").write_text(changed, encoding="utf-8")
    literals = report(root)
    literal_native = prepared_native(test, root, literals, 3, "renamed")
    test.assertTrue(literal_native["duplicates"])
    test.assertEqual({d["path"] for d in literals["diagnostics"]
                      if d["rule"] == "RENAMED_CLONE"},
                     {"src/demo/first.py", "tests/second.py"})
    (root / "src/demo/first.py").write_text(SAMPLE.replace("17", "'hello'"),
                                              encoding="utf-8")
    (root / "tests/second.py").write_text(changed.replace("23", "'world'"),
                                          encoding="utf-8")
    strings = report(root)
    string_native = prepared_native(test, root, strings, 3, "renamed")
    test.assertTrue(string_native["duplicates"])


def verify_renamed_failure(test: unittest.TestCase, root: Path) -> None:
    """Invalid renamed mappings and native failures retain exact and peer results."""
    (root / "src/demo/first.py").write_text(SAMPLE, encoding="utf-8")
    (root / "tests/second.py").write_text(SAMPLE, encoding="utf-8")
    (root / "tests/third.py").write_text(SAMPLE.replace("repeat", "change")
                                         .replace("value", "number").replace("first", "alpha")
                                         .replace("second", "beta").replace("third", "gamma")
                                         .replace("fourth", "delta").replace("17", "23"),
                                         encoding="utf-8")
    fake, _ = fake_tools({"pydoclint": {"stderr": "src/demo/first.py:1: DOC101: Issue\n",
                                       "code": 1}})
    python, _ = wheel()

    def dispatch(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
        if Path(argv[1]).name != "_clone_engine_worker.py":
            return fake(argv, cwd, timeout)
        result = capture([str(python), *argv[1:]], cwd, timeout)
        if argv[-1] == "all" and result["exit_code"] == 0:
            statuses = json.loads(result["stdout"])
            statuses["renamed"] = {"exit_code": 2, "stderr": "native renamed failure"}
            return {**result, "stdout": json.dumps(statuses)}
        return result

    with patch.dict(os.environ, {"QUARD_CLONE_ENGINE": "rust"}):
        result = NativeCheckRunner().run(root, lcom_files="", serial=True, timeout=60,
                                         capture_fn=dispatch, versions_fn=versions)
    jobs = {item["name"]: item for item in result["tools"]}
    test.assertEqual((result["exit_code"], jobs["jscpd"]["status"],
                      jobs["jscpd-renamed"]["status"]), (2, "violations", "error"))
    test.assertTrue({"EXACT_CLONE", "DOC101"} <= {d["rule"] for d in result["diagnostics"]})
    test.assertTrue(any(d["is_infrastructure_error"] and d["job"] == "jscpd-renamed"
                        for d in result["diagnostics"]))
    native = read_json(root / jobs["jscpd-renamed"]["raw_output"]["native_json"])
    native["duplicates"] = [{**native["duplicates"][0], "kind": "exact"}]
    bad = root / "bad-renamed.json"
    bad.write_text(json.dumps(native), encoding="utf-8")
    paths = read_json((root / result["report_path"]).parent / "clone-engine-files.json")["paths"]
    parsed = normalize("jscpd", {"exit_code": 1, "stdout": "", "stderr": "", "error": None},
                       root, bad, paths, True, "renamed")
    test.assertEqual(parsed["status"], "error")
    native["duplicates"][0]["kind"] = "renamed"
    native["duplicates"][0]["firstFile"]["startLoc"]["position"] += 1
    bad.write_text(json.dumps(native), encoding="utf-8")
    invalid_mapping = normalize("jscpd", {"exit_code": 1, "stdout": "", "stderr": "",
                                           "error": None}, root, bad, paths, True, "renamed")
    test.assertEqual(invalid_mapping["status"], "error")
    bad.unlink()
    missing = normalize("jscpd", {"exit_code": 1, "stdout": "", "stderr": "",
                                   "error": None}, root, bad, paths, True, "renamed")
    test.assertEqual(missing["status"], "error")
    test.assertTrue(all(any(d["is_infrastructure_error"] for d in entry["diagnostics"])
                        for entry in (parsed, invalid_mapping, missing)))


def verify_real_full_check(test: unittest.TestCase, root: Path) -> None:
    """Run all analyzers, unmocked, inside an isolated uv consumer without Rust.

    Args:
        test (unittest.TestCase): Assertion owner.
        root (Path): Scratch root containing the nested selected consumer.
    """
    consumer = root / "isolated-consumer"
    consumer.mkdir()
    adoption_consumer(consumer)
    python = consumer / ".venv/bin/python"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(consumer / ".venv")],
                   check=True, capture_output=True, text=True)
    install = subprocess.run(["uv", "pip", "install", "--python", str(python),
                     "--group", "dev", "--project", str(QUARD)],
                   check=False, capture_output=True, text=True, timeout=180)
    if install.returncode:
        raise AssertionError(install.stderr)
    _, artifact = wheel()
    subprocess.run(["uv", "pip", "install", "--python", str(python), str(artifact)],
                   check=True, capture_output=True, text=True)
    env = os.environ.copy()
    env["QUARD_CLONE_ENGINE"] = "rust"
    env["PATH"] = str(python.parent) + ":/usr/local/bin:/usr/bin:/bin"
    test.assertNotIn("cargo", env["PATH"])

    def check(expected: int, exact_code: int | None = None) -> dict[str, Any]:
        process = subprocess.run([str(python), str(QUARD / "check.py"), "--project",
                                  str(consumer), "--json"], cwd=consumer, env=env,
                                 capture_output=True, text=True, timeout=180, check=False)
        result = read_json(consumer / ".quard/reports/latest.json")
        test.assertEqual((process.returncode, result["exit_code"]), (expected, expected),
                         process.stderr + process.stdout[-2000:])
        test.assertEqual(result["summary"]["infrastructure_errors"], 0,
                         result["diagnostics"])
        jobs = {job["name"]: job for job in result["tools"]}
        test.assertEqual(jobs["jscpd"]["exit_code"],
                         None if expected == 0 and exact_code is None else
                         expected if exact_code is None else exact_code)
        test.assertIn("_clone_engine_worker.py", jobs["jscpd"]["argv"][1])
        for name in ("jscpd", "jscpd-renamed", "jscpd-similar"):
            test.assertIn("native_json", jobs[name]["raw_output"])
        test.assertIn("_clone_engine_worker.py", jobs["jscpd-renamed"]["argv"][1])
        test.assertIn("_clone_engine_worker.py", jobs["jscpd-similar"]["argv"][1])
        test.assertEqual(result["versions"]["quard-clones"], "0.1.0")
        return result

    clean = check(0)
    test.assertEqual({job["status"] for job in clean["tools"]}, {"passed", "skipped"})
    text = ("def calculate(value):\n    first = value + 17\n    second = first * 3\n"
            "    third = second - 4\n    fourth = third / 2\n    return fourth + 10\n")
    for name in ("first", "second"):
        (consumer / f"src/demo/main/{name}.py").write_text(text, encoding="utf-8")
    violations = check(1)
    test.assertEqual({item["path"] for item in violations["diagnostics"]
                      if item["rule"] == "EXACT_CLONE"},
                     {"src/demo/main/first.py", "src/demo/main/second.py"})
    renamed = text.replace("value", "number").replace("first", "initial")
    renamed = renamed.replace("second", "next_value").replace("third", "intermediate")
    renamed = renamed.replace("fourth", "final_value")
    (consumer / "src/demo/main/second.py").write_text(renamed, encoding="utf-8")
    renamed_result = check(1, exact_code=0)
    test.assertEqual({item["path"] for item in renamed_result["diagnostics"]
                      if item["rule"] == "RENAMED_CLONE"},
                      {"src/demo/main/first.py", "src/demo/main/second.py"})
    # A third real packaged-wheel scan proves Type-3 detection, not just output files.
    beginning = ("def calculate(value):\n    alpha = value + 17\n    beta = alpha * 3\n"
                 "    gamma = beta - 4\n    delta = gamma / 2\n")
    ending = ("    left = value + 71\n    right = left * 8\n"
              "    total = right - 6\n    adjusted = total / 9\n    return adjusted\n")
    (consumer / "src/demo/main/first.py").write_text(beginning + "    extra = 1\n" + ending,
                                                        encoding="utf-8")
    (consumer / "src/demo/main/second.py").write_text(beginning + "    extra = 2\n" + ending,
                                                         encoding="utf-8")
    gap = check(1, exact_code=1)
    test.assertEqual(next(j for j in gap["tools"] if j["name"] == "jscpd-similar")["status"],
                     "violations")
    test.assertEqual({d["path"] for d in gap["diagnostics"] if d["rule"] == "SIMILAR_CLONE"},
                     {"src/demo/main/first.py", "src/demo/main/second.py"})
