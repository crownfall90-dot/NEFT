"""Есть ли на 5-минутном горизонте BTC хоть какой-то предсказуемый сигнал?

Проверяем поодиночке, БЕЗ подбора параметров: каждый признак делим на квинтили
и смотрим долю Up через 5 минут. Если признак несёт информацию, крайние
квинтили дадут винрейт, устойчиво отличный от 50%.

    python scripts/updown_edge_scan.py --days 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from neft.core.indicators import atr, ema
from scripts.updown_data import load

H = 5  # горизонт экспирации, баров


def features(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy().reset_index(drop=True)
    d["atr"] = atr(d, 14)
    a = d.atr.replace(0, np.nan)

    for n in (3, 5, 10, 15, 30, 60):
        d[f"mom{n}"] = (d.close - d.close.shift(n)) / a
    d["ema20"] = ema(d.close, 20)
    d["ema60"] = ema(d.close, 60)
    d["ema_gap"] = (d.ema20 - d.ema60) / a
    d["px_vs_ema20"] = (d.close - d.ema20) / a

    rng = (d.high - d.low).replace(0, np.nan)
    d["body"] = (d.close - d.open) / rng
    d["upper_wick"] = (d.high - d[["open", "close"]].max(axis=1)) / rng
    d["lower_wick"] = (d[["open", "close"]].min(axis=1) - d.low) / rng
    d["bar_pos"] = (d.close - d.low) / rng          # где закрылись внутри бара

    d["atr_pct"] = d.atr / d.close * 100
    d["atr_chg"] = d.atr / d.atr.rolling(60).mean() - 1
    d["vol_chg"] = d.volume / d.volume.rolling(60).mean().replace(0, np.nan) - 1

    # серия однонаправленных баров
    up = (d.close > d.open).astype(int)
    run = up.groupby((up != up.shift()).cumsum()).cumcount() + 1
    d["run_len"] = run * np.where(up == 1, 1, -1)

    d["ret_prev"] = d.close.pct_change() * 100
    d["hour"] = d.time.dt.hour

    # цель: цена через H баров выше текущей?
    d["target_up"] = (d.close.shift(-H) > d.close).astype(float)
    d.loc[d.close.shift(-H).isna(), "target_up"] = np.nan
    return d


def scan(d: pd.DataFrame, col: str, q: int = 5) -> pd.DataFrame:
    sub = d[[col, "target_up"]].dropna()
    if len(sub) < 200 or sub[col].nunique() < q:
        return pd.DataFrame()
    try:
        sub["bin"] = pd.qcut(sub[col], q, duplicates="drop")
    except ValueError:
        return pd.DataFrame()
    g = sub.groupby("bin", observed=True).target_up.agg(["mean", "count"])
    g["wr"] = g["mean"] * 100
    g["se"] = np.sqrt(0.25 / g["count"]) * 100
    g["z"] = (g.wr - 50) / g.se
    return g


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    d = features(df)
    n_all = int(d.target_up.notna().sum())
    base = d.target_up.mean() * 100

    print(f"данные: {d.time.iloc[0]} → {d.time.iloc[-1]}  ({len(d)} баров)")
    print(f"базовая доля Up через {H} мин: {base:.2f}%  на {n_all} окнах")
    print(f"σ на всей выборке: ±{np.sqrt(0.25/n_all)*100:.2f} п.п.")
    print(f"порог безубытка при цене 0.46: 46.0%\n")
    print("Для каждого признака — крайние квинтили (|z|>2 = статистически заметно):\n")

    cols = ["mom3", "mom5", "mom10", "mom15", "mom30", "mom60", "ema_gap",
            "px_vs_ema20", "body", "upper_wick", "lower_wick", "bar_pos",
            "atr_pct", "atr_chg", "vol_chg", "run_len", "ret_prev"]
    found = []
    for c in cols:
        g = scan(d, c)
        if g.empty:
            continue
        lo, hi = g.iloc[0], g.iloc[-1]
        mark = ""
        if abs(lo.z) > 2 or abs(hi.z) > 2:
            mark = "  <<<"
            found.append(c)
        print(f"{c:14} низ: {lo.wr:5.2f}% (z={lo.z:+5.2f}, n={int(lo['count'])})   "
              f"верх: {hi.wr:5.2f}% (z={hi.z:+5.2f}, n={int(hi['count'])}){mark}")

    print(f"\nпризнаков с |z|>2: {len(found)}  {found}")
    print("\nПО ЧАСАМ (UTC):")
    hourly = d.groupby("hour").target_up.agg(["mean", "count"])
    hourly["wr"] = hourly["mean"] * 100
    hourly["z"] = (hourly.wr - 50) / (np.sqrt(0.25 / hourly["count"]) * 100)
    strong = hourly[hourly.z.abs() > 2]
    if len(strong):
        for h, r in strong.iterrows():
            print(f"  {h:02d}:00  {r.wr:.2f}%  (z={r.z:+.2f}, n={int(r['count'])})")
    else:
        print("  ни одного часа с |z|>2")


if __name__ == "__main__":
    main()
