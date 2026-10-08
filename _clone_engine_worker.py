"""Compatibility entry point for the first-party in-memory clone worker."""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
runpy.run_module("quard.clone_engine_utils", run_name="__main__", alter_sys=True)
