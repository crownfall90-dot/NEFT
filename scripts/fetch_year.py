"""Качает ~1 год M1 и M5 по всем размеченным крипто-парам, с прогрессом."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import time

from neft.backtest import crypto_data
from neft.core.routing import CRYPTO_ROUTES

PAIRS = list(CRYPTO_ROUTES)
DAYS = 365

if __name__ == "__main__":
    for tf in ("1m", "5m"):
        for sym in PAIRS:
            t0 = time.time()
            try:
                df = crypto_data.load(sym, tf, days=DAYS, refresh=False)
                print(f"{sym:20s} {tf:3s} {len(df):>7} баров  "
                      f"{df.time.iloc[0]:%Y-%m-%d} - {df.time.iloc[-1]:%Y-%m-%d}  "
                      f"{time.time()-t0:5.1f}с", flush=True)
            except Exception as e:
                print(f"{sym:20s} {tf:3s}  ОШИБКА {type(e).__name__}: {e}",
                      flush=True)
    print("ГОТОВО")
