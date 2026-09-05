"""Анализ собранного стакана: исполнится ли лимитник мейкера.

Логика оценки. Лимитная заявка на покупку по цене P исполняется, когда
кто-то соглашается продать по P или дешевле — то есть когда рыночный ASK
опускается до P. Отслеживаем по снимкам, как часто это происходит.

Пороги из updown_maker.py: лимитник прибылен до 0.52 включительно.

    python scripts/predict_fill_analysis.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

SNAP = Path(__file__).resolve().parents[1] / "logs" / "book_snapshots.jsonl"
LIMITS = [0.46, 0.48, 0.50, 0.51, 0.52]


def load() -> pd.DataFrame:
    rows = []
    with SNAP.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    d = pd.DataFrame(rows)
    d["t"] = pd.to_datetime(d.t)
    return d


def main() -> None:
    if not SNAP.exists():
        print(f"нет файла {SNAP} — сначала predict_book_watch.py")
        return
    d = load()
    print(f"снимков: {len(d)}   рынков: {d.id.nunique()}")
    print(f"период: {d.t.min()} → {d.t.max()}")
    span = (d.t.max() - d.t.min()).total_seconds() / 60
    print(f"длительность наблюдения: {span:.0f} мин\n")

    # Только BTC-рынки с котировками
    d = d[d.slug.str.contains("bitcoin", na=False)]
    d = d[d.Up_ask.notna() | d.Down_ask.notna()]
    if d.empty:
        print("нет BTC-рынков с котировками")
        return

    print("=" * 76)
    print("1. РАСПРЕДЕЛЕНИЕ ЦЕН (что реально стоит в стакане)")
    print("=" * 76)
    for col, lbl in [("Up_ask", "Up ask (цена покупки Up)"),
                     ("Up_bid", "Up bid (цена продажи Up)"),
                     ("Down_ask", "Down ask"), ("Down_bid", "Down bid")]:
        v = d[col].dropna()
        if len(v):
            print(f"   {lbl:28} медиана {v.median():.3f}  "
                  f"мин {v.min():.3f}  макс {v.max():.3f}")
    sp = (d.Up_ask - d.Up_bid).dropna()
    if len(sp):
        print(f"\n   спред Up: медиана {sp.median():.3f}, "
              f"минимальный {sp.min():.3f}")

    print("\n" + "=" * 76)
    print("2. ГЛАВНОЕ: КАК ЧАСТО ASK ОПУСКАЕТСЯ ДО УРОВНЯ ЛИМИТНИКА")
    print("=" * 76)
    print("   (лимитник на покупку по P исполнится, когда ask <= P)")
    print(f"{'лимит':>7} {'Up: снимков с ask<=P':>24} {'доля':>8} "
          f"{'Down':>10} {'доля':>8}")
    for p in LIMITS:
        ua = d.Up_ask.dropna()
        da = d.Down_ask.dropna()
        u_hit = (ua <= p).sum()
        d_hit = (da <= p).sum()
        print(f"{p:>7.2f} {u_hit:>18}/{len(ua):<5} {u_hit/max(len(ua),1):>7.1%} "
              f"{d_hit:>5}/{len(da):<4} {d_hit/max(len(da),1):>7.1%}")

    print("\n" + "=" * 76)
    print("3. ПО ОТДЕЛЬНЫМ РЫНКАМ (5-минутные окна интереснее всего)")
    print("=" * 76)
    per = defaultdict(dict)
    for mid, g in d.groupby("id"):
        slug = g.slug.iloc[0]
        ua = g.Up_ask.dropna()
        if len(ua) < 2:
            continue
        per[slug] = {
            "снимков": len(ua), "мин ask": ua.min(), "медиана": ua.median(),
            "<=0.50": (ua <= 0.50).mean(), "<=0.52": (ua <= 0.52).mean(),
        }
    if per:
        t = pd.DataFrame(per).T.sort_values("мин ask")
        print(t.head(14).to_string())

    print("\n" + "=" * 76)
    print("4. ВЫВОД ПО ИСПОЛНИМОСТИ")
    print("=" * 76)
    ua = d.Up_ask.dropna()
    da = d.Down_ask.dropna()
    both = pd.concat([ua, da])
    fill_52 = (both <= 0.52).mean()
    fill_50 = (both <= 0.50).mean()
    fill_46 = (both <= 0.46).mean()
    print(f"   доля времени, когда ask <= 0.52 (предел прибыльности): "
          f"{fill_52:.1%}")
    print(f"   доля времени, когда ask <= 0.50: {fill_50:.1%}")
    print(f"   доля времени, когда ask <= 0.46: {fill_46:.1%}")
    print()
    if fill_52 < 0.05:
        print("   Лимитник почти никогда не исполнится по прибыльной цене.")
        print("   Мейкерский сценарий НЕ РАБОТАЕТ на этих рынках.")
    elif fill_52 < 0.25:
        print("   Исполнение редкое. Сделок будет мало, но каждая в плюс.")
        print("   Нужен долгий сбор данных, чтобы оценить реальную частоту.")
    else:
        print("   Исполнение реалистично — мейкерский сценарий жизнеспособен.")

    # Оценка месячного роста
    if fill_52 > 0:
        ev = 0.01  # EV при цене около предела прибыльности, консервативно
        trades = 97 * fill_52 * 30
        growth = (1 + 0.0025 * ev) ** trades
        print(f"\n   грубая оценка: {97*fill_52:.0f} сделок/сутки, "
              f"за месяц ~{growth:.2f}× (при ставке 0.25%)")


if __name__ == "__main__":
    main()
