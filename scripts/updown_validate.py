"""Проверка: перенос лучшей конфигурации с одной половины данных на другую.

Если преимущество реально, конфиг, найденный на первой половине, должен
работать и на второй. Если это подгонка под шум — развалится.

    python scripts/updown_validate.py --days 3
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from neft.strategies.updown_5m import UpDown5m
from scripts.updown_data import load

GRID = dict(
    momentum_bars=[3, 5, 10, 15],
    min_momentum_atr=[0.2, 0.5, 1.0, 1.5],
    max_momentum_atr=[2.0, 3.0, 99.0],
    require_ema_stack=[True, False],
)


def wr_of(df: pd.DataFrame, kw: dict) -> tuple[float, int]:
    s = UpDown5m(**kw)
    s.prepare(df)
    sig = s.signals()
    if sig.empty:
        return 0.0, 0
    w = int((sig.outcome == "win").sum())
    l = int((sig.outcome == "loss").sum())
    return (w / (w + l) * 100 if w + l else 0.0), len(sig)


def combos():
    keys = list(GRID)
    for c in itertools.product(*(GRID[k] for k in keys)):
        kw = dict(zip(keys, c))
        if kw["min_momentum_atr"] < kw["max_momentum_atr"]:
            yield kw


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--price", type=float, default=0.46)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    half = len(df) // 2
    a = df.iloc[:half].reset_index(drop=True)
    b = df.iloc[half:].reset_index(drop=True)
    be = args.price * 100

    print(f"выборка A: {a.time.iloc[0]} → {a.time.iloc[-1]}  ({len(a)} баров)")
    print(f"выборка B: {b.time.iloc[0]} → {b.time.iloc[-1]}  ({len(b)} баров)")
    print(f"порог безубытка: {be}%\n")

    rows = []
    for kw in combos():
        wr_a, n_a = wr_of(a, kw)
        wr_b, n_b = wr_of(b, kw)
        if n_a >= 20 and n_b >= 20:
            rows.append({**kw, "wr_A": wr_a, "n_A": n_a, "wr_B": wr_b, "n_B": n_b})
    res = pd.DataFrame(rows)

    best_a = res.sort_values("wr_A", ascending=False).iloc[0]
    print("Лучший конфиг НА ВЫБОРКЕ A:")
    print(f"  momentum_bars={best_a.momentum_bars}, min={best_a.min_momentum_atr}, "
          f"max={best_a.max_momentum_atr}, stack={best_a.require_ema_stack}")
    print(f"  винрейт на A: {best_a.wr_A:.2f}%  ({int(best_a.n_A)} сделок)")
    print(f"  тот же конфиг на B: {best_a.wr_B:.2f}%  ({int(best_a.n_B)} сделок)")
    print(f"  → потеря: {best_a.wr_B - best_a.wr_A:+.2f} п.п.\n")

    corr = res.wr_A.corr(res.wr_B)
    print(f"корреляция винрейта A vs B по {len(res)} конфигам: {corr:+.3f}")
    print("  (около нуля или отрицательная = преимущества нет, это шум)\n")

    print(f"конфигов выше порога на A: {(res.wr_A > be).sum()}/{len(res)}")
    print(f"из них удержались выше порога на B: "
          f"{((res.wr_A > be) & (res.wr_B > be)).sum()}")
    print(f"ожидалось бы при случайности: ~{(res.wr_A > be).sum() * 0.5:.0f}\n")

    # Базовая линия: каков винрейт «слепого» входа каждые 5 минут?
    px = df.close.to_numpy()
    h = 5
    up_moves = (px[h:] > px[:-h]).sum()
    total = len(px) - h
    print(f"БАЗОВАЯ ЛИНИЯ (без стратегии, всегда Up): "
          f"{up_moves / total * 100:.2f}%  на {total} окнах")
    print(f"всегда Down: {(1 - up_moves / total) * 100:.2f}%")

    # Насколько велик разброс чисто случайного винрейта на такой выборке?
    n = int(res.n_A.median())
    sd = np.sqrt(0.25 / n) * 100
    print(f"\nпри {n} сделках стандартное отклонение случайного винрейта: "
          f"±{sd:.2f} п.п.")
    print(f"то есть винрейт до ~{50 + 2 * sd:.1f}% укладывается в случайность "
          f"(2 сигмы)")


if __name__ == "__main__":
    main()
