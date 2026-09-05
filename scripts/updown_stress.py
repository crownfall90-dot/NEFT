"""Стресс-тест лучшего конфига: значимость, устойчивость, задержка входа.

    python scripts/updown_stress.py --days 3
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from neft.strategies.updown_5m import UpDown5m
from scripts.updown_data import load

BEST = dict(momentum_bars=15, min_momentum_atr=1.5,
            max_momentum_atr=2.0, require_ema_stack=True)


def sig_for(df: pd.DataFrame, **over) -> pd.DataFrame:
    kw = {**BEST, **over}
    s = UpDown5m(**kw)
    s.prepare(df)
    return s.signals()


def wr(sig: pd.DataFrame) -> tuple[float, int, int]:
    w = int((sig.outcome == "win").sum())
    l = int((sig.outcome == "loss").sum())
    return (w / (w + l) * 100 if w + l else 0.0), w, l


def binom_p(wins: int, n: int, p0: float = 0.5) -> float:
    """Вероятность получить >= wins побед при честной монете (одностороний)."""
    from math import comb
    return sum(comb(n, k) * p0**n for k in range(wins, n + 1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--price", type=float, default=0.46)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    be = args.price * 100

    print("=" * 68)
    print("КОНФИГ:", BEST)
    print("=" * 68)

    sig = sig_for(df)
    w_rate, wins, losses = wr(sig)
    n = wins + losses
    print(f"\nВесь период: винрейт {w_rate:.2f}%  ({wins}W / {losses}L = {n} сделок)")
    print(f"порог безубытка {be}% → перевес {w_rate - be:+.2f} п.п.")

    p = binom_p(wins, n)
    sd = np.sqrt(0.25 / n) * 100
    print(f"\nЗНАЧИМОСТЬ:")
    print(f"  p-value (против честной монеты 50%): {p:.4f}")
    print(f"  {'ЗНАЧИМО' if p < 0.05 else 'НЕ ЗНАЧИМО'} на уровне 0.05")
    print(f"  σ случайного винрейта при {n} сделках: ±{sd:.2f} п.п.")
    print(f"  95% доверительный интервал винрейта: "
          f"{w_rate - 1.96 * sd:.1f}% … {w_rate + 1.96 * sd:.1f}%")
    if w_rate - 1.96 * sd < be:
        print(f"  ⚠ нижняя граница НИЖЕ порога {be}% — убыток не исключён")

    print(f"\nПО ДНЯМ:")
    s2 = sig.copy()
    s2["day"] = pd.to_datetime(s2.time).dt.date
    for day, g in s2.groupby("day"):
        gw = int((g.outcome == "win").sum()); gl = int((g.outcome == "loss").sum())
        r = gw / (gw + gl) * 100 if gw + gl else 0
        flag = "✓" if r > be else "✗"
        print(f"  {day}  {r:5.1f}%  ({gw}W/{gl}L)  {flag}")

    print(f"\nЧУВСТВИТЕЛЬНОСТЬ (сдвиг одного параметра):")
    for key, vals in [("momentum_bars", [12, 14, 15, 16, 18]),
                      ("min_momentum_atr", [1.3, 1.4, 1.5, 1.6, 1.7]),
                      ("max_momentum_atr", [1.8, 1.9, 2.0, 2.1, 2.3])]:
        line = []
        for v in vals:
            s = sig_for(df, **{key: v})
            r, _, _ = wr(s)
            line.append(f"{v}:{r:.1f}%(n={len(s)})")
        print(f"  {key:18} " + "  ".join(line))

    print(f"\nЗАДЕРЖКА ВХОДА (реальность: сигнал на close, вход не мгновенный):")
    for lag in (0, 1, 2):
        s = sig_for(df)
        if lag:
            d = UpDown5m(**BEST).prepare(df)
            ok = s[s.i_entry + lag + 5 < len(d)]
            px = d.close.to_numpy()
            e = px[ok.i_entry.to_numpy() + lag]
            x = px[ok.i_entry.to_numpy() + lag + 5]
            up = ok.side.to_numpy() == "Up"
            win = np.where(up, x > e, x < e)
            r = win.sum() / len(win) * 100
            print(f"  вход через {lag} мин после сигнала: {r:.2f}%  (n={len(win)})")
        else:
            r, _, _ = wr(s)
            print(f"  вход мгновенно на close:     {r:.2f}%  (n={len(s)})")

    print(f"\nРАСПРЕДЕЛЕНИЕ ПО СТОРОНАМ:")
    for side, g in sig.groupby("side"):
        gw = int((g.outcome == "win").sum()); gl = int((g.outcome == "loss").sum())
        r = gw / (gw + gl) * 100 if gw + gl else 0
        print(f"  {side:5} {r:5.1f}%  ({gw}W/{gl}L)")


if __name__ == "__main__":
    main()
