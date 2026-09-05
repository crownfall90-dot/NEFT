"""Поиск параметров UpDown5m с винрейтом выше порога безубытка.

    python scripts/updown_grid.py --days 3
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from neft.strategies.updown_5m import UpDown5m
from scripts.updown_data import load


def run_one(df: pd.DataFrame, **kw) -> dict:
    s = UpDown5m(**kw)
    s.prepare(df)
    sig = s.signals()
    if sig.empty:
        return {"trades": 0, "wr": 0.0, **kw}
    wins = int((sig.outcome == "win").sum())
    losses = int((sig.outcome == "loss").sum())
    dec = wins + losses
    return {"trades": len(sig), "wins": wins, "losses": losses,
            "wr": round(wins / dec * 100, 2) if dec else 0.0, **kw}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--price", type=float, default=0.46)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    be = args.price * 100

    grid = dict(
        momentum_bars=[3, 5, 10, 15],
        min_momentum_atr=[0.2, 0.5, 1.0, 1.5],
        max_momentum_atr=[2.0, 3.0, 99.0],
        require_ema_stack=[True, False],
        min_atr_pct=[0.0, 0.03],
        cooldown_bars=[5],
    )
    keys = list(grid)
    rows = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        kw = dict(zip(keys, combo))
        if kw["min_momentum_atr"] >= kw["max_momentum_atr"]:
            continue
        rows.append(run_one(df, **kw))

    res = pd.DataFrame(rows)
    res = res[res.trades >= 30].sort_values("wr", ascending=False)
    print(f"порог безубытка: {be}%   конфигураций: {len(res)}")
    print(f"выше порога: {(res.wr > be).sum()}")
    print()
    cols = ["wr", "trades", "wins", "losses", "momentum_bars", "min_momentum_atr",
            "max_momentum_atr", "require_ema_stack", "min_atr_pct"]
    print("ЛУЧШИЕ 12:")
    print(res[cols].head(12).to_string(index=False))
    print()
    print("ХУДШИЕ 5:")
    print(res[cols].tail(5).to_string(index=False))
    print()
    print(f"медианный винрейт по всем конфигам: {res.wr.median():.2f}%")
    print(f"средний винрейт: {res.wr.mean():.2f}%")


if __name__ == "__main__":
    main()
