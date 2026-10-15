"""Launch the cached-property migration worker from a consumer checkout."""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
runpy.run_module("quard._cached_property_migration_worker", run_name="__main__", alter_sys=True)
