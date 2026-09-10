#!/usr/bin/env python3
"""Backward-compat shim so `uv run --no-sync sync.py ...` keeps working.

Real implementation lives in `src/sync.py`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from sync import main

if __name__ == "__main__":
    main()
