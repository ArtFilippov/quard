"""Compatibility entry point for Quard's test placement worker."""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
runpy.run_module("quard._test_placement_worker", run_name="__main__", alter_sys=True)
