"""Compatibility entry point for Eryx's dependency type worker."""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
runpy.run_module("eryx._dependency_types_worker", run_name="__main__", alter_sys=True)
