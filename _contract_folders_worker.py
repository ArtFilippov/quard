"""Compatibility entry point for Quard's contract folder worker."""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
runpy.run_module("quard._contract_folders_worker", run_name="__main__", alter_sys=True)
