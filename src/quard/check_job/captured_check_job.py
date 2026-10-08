"""Run a native analyzer job and preserve both its output and failures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, override

from quard._common import atomic_write
from quard.report_parsing.native_report_parser import NativeReportParser
from quard.check_job.check_job import CheckJob


class CapturedCheckJob(CheckJob):
    """Return a normalized result with links to consumer-owned raw logs."""

    @override
    def capture_output(self, job: dict[str, Any], root: Path, run_dir: Path, timeout: int,
                       *, capture_fn: Callable[..., dict[str, Any]]
                       ) -> tuple[dict[str, Any], dict[str, str]]:
        try:
            result = capture_fn(job["argv"], root, timeout)
        except Exception as exc:
            result = {"argv": job["argv"], "exit_code": None, "stdout": "", "stderr": str(exc),
                      "duration_seconds": 0, "error": "RUNNER_SUBPROCESS_ERROR"}
        logs: dict[str, str] = {}
        for stream in ("stdout", "stderr"):
            path = run_dir / "raw" / f"{job['name']}.{stream}.log"
            atomic_write(path, str(result.get(stream, "")), root)
            logs[stream] = path.relative_to(root).as_posix()
        if job.get("json_file") and job["json_file"].exists():
            logs["native_json"] = job["json_file"].relative_to(root).as_posix()
        return result, logs

    @override
    def execute(self, job: dict[str, Any], root: Path, run_dir: Path, timeout: int, *,
                capture_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        result, logs = self.capture_output(job, root, run_dir, timeout, capture_fn=capture_fn)
        normalized = NativeReportParser(root).normalize(
            job["tool"], result, job.get("json_file"), job.get("expected_paths"),
            job.get("engine", False))
        for entry in normalized["diagnostics"]:
            entry["job"] = job["name"]
            if "profile" in job:
                entry["profile"] = job["profile"]
        return {"name": job["name"], "tool": job["tool"], "status": normalized["status"],
                "exit_code": result["exit_code"], "duration_seconds": result["duration_seconds"],
                "argv": job["argv"], "raw_output": logs, "diagnostics": normalized["diagnostics"],
                "error": normalized["error"]}
