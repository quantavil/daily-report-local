#!/usr/bin/env python3
"""Launch the daily-report CLI from this checkout."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))

from daily_report.cli import main

if __name__ == '__main__':
    main()
