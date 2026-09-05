"""Можно ли усилить окно 19-23: границы, дни недели, порог импульса.

Окно 19-23 UTC даёт 56.73% против базы 52.33% и проходит проверки
(0 плохих недель, все часы в плюс, jackknife 0.89 п.п.).

Тейкером по реальной цене 0.55 перевес +0.92 п.п. — положительный, но
нижняя граница доверительного интервала ещё в минусе. Пробуем поднять.

Всё проверяется на отложенных 40% данных.

    python scripts/updown_window_tune.py --days 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from scripts.updown_combo import build_mask, evaluate
from scripts.updown_hours import window_mask, wr
from scripts.updown_wr_hunt import prep
from scripts.updown_data import load


def taker_fee(p: float, disc: float = 0.10) -> float:
    return 0.02 * min(p, 1 - p) * (1 - disc)


BE_TAKER_055 = (0.55 + taker_fee(0.55)) * 100


def trades_thr(df: pd.DataFrame, thr: float) -> pd.DataFrame:
    d = prep(df)
    cfg = dict(mom_col="mom10", thr=thr, vol_min=None, wick_min=None,
               edge=False, atr_min=None)
    r = evaluate(d, build_mask(d, **cfg), "mom10")
    if not r.get("n"):
        return pd.DataFrame()
    idx = r["idx"]
    return pd.DataFrame({
        "t": d.time.to_numpy()[idx], "win": r["win"],
        "hour": d.time.dt.hour.to_numpy()[idx],
        "dow": d.time.dt.dayofweek.to_numpy()[idx],
    })


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    s = trades_thr(df, 3.0)
    cut = s.t.quantile(0.6)

    print(f"порог безубытка тейкера при цене 0.55: {BE_TAKER_055:.2f}%\n")

    print("=" * 76)
    print("1. ТОЧНЫЕ ГРАНИЦЫ ОКНА (проверка на отложенных 40%)")
    print("=" * 76)
    print(f"{'окно':>7} {'подбор':>8} {'проверка':>10} {'весь период':>12} "
          f"{'n':>6}")
    for lo, hi in [(18, 23), (19, 23), (19, 22), (20, 23), (19, 0),
                   (18, 0), (20, 0), (19, 1), (20, 2)]:
        a = s[(s.t < cut) & window_mask(s, lo, hi)]
        b = s[(s.t >= cut) & window_mask(s, lo, hi)]
        f = s[window_mask(s, lo, hi)]
        wa, na, _ = wr(a); wb, nb, _ = wr(b); wf, nf, sef = wr(f)
        if na < 100 or nb < 80:
            continue
        print(f"{lo:02d}-{hi:02d}  {wa:>7.2f}% {wb:>9.2f}% {wf:>11.2f}% "
              f"{nf:>6}")

    print("\n" + "=" * 76)
    print("2. ДНИ НЕДЕЛИ ВНУТРИ ОКНА 19-23")
    print("=" * 76)
    sub = s[window_mask(s, 19, 23)]
    names = ["пн", "вт", "ср", "чт", "пт"]
    for dow in range(5):
        g = sub[sub.dow == dow]
        if len(g) < 30:
            continue
        r = g.win.mean() * 100
        e = np.sqrt(0.25 / len(g)) * 100
        print(f"   {names[dow]}: {r:5.2f}% +-{2*e:4.2f}  n={len(g):>3}")

    print("\n" + "=" * 76)
    print("3. ПОРОГ ИМПУЛЬСА ВНУТРИ ОКНА (подбор/проверка)")
    print("=" * 76)
    print(f"{'thr':>5} {'подбор':>8} {'проверка':>10} {'весь':>8} {'n':>6} "
          f"{'перевес тейкера':>16}")
    for thr in (2.0, 2.5, 3.0, 3.5, 4.0, 5.0):
        st = trades_thr(df, thr)
        w_ = st[window_mask(st, 19, 23)]
        a = w_[w_.t < cut]; b = w_[w_.t >= cut]
        wa, na, _ = wr(a); wb, nb, _ = wr(b); wf, nf, _ = wr(w_)
        if nf < 150:
            continue
        print(f"{thr:>5} {wa:>7.2f}% {wb:>9.2f}% {wf:>7.2f}% {nf:>6} "
              f"{wf-BE_TAKER_055:>+15.2f}")

    print("\n" + "=" * 76)
    print("4. ИТОГ: ЧТО ВЫБРАТЬ")
    print("=" * 76)
    best = s[window_mask(s, 19, 23)]
    w, n, se = wr(best)
    lo95 = w - 1.96 * se
    print(f"   окно 19-23 UTC (22-02 мск), thr=3.0")
    print(f"   винрейт {w:.2f}%  n={n}  95% ДИ {lo95:.2f}-{w+1.96*se:.2f}%")
    print(f"   тейкер по 0.55: перевес {w-BE_TAKER_055:+.2f} п.п., "
          f"нижняя граница {lo95-BE_TAKER_055:+.2f}")
    be_maker = (0.47 - taker_fee(0.47) * 0.25) * 100
    print(f"   мейкер по 0.47: перевес {w-be_maker:+.2f} п.п., "
          f"нижняя граница {lo95-be_maker:+.2f}")
    print(f"\n   сделок: {n/59:.0f} в сутки против 97 круглосуточно")


if __name__ == "__main__":
    main()
