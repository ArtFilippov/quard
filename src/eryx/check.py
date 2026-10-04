#!/usr/bin/env python3
"""Compatibility entry point for Eryx's check command."""
import sys

from eryx.check_utils import (argparse, json, Path, Any, Callable, SCHEMA, capture,
                              project_dir, repo_lock, diagnostic, NativeCheckRunner,
                              binary, execute_job, installed_versions, make_jobs,
                              select_lcom_files, run_checks, display, main)
sys.dont_write_bytecode = True

__all__ = ["argparse", "json", "Path", "sys", "Any", "Callable", "SCHEMA", "capture",
           "project_dir", "repo_lock", "diagnostic", "NativeCheckRunner", "binary",
           "execute_job", "installed_versions", "make_jobs", "select_lcom_files",
           "run_checks", "display", "main"]

if __name__ == "__main__":
    sys.exit(main())
