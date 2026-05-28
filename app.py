#!/usr/bin/env python3
"""Convenience launcher for the Haven dashboard app."""

from pathlib import Path
import runpy
import os


APP_DIR = Path(__file__).resolve().parent / "Haven"
os.chdir(APP_DIR)
runpy.run_path(str(APP_DIR / "app.py"), run_name="__main__")
