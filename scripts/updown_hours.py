"""Торговое окно по часам: заданное 8-20 и поиск оптимального.

ОСТОРОЖНО С ПОДГОНКОЙ. Ранее скан показал час 02:00 UTC с винрейтом 62%
на 502 наблюдениях — заманчиво, но механизма за этим нет, и такой выбор
почти наверняка не переносится. Поэтому здесь:
  * окна оцениваются на ПЕРВЫХ 60% данных,
  * лучшее проверяется на отложенных 40%, которых не было при выборе,
  * отдельно считается, сколько окон «работает» случайно.

Время данных — UTC. Локальное время пользователя UTC+3 (Москва),
поэтому 8-20 по местному = 5-17 UTC. Считаем оба варианта.

    python scripts/updown_hours.py --days 60
"""
from __future__ import annotations

import argparse
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
TZ_OFFSET = 3          # локальное время пользователя = UTC+3


def trades(df: pd.DataFrame) -> pd.DataFrame:
    """Сделки стратегии с отметкой часа (UTC) и дня недели."""
    d = prep(df)
    r = evaluate(d, build_mask(d, **CFG), CFG["mom_col"])
    if not r.get("n"):
        return pd.DataFrame()
    idx = r["idx"]
    return pd.DataFrame({
        "t": d.time.to_numpy()[idx],
        "win": r["win"],
        "hour": d.time.dt.hour.to_numpy()[idx],
        "dow": d.time.dt.dayofweek.to_numpy()[idx],
    })


def wr(s: pd.DataFrame) -> tuple[float, int, float]:
    if s.empty:
        return float("nan"), 0, float("nan")
    n = len(s)
    w = s.win.mean() * 100
    return w, n, np.sqrt(0.25 / n) * 100


def window_mask(s: pd.DataFrame, lo: int, hi: int) -> pd.Series:
    """Окно [lo, hi) по часам UTC, с переходом через полночь."""
    if lo <= hi:
        return (s.hour >= lo) & (s.hour < hi)
    return (s.hour >= lo) | (s.hour < hi)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)   # только будни
    s = trades(df)
    if s.empty:
        print("нет сделок")
        return

    base_wr, base_n, base_se = wr(s)
    print(f"данные: {s.t.min()} -> {s.t.max()}  (только будни, время UTC)")
    print(f"база (круглосуточно): {base_wr:.2f}%  n={base_n}  "
          f"+-{2*base_se:.2f}")
    print(f"порог безубытка: {BE:.2f}%\n")

    print("=" * 78)
    print("1. ЗАДАННОЕ ОКНО 8-20")
    print("=" * 78)
    for lbl, lo, hi in [
        ("08-20 UTC", 8, 20),
        (f"08-20 местного (UTC+{TZ_OFFSET}) = {8-TZ_OFFSET:02d}-"
         f"{20-TZ_OFFSET:02d} UTC", 8 - TZ_OFFSET, 20 - TZ_OFFSET),
    ]:
        sub = s[window_mask(s, lo, hi)]
        w, n, se = wr(sub)
        share = n / base_n
        print(f"   {lbl}")
        print(f"      винрейт {w:.2f}%  n={n} ({share:.0%} сделок)  "
              f"+-{2*se:.2f}  vs база {w-base_wr:+.2f} п.п.")

    print("\n" + "=" * 78)
    print("2. ВИНРЕЙТ ПО ЧАСАМ (весь период, для ориентира)")
    print("=" * 78)
    g = s.groupby("hour").win.agg(["mean", "count"])
    g["wr"] = g["mean"] * 100
    g["se"] = np.sqrt(0.25 / g["count"]) * 100
    for h, r in g.iterrows():
        bar = "#" * max(0, int((r.wr - 44) * 2))
        loc = (h + TZ_OFFSET) % 24
        print(f"   {h:02d} UTC ({loc:02d} мск)  {r.wr:5.2f}% "
              f"+-{2*r.se:4.2f}  n={int(r['count']):>4}  {bar}")

    print("\n" + "=" * 78)
    print("3. ПОИСК ОКНА: подбор на первых 60%, проверка на отложенных 40%")
    print("=" * 78)
    cut = s.t.quantile(0.6)
    tr, te = s[s.t < cut], s[s.t >= cut]
    print(f"   подбор:   {tr.t.min()} -> {tr.t.max()}  ({len(tr)} сделок)")
    print(f"   проверка: {te.t.min()} -> {te.t.max()}  ({len(te)} сделок)\n")

    rows = []
    for lo in range(24):
        for length in range(4, 25, 2):
            hi = (lo + length) % 24
            a = tr[window_mask(tr, lo, hi)]
            b = te[window_mask(te, lo, hi)]
            wa, na, _ = wr(a)
            wb, nb, _ = wr(b)
            if na < 200 or nb < 150:
                continue
            rows.append({"окно": f"{lo:02d}-{hi:02d}", "часов": length,
                         "подбор_wr": round(wa, 2), "подбор_n": na,
                         "проверка_wr": round(wb, 2), "проверка_n": nb,
                         "разница": round(wb - wa, 2)})
    res = pd.DataFrame(rows)
    if res.empty:
        print("   недостаточно данных")
        return

    top = res.nlargest(10, "подбор_wr")
    print("   ЛУЧШИЕ 10 ОКОН ПО ПОДБОРУ (и что они дали на проверке):")
    print(top.to_string(index=False))

    best = top.iloc[0]
    print(f"\n   лучшее на подборе: {best['окно']} -> {best.подбор_wr}%")
    print(f"   оно же на проверке: {best.проверка_wr}%  "
          f"({best.разница:+.2f} п.п.)")

    corr = res.подбор_wr.corr(res.проверка_wr)
    print(f"\n   корреляция подбор/проверка по {len(res)} окнам: {corr:+.3f}")
    print("   (около нуля = выбор окна по прошлому не помогает)")

    keep = ((res.подбор_wr > base_wr) & (res.проверка_wr > base_wr)).sum()
    better = (res.подбор_wr > base_wr).sum()
    print(f"   окон лучше базы на подборе: {better}/{len(res)}")
    print(f"   из них удержались на проверке: {keep} "
          f"(ожидалось бы случайно ~{better*0.5:.0f})")

    print("\n" + "=" * 78)
    print("4. ЧЕСТНАЯ ОЦЕНКА ЛУЧШЕГО ОКНА НА ВСЕХ ДАННЫХ")
    print("=" * 78)
    lo_s, hi_s = best["окно"].split("-")
    sub = s[window_mask(s, int(lo_s), int(hi_s))]
    w, n, se = wr(sub)
    print(f"   окно {best['окно']} UTC на всём периоде: {w:.2f}%  n={n}  "
          f"+-{2*se:.2f}")
    print(f"   база круглосуточно: {base_wr:.2f}%  n={base_n}")
    diff = w - base_wr
    need = 2 * np.sqrt(se**2 + base_se**2)
    print(f"   разница {diff:+.2f} п.п., порог значимости +-{need:.2f}")
    print(f"   вывод: {'ЗНАЧИМО' if abs(diff) > need else 'НЕ ЗНАЧИМО'}")


if __name__ == "__main__":
    main()
