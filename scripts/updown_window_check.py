"""Проверка окна 19-23 UTC: закономерность или везение.

Поиск окон дал 19-23 UTC (22-02 мск): 55.3% на подборе, 59.1% на отложенных
данных, +4.41 п.п. к базе при пороге значимости 3.47. Выглядит убедительно,
но раньше в этой сессии так же выглядели фильтры, которые потом разваливались.

Здесь проверяем:
  1. держится ли окно по неделям, а не в среднем;
  2. что даёт каждый час окна по отдельности;
  3. не артефакт ли это конкретных дней;
  4. хватает ли перевеса при РЕАЛЬНОЙ цене покупки 0.55.

    python scripts/updown_window_check.py --days 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from scripts.updown_combo import BE, build_mask, evaluate
from scripts.updown_hours import CFG, trades, window_mask, wr
from scripts.updown_data import load

WIN_LO, WIN_HI = 19, 23


def taker_fee(p: float, disc: float = 0.10) -> float:
    return 0.02 * min(p, 1 - p) * (1 - disc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    s = trades(df)
    base_wr, base_n, base_se = wr(s)
    sub = s[window_mask(s, WIN_LO, WIN_HI)].copy()
    w, n, se = wr(sub)

    print(f"окно {WIN_LO:02d}-{WIN_HI:02d} UTC "
          f"({(WIN_LO+3)%24:02d}-{(WIN_HI+3)%24:02d} мск), только будни")
    print(f"винрейт {w:.2f}%  n={n}  +-{2*se:.2f}")
    print(f"база: {base_wr:.2f}%  n={base_n}\n")

    print("=" * 74)
    print("1. ПО НЕДЕЛЯМ (закономерность должна держаться, а не скакать)")
    print("=" * 74)
    sub["week"] = pd.to_datetime(sub.t).dt.isocalendar().week
    bad = 0
    for wk, g in sub.groupby("week"):
        if len(g) < 30:
            continue
        r = g.win.mean() * 100
        if r <= BE:
            bad += 1
        mark = "ok" if r > base_wr else ("-" if r > BE else "ПЛОХО")
        print(f"   неделя {wk}: {r:5.2f}%  n={len(g):>3}  {mark}")
    print(f"\n   недель ниже порога безубытка: {bad}")

    print("\n" + "=" * 74)
    print("2. ПОЧАСОВАЯ РАЗБИВКА ОКНА")
    print("=" * 74)
    for h in range(WIN_LO, WIN_HI):
        g = sub[sub.hour == h]
        if len(g) < 20:
            continue
        r = g.win.mean() * 100
        e = np.sqrt(0.25 / len(g)) * 100
        print(f"   {h:02d} UTC: {r:5.2f}% +-{2*e:4.2f}  n={len(g):>3}")
    print("   (если перевес даёт лишь один час — окно ненадёжно)")

    print("\n" + "=" * 74)
    print("3. УСТОЙЧИВОСТЬ К ИСКЛЮЧЕНИЮ ДНЕЙ (jackknife)")
    print("=" * 74)
    sub["day"] = pd.to_datetime(sub.t).dt.date
    days = sorted(sub.day.unique())
    outs = []
    for d in days:
        g = sub[sub.day != d]
        outs.append(g.win.mean() * 100)
    outs = np.array(outs)
    print(f"   дней в окне: {len(days)}")
    print(f"   винрейт без одного дня: мин {outs.min():.2f}%, "
          f"макс {outs.max():.2f}%")
    print(f"   разброс: {outs.max()-outs.min():.2f} п.п.")
    if outs.min() > base_wr:
        print("   OK: даже без лучшего дня окно выше базы")
    else:
        print("   ВНИМАНИЕ: результат держится на отдельных днях")

    print("\n" + "=" * 74)
    print("4. ГЛАВНОЕ: ХВАТАЕТ ЛИ ПЕРЕВЕСА ПРИ РЕАЛЬНОЙ ЦЕНЕ")
    print("=" * 74)
    print("   Замер стакана: цена покупки (ask) = 0.55, bid = 0.46")
    lo95 = w - 1.96 * se
    for price, role in [(0.55, "тейкер"), (0.46, "мейкер"),
                        (0.47, "мейкер")]:
        if role == "тейкер":
            cost = price + taker_fee(price)
        else:
            cost = price - taker_fee(price) * 0.25
        be = cost * 100
        print(f"   {role} по {price}: порог {be:.2f}%  "
              f"перевес {w-be:+.2f} п.п.  "
              f"(нижняя граница 95% ДИ: {lo95-be:+.2f})")

    print("\n   Напоминание: мейкерские заявки по 0.46-0.47 за 60 снимков")
    print("   активных окон не исполнились ни разу — ликвидность дешевле")
    print("   0.52 отсутствовала полностью.")

    print("\n" + "=" * 74)
    print("5. СКОЛЬКО СДЕЛОК ОСТАЁТСЯ")
    print("=" * 74)
    span = (pd.to_datetime(s.t).max() - pd.to_datetime(s.t).min()).days
    print(f"   круглосуточно: {base_n} сделок за {span} дней "
          f"({base_n/span:.0f}/сутки)")
    print(f"   окно {WIN_LO}-{WIN_HI}: {n} сделок ({n/span:.0f}/сутки)")
    print(f"   отсекается {(1-n/base_n):.0%} сделок")


if __name__ == "__main__":
    main()
