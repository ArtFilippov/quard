#!/usr/bin/env python3
"""Compatibility entry point for Eryx's installer."""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
runpy.run_module("eryx.install", run_name="__main__", alter_sys=True)
