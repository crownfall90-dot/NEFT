"""Почему винрейт упал с 60% и 54% до 52.3%: разбор по периодам.

В сессии было три числа:
  60.00% — трендовая версия, 130 сделок, 3 дня (период подбора)
  54.36% — mean-reversion, 299 сделок, те же 3 дня
  52.30% — mean-reversion, 5729 сделок, 59 дней

Проверяем: это ухудшение стратегии или свойство выборки.

    python scripts/updown_why_lower.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from scripts.updown_combo import BE, build_mask, evaluate
from scripts.updown_wr_hunt import prep
from scripts.updown_data import load

CFG = dict(mom_col="mom10", thr=3.0, vol_min=None, wick_min=None,
           edge=False, atr_min=None)


def wr_on(df: pd.DataFrame) -> tuple[float, int, float]:
    d = prep(df)
    r = evaluate(d, build_mask(d, **CFG), CFG["mom_col"])
    if not r.get("n"):
        return float("nan"), 0, float("nan")
    win = r["win"]
    n = len(win)
    wr = win.mean() * 100
    se = np.sqrt(0.25 / n) * 100
    return wr, n, se


def main() -> None:
    print("=" * 78)
    print("1. ТА ЖЕ СТРАТЕГИЯ НА РАЗНЫХ ОТРЕЗКАХ")
    print("=" * 78)
    full = load("BTCUSDT", days=60)
    full = full[full.time.dt.dayofweek < 5].reset_index(drop=True)

    rows = []
    for days in (3, 7, 14, 30, 59):
        sub = full[full.time >= full.time.max() - pd.Timedelta(days=days)]
        sub = sub.reset_index(drop=True)
        wr, n, se = wr_on(sub)
        rows.append({"период": f"{days} дн", "сделок": n,
                     "винрейт": round(wr, 2), "±2σ": round(2 * se, 2),
                     "нижняя_граница": round(wr - 2 * se, 2)})
    t = pd.DataFrame(rows)
    print(t.to_string(index=False))
    print("\n   Чем короче отрезок, тем шире доверительный интервал.")
    print("   На 3 днях ±2σ около 6 п.п. — 54% и 48% статистически неразличимы.")

    print("\n" + "=" * 78)
    print("2. РАЗБИВКА 59 ДНЕЙ НА ТРЁХДНЕВКИ")
    print("=" * 78)
    print("   Если 54% на 3 днях — закономерность, такие отрезки будут часто.")
    d = prep(full)
    r = evaluate(d, build_mask(d, **CFG), CFG["mom_col"])
    win, idx = r["win"], r["idx"]
    times = d.time.to_numpy()[idx]
    s = pd.DataFrame({"t": times, "win": win})
    s["bucket"] = ((s.t - s.t.min()) // pd.Timedelta(days=3)).astype(int)
    g = s.groupby("bucket").win.agg(["mean", "count"])
    g["wr"] = g["mean"] * 100
    g = g[g["count"] >= 50]
    print(f"   трёхдневок с >=50 сделками: {len(g)}")
    print(f"   средний винрейт по ним: {g.wr.mean():.2f}%")
    print(f"   минимум: {g.wr.min():.2f}%   максимум: {g.wr.max():.2f}%")
    print(f"   отрезков с винрейтом >=54%: {(g.wr >= 54).sum()} "
          f"({(g.wr >= 54).mean():.0%})")
    print(f"   отрезков с винрейтом >=60%: {(g.wr >= 60).sum()} "
          f"({(g.wr >= 60).mean():.0%})")
    print("\n   распределение:")
    for lo, hi in [(0, 45), (45, 50), (50, 52), (52, 55), (55, 60), (60, 100)]:
        c = ((g.wr >= lo) & (g.wr < hi)).sum()
        bar = "#" * c
        print(f"     {lo:>3}-{hi:<3}%: {c:>3} {bar}")

    print("\n" + "=" * 78)
    print("3. СКОЛЬКО СДЕЛОК НУЖНО, ЧТОБЫ ВИНРЕЙТ БЫЛ НАДЁЖЕН")
    print("=" * 78)
    print(f"{'сделок':>8} {'±2σ п.п.':>10}  что это значит")
    for n in (130, 300, 1000, 3000, 5729):
        se = np.sqrt(0.25 / n) * 100
        note = ""
        if n == 130:
            note = "первый отчёт (60%) — истинное значение 51-69%"
        elif n == 300:
            note = "второй отчёт (54%) — истинное значение 48-60%"
        elif n == 5729:
            note = "текущая оценка (52.3%) — истинное 51-54%"
        print(f"{n:>8} {2*se:>9.2f}  {note}")

    print("\n" + "=" * 78)
    print("4. ВЫВОД")
    print("=" * 78)
    wr3, n3, se3 = wr_on(full[full.time >= full.time.max() -
                              pd.Timedelta(days=3)].reset_index(drop=True))
    wr59, n59, se59 = wr_on(full)
    print(f"   на 3 днях:  {wr3:.2f}%  ({n3} сделок, ±{2*se3:.2f})")
    print(f"   на 59 днях: {wr59:.2f}%  ({n59} сделок, ±{2*se59:.2f})")
    print(f"\n   {wr3:.1f}% попадает в интервал 59-дневной оценки: "
          f"{'ДА' if abs(wr3-wr59) < 2*se3 else 'нет'}")
    print("   Стратегия не ухудшилась — просто на 3 днях оценка неточная.")
    print("   52.3% на 5729 сделках надёжнее, чем 60% на 130.")


if __name__ == "__main__":
    main()
