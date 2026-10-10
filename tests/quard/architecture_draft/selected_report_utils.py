"""Run selected real analyzer jobs through the report path in architecture tests."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch


def selected_report(test: unittest.TestCase, root: Path, *names: str) -> dict[str, Any]:
    """Select real job definitions before clone batching and archive their output.

    Args:
        test (unittest.TestCase): Owner of the assertions.
        root (Path): Isolated consumer root.
        *names (str): Exact names of jobs to run.

    Returns:
        dict[str, Any]: Report with real results for the selected jobs only.
    """
    runner = import_module("quard.check_runner.native_check_runner")
    original = runner.make_jobs
    requested = set(names)
    test.assertTrue(requested)
    test.assertEqual(len(names), len(requested))
    available: set[str] = set()

    def select(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        jobs = original(*args, **kwargs)
        available.update(job["name"] for job in jobs)
        return [job for job in jobs if job["name"] in requested]

    with patch.object(runner, "make_jobs", side_effect=select):
        report = runner.NativeCheckRunner().run(root)
    test.assertTrue(requested <= available, (requested, available, report["diagnostics"]))
    executed = {job["name"] for job in report["tools"] if job["status"] != "skipped"}
    test.assertEqual(executed, requested, report["tools"])
    test.assertEqual(report["summary"]["infrastructure_errors"], 0, report["diagnostics"])
    for job in report["tools"]:
        if job["name"] in requested:
            test.assertIn(job["status"], {"passed", "violations"}, job)
            test.assertTrue(job["raw_output"], job)
            for path in job["raw_output"].values():
                test.assertTrue((root / path).is_file(), job)
    return report
