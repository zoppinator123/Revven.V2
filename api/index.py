"""Vercel serverless entrypoint for the Revven.V2 / Haven dashboard.

Vercel's Python runtime auto-discovers a WSGI/ASGI callable named ``app`` in
files under ``/api``. We re-export the Flask app defined in ``Haven/app.py``.
"""

from pathlib import Path
import os
import sys

# Ensure the Haven package directory is importable and used as the working
# directory so relative data file paths (CSV, JSON snapshots, templates)
# resolve the same way they do under ``python3 app.py``.
HAVEN_DIR = Path(__file__).resolve().parent.parent / "Haven"
sys.path.insert(0, str(HAVEN_DIR))
os.chdir(HAVEN_DIR)

from app import app  # noqa: E402  (import after sys.path/cwd setup)

# Vercel looks for ``app`` (WSGI callable) at module top-level.
__all__ = ["app"]
