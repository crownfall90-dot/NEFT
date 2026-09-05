"""Финальная проверка на отложенных 30% + walk-forward + подбор сайзинга.

Кандидаты отобраны в updown_combo.py на ПЕРВЫХ 70%. Здесь они впервые
встречаются с последними 30% данных. Плюс walk-forward по всей истории.

Цели (заданы под эту стратегию):
  * максимальный винрейт, размер иксов вторичен;
  * просадка не глубже 10-15%.

    python scripts/updown_final.py --days 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from scripts.updown_combo import BE, PRICE, build_mask, drawdown, evaluate, taker_fee
from scripts.updown_wr_hunt import prep
from scripts.updown_data import load

# Кандидаты из подбора на первых 70%. Во всех есть vol>=1.0 — объёмный
# всплеск, единственный фильтр с внятным механизмом.
CANDIDATES = [
    dict(name="A vol+wick",      mom_col="mom15", thr=5.0, vol_min=1.0, wick_min=0.3, edge=False, atr_min=0.0),
    dict(name="B vol+edge",      mom_col="mom10", thr=5.0, vol_min=1.0, wick_min=None, edge=True,  atr_min=0.0),
    dict(name="C vol only m10",  mom_col="mom10", thr=5.0, vol_min=1.0, wick_min=None, edge=False, atr_min=0.0),
    dict(name="D vol only m15",  mom_col="mom15", thr=5.0, vol_min=1.0, wick_min=None, edge=False, atr_min=0.0),
    dict(name="E vol1.5 edge",   mom_col="mom10", thr=5.0, vol_min=1.5, wick_min=None, edge=True,  atr_min=0.0),
    dict(name="F базовая fade",  mom_col="mom10", thr=3.0, vol_min=None, wick_min=None, edge=False, atr_min=None),
]


def run(d: pd.DataFrame, c: dict) -> dict:
    m = build_mask(d, mom_col=c["mom_col"], thr=c["thr"], vol_min=c["vol_min"],
                   wick_min=c["wick_min"], edge=c["edge"], atr_min=c["atr_min"])
    return evaluate(d, m, c["mom_col"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    d_all = prep(df)
    cut = int(len(d_all) * 0.7)
    train = d_all.iloc[:cut].reset_index(drop=True)
    test = d_all.iloc[cut:].reset_index(drop=True)

    print(f"TRAIN (подбор):  {train.time.iloc[0]} → {train.time.iloc[-1]}")
    print(f"TEST  (впервые): {test.time.iloc[0]} → {test.time.iloc[-1]}")
    print(f"порог безубытка: {BE:.2f}%\n")

    print("=" * 78)
    print("1. ПЕРЕНОС НА ОТЛОЖЕННЫЕ 30%")
    print("=" * 78)
    print(f"{'конфиг':16} {'train':>16} {'test':>16} {'разница':>9}")
    keep = []
    for c in CANDIDATES:
        rt, rv = run(train, c), run(test, c)
        if not np.isfinite(rv.get("wr", np.nan)):
            print(f"{c['name']:16} {rt['wr']:>7.2f}% n={rt['n']:<5} "
                  f"{'мало сделок':>16}")
            continue
        se = np.sqrt(0.25 / rv["n"]) * 100
        d_wr = rv["wr"] - rt["wr"]
        flag = "✓" if rv["wr"] > BE else "✗"
        print(f"{c['name']:16} {rt['wr']:>7.2f}% n={rt['n']:<5} "
              f"{rv['wr']:>7.2f}% n={rv['n']:<5} {d_wr:>+8.2f} {flag}")
        keep.append((c, rt, rv, se))

    print("\n" + "=" * 78)
    print("2. WALK-FORWARD ПО ВСЕЙ ИСТОРИИ (обучение 14 дн → торговля 1 день)")
    print("=" * 78)
    d_all["date"] = d_all.time.dt.date
    dates = sorted(d_all.date.unique())
    wf = {}
    for c in CANDIDATES:
        outs = []
        for k in range(14, len(dates)):
            te = d_all[d_all.date == dates[k]].reset_index(drop=True)
            if len(te) < 100:
                continue
            r = run(te, c)
            if r["n"] and np.isfinite(r.get("wr", np.nan)):
                outs.extend(r["win"].tolist())
        if len(outs) >= 50:
            arr = np.array(outs)
            wr = arr.mean() * 100
            se = np.sqrt(0.25 / len(arr)) * 100
            wf[c["name"]] = (wr, len(arr), se, arr)
            print(f"   {c['name']:16} {wr:6.2f}% ±{se:.2f}  n={len(arr):<5} "
                  f"{'✓' if wr > BE else '✗'}")

    print("\n" + "=" * 78)
    print("3. ПОДБОР СТАВКИ ПОД ПРОСАДКУ 10–15%")
    print("=" * 78)
    for name, (wr, n, se, arr) in wf.items():
        line = []
        for stake in (2, 3, 5, 7, 10):
            x, dd = drawdown(arr, stake)
            ok = "✓" if dd >= -15 else "✗"
            line.append(f"{stake}%:×{x:.2f}/{dd:.0f}%{ok}")
        print(f"   {name:16} " + "  ".join(line))

    print("\n" + "=" * 78)
    print("4. УСТОЙЧИВОСТЬ ЛУЧШЕГО (walk-forward, по неделям)")
    print("=" * 78)
    if wf:
        best_name = max(wf, key=lambda k: wf[k][0])
        c = next(x for x in CANDIDATES if x["name"] == best_name)
        print(f"   лучший по walk-forward: {best_name}  {wf[best_name][0]:.2f}%\n")
        weeks = {}
        for k in range(14, len(dates)):
            te = d_all[d_all.date == dates[k]].reset_index(drop=True)
            if len(te) < 100:
                continue
            r = run(te, c)
            if r["n"] and np.isfinite(r.get("wr", np.nan)):
                wk = pd.Timestamp(dates[k]).isocalendar().week
                weeks.setdefault(wk, []).extend(r["win"].tolist())
        bad = 0
        for wk, lst in sorted(weeks.items()):
            a = np.array(lst)
            if len(a) < 10:
                continue
            r = a.mean() * 100
            x, dd = drawdown(a, 5)
            if r <= BE:
                bad += 1
            print(f"   неделя {wk}: {r:5.1f}%  n={len(a):<4} ×{x:.2f} "
                  f"{'✓' if r > BE else '✗'}")
        print(f"\n   недель ниже порога: {bad}/{len(weeks)}")

        arr = wf[best_name][3]
        run_loss, cur = 0, 0
        for w in arr:
            cur = 0 if w else cur + 1
            run_loss = max(run_loss, cur)
        print(f"   макс. серия поражений подряд: {run_loss}")
        rng = np.random.default_rng(0)
        boots = [rng.choice(arr, len(arr), replace=True).mean() * 100
                 for _ in range(3000)]
        print(f"   бутстрэп 95% ДИ: {np.percentile(boots,2.5):.2f}% … "
              f"{np.percentile(boots,97.5):.2f}%")
        print(f"   доля выборок ниже порога: {(np.array(boots) < BE).mean()*100:.2f}%")


if __name__ == "__main__":
    main()
