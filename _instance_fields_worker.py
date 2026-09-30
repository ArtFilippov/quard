"""Compatibility entry point for Eryx's instance field worker."""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
runpy.run_module("eryx._instance_fields_worker", run_name="__main__", alter_sys=True)
