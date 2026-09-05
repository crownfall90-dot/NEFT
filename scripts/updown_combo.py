"""Складываются ли фильтры? Ищем комбинацию с максимальным винрейтом.

Из одиночного скана надёжными выглядят (много наблюдений, есть логика):
  * объёмный всплеск  (vol_z > 1)      57.7%
  * фитиль отбоя      (wick > 0.4)     54.0%
  * край диапазона    (range_pos)      53.0-53.7%
  * сила импульса     (|mom| высокий)  53-56%

Часы намеренно НЕ используем: 02:00 UTC даёт 62% на 502 наблюдениях, но это
подгонка — у часа нет механизма, который бы это объяснял.

Подбор идёт на первых 70%, финальная проверка — в updown_final.py.

    python scripts/updown_combo.py --days 60
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from scripts.updown_wr_hunt import prep
from scripts.updown_data import load

PRICE = 0.46


def taker_fee(p: float, disc: float = .10) -> float:
    return 0.02 * min(p, 1 - p) * (1 - disc)


BE = (PRICE + taker_fee(PRICE)) * 100


def build_mask(d: pd.DataFrame, *, mom_col: str, thr: float,
               vol_min: float | None, wick_min: float | None,
               edge: bool, atr_min: float | None) -> pd.Series:
    m = d[mom_col].abs() >= thr
    if vol_min is not None:
        m &= d.vol_z >= vol_min
    if wick_min is not None:
        up = (d[mom_col] > 0) & (d.upper_wick >= wick_min)
        dn = (d[mom_col] < 0) & (d.lower_wick >= wick_min)
        m &= (up | dn)
    if edge:
        m &= (((d[mom_col] < 0) & (d.range_pos <= .35)) |
              ((d[mom_col] > 0) & (d.range_pos >= .65)))
    if atr_min is not None:
        m &= d.atr_z >= atr_min
    return m.fillna(False)


def evaluate(d: pd.DataFrame, mask: pd.Series, mom_col: str,
             cooldown: int = 5) -> dict:
    """Винрейт с учётом cooldown (нельзя входить, пока идёт прошлый контракт)."""
    idx = np.where(mask.to_numpy() & d.fwd.notna().to_numpy())[0]
    if len(idx) == 0:
        return {"n": 0, "wr": np.nan}
    mom = d[mom_col].to_numpy()
    fwd = d.fwd.to_numpy()
    picked, last = [], -10**9
    for i in idx:
        if i - last < cooldown:
            continue
        picked.append(i)
        last = i
    if len(picked) < 30:
        return {"n": len(picked), "wr": np.nan}
    p = np.array(picked)
    side_up = mom[p] < 0
    win = np.where(side_up, fwd[p] > 0, fwd[p] < 0)
    wr = win.mean() * 100
    return {"n": len(p), "wr": wr, "wins": int(win.sum()),
            "losses": int((~win).sum()), "idx": p, "win": win}


def drawdown(win: np.ndarray, stake_pct: float, price: float = PRICE) -> tuple:
    bal, peak, dd = 1000.0, 1000.0, 0.0
    fee = taker_fee(price)
    for w in win:
        budget = bal * stake_pct / 100
        shares = budget / (price + fee)
        bal += (shares if w else 0) - budget
        peak = max(peak, bal)
        dd = min(dd, (bal - peak) / peak * 100)
    return bal / 1000, dd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    d_all = prep(df)
    cut = int(len(d_all) * 0.7)
    d = d_all.iloc[:cut].reset_index(drop=True)
    span_days = (d.time.iloc[-1] - d.time.iloc[0]).days or 1

    print(f"подбор на {d.time.iloc[0]} → {d.time.iloc[-1]}")
    print(f"порог безубытка с комиссией: {BE:.2f}%\n")

    rows = []
    for mom_col, thr, vol_min, wick_min, edge, atr_min in itertools.product(
        ["mom5", "mom10", "mom15"],
        [3.0, 4.0, 5.0],
        [None, 0.5, 1.0, 1.5],
        [None, 0.3, 0.4],
        [False, True],
        [None, 0.0],
    ):
        m = build_mask(d, mom_col=mom_col, thr=thr, vol_min=vol_min,
                       wick_min=wick_min, edge=edge, atr_min=atr_min)
        r = evaluate(d, m, mom_col)
        if r["n"] < 40 or not np.isfinite(r["wr"]):
            continue
        x2, dd2 = drawdown(r["win"], 2.0)
        x5, dd5 = drawdown(r["win"], 5.0)
        rows.append({
            "mom": mom_col, "thr": thr, "vol": vol_min, "wick": wick_min,
            "edge": edge, "atr": atr_min, "n": r["n"], "wr": round(r["wr"], 2),
            "per_day": round(r["n"] / span_days, 1),
            "x@2%": round(x2, 2), "dd@2%": round(dd2, 1),
            "x@5%": round(x5, 2), "dd@5%": round(dd5, 1),
        })

    res = pd.DataFrame(rows).sort_values("wr", ascending=False)
    print(f"комбинаций с n>=40: {len(res)}\n")
    print("ТОП-15 ПО ВИНРЕЙТУ:")
    print(res.head(15).to_string(index=False))

    print("\nТОП-10 С ОГРАНИЧЕНИЕМ n>=150 (нужна статистика):")
    big = res[res.n >= 150]
    print(big.head(10).to_string(index=False))

    print("\nТОП-10 ПРИ ПРОСАДКЕ <=15% И СТАВКЕ 5%:")
    safe = res[(res["dd@5%"] >= -15) & (res.n >= 100)]
    print(safe.head(10).to_string(index=False))

    print(f"\nмедианный винрейт всех комбинаций: {res.wr.median():.2f}%")
    print(f"комбинаций выше 55%: {(res.wr >= 55).sum()}")
    print(f"комбинаций выше 60%: {(res.wr >= 60).sum()}")


if __name__ == "__main__":
    main()
