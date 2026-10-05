#!/usr/bin/env python3
"""Compatibility entry point for Quard's installer."""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
runpy.run_module("quard.install", run_name="__main__", alter_sys=True)
