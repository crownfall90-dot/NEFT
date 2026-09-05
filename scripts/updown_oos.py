"""Честная проверка вне выборки: конфиг подобран на 3 днях, проверяем на 30.

    python scripts/updown_oos.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from neft.strategies.updown_5m import UpDown5m
from scripts.updown_data import load

BEST = dict(momentum_bars=15, min_momentum_atr=1.5,
            max_momentum_atr=2.0, require_ema_stack=True)
PRICE = 0.46


def run(df: pd.DataFrame, label: str) -> dict:
    s = UpDown5m(**BEST)
    s.prepare(df)
    sig = s.signals()
    if sig.empty:
        print(f"{label}: нет сделок")
        return {}
    w = int((sig.outcome == "win").sum())
    l = int((sig.outcome == "loss").sum())
    n = w + l
    r = w / n * 100 if n else 0
    be = PRICE * 100
    # Банкролл 3% от текущего депозита
    bal = 1000.0
    mult = 1 / PRICE - 1
    for _, t in sig.iterrows():
        st = bal * 0.03
        bal += st * mult if t.outcome == "win" else (-st if t.outcome == "loss" else -st * .5)
    print(f"{label:26} {r:5.2f}%  ({w}W/{l}L, n={n})  "
          f"{'✓' if bal > 1000 else '✗'}  депозит ×{bal/1000:.3f}")
    return {"wr": r, "n": n, "x": bal / 1000}


def main() -> None:
    print(f"конфиг: {BEST}")
    print(f"порог безубытка: {PRICE*100}%\n")

    print("── период оптимизации (на нём конфиг и подбирался) ──")
    d3 = load("BTCUSDT", days=3)
    d3 = d3[d3.time.dt.dayofweek < 5].reset_index(drop=True)
    run(d3, "последние 3 дня")

    print("\n── ВНЕ ВЫБОРКИ: данных не было при подборе ──")
    d30 = load("BTCUSDT", days=30)
    d30 = d30[d30.time.dt.dayofweek < 5].reset_index(drop=True)
    # исключаем последние 3 дня — они были в оптимизации
    cutoff = d3.time.iloc[0]
    oos = d30[d30.time < cutoff].reset_index(drop=True)
    res = run(oos, "27 дней до этого")

    print("\n── по неделям (вне выборки) ──")
    oos2 = oos.copy()
    oos2["wk"] = oos2.time.dt.isocalendar().week
    for wk, g in oos2.groupby("wk"):
        if len(g) > 500:
            run(g.reset_index(drop=True), f"  неделя {wk}")

    if res:
        n = res["n"]
        sd = np.sqrt(0.25 / n) * 100
        print(f"\nна {n} сделках вне выборки σ = ±{sd:.2f} п.п.")
        print(f"95% ДИ: {res['wr'] - 1.96*sd:.1f}% … {res['wr'] + 1.96*sd:.1f}%")


if __name__ == "__main__":
    main()
