"""Проверка mean-reversion гипотезы с честным train/test разделением.

Сканирование на 60 днях показало: после сильного роста доля Up падает до 47%,
после сильного падения растёт до 51%. Здесь проверяем, торгуемо ли это.

Параметры подбираются ТОЛЬКО на train, test не трогается до финала.

    python scripts/updown_reversion.py --days 60
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from neft.core.indicators import atr, ema
from scripts.updown_data import load

H = 5
PRICE = 0.46
BE = PRICE * 100


def prep(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy().reset_index(drop=True)
    d["atr"] = atr(d, 14)
    a = d.atr.replace(0, np.nan)
    for n in (5, 10, 15, 30, 60):
        d[f"mom{n}"] = (d.close - d.close.shift(n)) / a
    d["ema20"] = ema(d.close, 20)
    d["px_vs_ema20"] = (d.close - d.ema20) / a
    rng = (d.high - d.low).replace(0, np.nan)
    d["bar_pos"] = (d.close - d.low) / rng
    d["atr_pct"] = d.atr / d.close * 100
    d["hour"] = d.time.dt.hour
    d["fwd"] = d.close.shift(-H) - d.close
    return d


def trade(d: pd.DataFrame, *, mom_col: str, thr: float, use_pos: bool,
          cooldown: int = 5, hours: set | None = None) -> pd.DataFrame:
    """Fade: сильный рост → ставим Down, сильное падение → ставим Up."""
    rows = []
    last = -10**9
    m = d[mom_col].to_numpy()
    fwd = d.fwd.to_numpy()
    pos = d.bar_pos.to_numpy()
    warm = 65
    for i in range(warm, len(d) - H):
        if i - last < cooldown:
            continue
        if hours is not None and d.hour.iat[i] not in hours:
            continue
        v = m[i]
        if not np.isfinite(v) or abs(v) < thr:
            continue
        side = "Down" if v > 0 else "Up"      # против движения
        if use_pos:
            p = pos[i]
            if not np.isfinite(p):
                continue
            # ставим Down только если закрылись у верха бара, и наоборот
            if side == "Down" and p < 0.6:
                continue
            if side == "Up" and p > 0.4:
                continue
        f = fwd[i]
        if not np.isfinite(f):
            continue
        out = "tie" if f == 0 else ("win" if (f > 0) == (side == "Up") else "loss")
        rows.append({"i": i, "time": d.time.iat[i], "side": side,
                     "entry": d.close.iat[i], "exit": d.close.iat[i + H],
                     "outcome": out})
        last = i
    return pd.DataFrame(rows)


def score(sig: pd.DataFrame) -> dict:
    if sig.empty:
        return {"n": 0, "wr": 0.0, "x": 1.0}
    w = int((sig.outcome == "win").sum())
    l = int((sig.outcome == "loss").sum())
    n = w + l
    wr = w / n * 100 if n else 0.0
    bal, mult = 1000.0, 1 / PRICE - 1
    for o in sig.outcome:
        st = bal * 0.03
        bal += st * mult if o == "win" else (-st if o == "loss" else -st * 0.5)
    return {"n": len(sig), "wins": w, "losses": l, "wr": round(wr, 2),
            "x": round(bal / 1000, 3)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    d = prep(df)

    cut = int(len(d) * 0.6)
    train = d.iloc[:cut].reset_index(drop=True)
    test = d.iloc[cut:].reset_index(drop=True)
    print(f"TRAIN: {train.time.iloc[0]} → {train.time.iloc[-1]}  ({len(train)} баров)")
    print(f"TEST:  {test.time.iloc[0]} → {test.time.iloc[-1]}  ({len(test)} баров)")
    print(f"порог безубытка {BE}%\n")

    grid = list(itertools.product(
        ["mom5", "mom10", "mom15", "mom30", "mom60"],
        [1.0, 1.5, 2.0, 2.5, 3.0],
        [False, True],
    ))
    rows = []
    for col, thr, up in grid:
        s = trade(train, mom_col=col, thr=thr, use_pos=up)
        sc = score(s)
        if sc["n"] >= 50:
            rows.append({"mom": col, "thr": thr, "bar_pos": up, **sc})
    res = pd.DataFrame(rows).sort_values("wr", ascending=False)
    print("TRAIN — топ 10:")
    print(res.head(10).to_string(index=False))
    print(f"\nконфигов выше порога на train: {(res.wr > BE).sum()}/{len(res)}")
    print(f"медиана винрейта на train: {res.wr.median():.2f}%\n")

    print("=" * 70)
    print("ПЕРЕНОС НА TEST (данные не участвовали в подборе):")
    print("=" * 70)
    top = res.head(5)
    for _, r in top.iterrows():
        s = trade(test, mom_col=r["mom"], thr=r.thr, use_pos=r.bar_pos)
        sc = score(s)
        delta = sc["wr"] - r.wr
        flag = "✓" if sc["x"] > 1 else "✗"
        print(f"{r['mom']:6} thr={r.thr} pos={str(r.bar_pos):5}  "
              f"train {r.wr:5.2f}% → test {sc['wr']:5.2f}%  "
              f"({delta:+5.2f} п.п., n={sc['n']}, ×{sc['x']}) {flag}")

    print("\nкорреляция train/test по всем конфигам:")
    both = []
    for _, r in res.iterrows():
        s = trade(test, mom_col=r["mom"], thr=r.thr, use_pos=r.bar_pos)
        sc = score(s)
        if sc["n"] >= 30:
            both.append({"train": r.wr, "test": sc["wr"], "x": sc["x"]})
    b = pd.DataFrame(both)
    if len(b) > 3:
        print(f"  r = {b.train.corr(b.test):+.3f}  (по {len(b)} конфигам)")
        print(f"  конфигов прибыльных на test: {(b.x > 1).sum()}/{len(b)}")
        print(f"  средний винрейт на test: {b.test.mean():.2f}%")


if __name__ == "__main__":
    main()
