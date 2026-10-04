"""Compatibility entry point for Eryx's contract folder worker."""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
runpy.run_module("eryx._contract_folders_worker", run_name="__main__", alter_sys=True)
