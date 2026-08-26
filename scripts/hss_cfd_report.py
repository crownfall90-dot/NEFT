"""Обратная совместимость: HSS CFD отчёт → strategy_backtest.

    python scripts/hss_cfd_report.py --days 90
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "--strategy", "hss", *sys.argv[1:]]
    runpy.run_path(str(Path(__file__).resolve().with_name("strategy_backtest.py")),
                   run_name="__main__")
