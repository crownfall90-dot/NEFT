"""Экономика мейкерского входа: что нужно, чтобы стратегия стала прибыльной.

Замер живого рынка: медианный ask 0.55, bid 0.46, спред 9 центов.
Как тейкер стратегия убыточна (порог 55.8% против винрейта 52.3%).

Здесь считаем, при каких условиях мейкерский вход даёт плюс, и сколько
сделок мы теряем из-за неисполненных лимитников.

    python scripts/updown_maker.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

WR = 52.3          # винрейт стратегии (walk-forward 52.9%)
REBATE = 0.25      # мейкер получает 25% от тейкерской комиссии


def taker_fee(p: float, disc: float = 0.10) -> float:
    return 0.02 * min(p, 1 - p) * (1 - disc)


def maker_credit(p: float) -> float:
    """Ребейт мейкеру: 25% от тейкерской комиссии на этом уровне цены."""
    return taker_fee(p) * REBATE


def ev_taker(wr: float, price: float) -> float:
    """EV на $1 вложенный, вход по рынку."""
    p = wr / 100
    cost = price + taker_fee(price)
    return (p * 1.0 - cost) / cost


def ev_maker(wr: float, price: float, fill_rate: float = 1.0) -> float:
    """EV на $1, вход лимитником. fill_rate — доля исполнившихся заявок.

    Неисполненные заявки не приносят ни прибыли, ни убытка — просто
    сокращают число сделок. На EV одной СОСТОЯВШЕЙСЯ сделки они не влияют,
    но влияют на доходность за период.
    """
    p = wr / 100
    cost = price - maker_credit(price)
    return (p * 1.0 - cost) / cost


def breakeven(price: float, *, maker: bool) -> float:
    cost = price - maker_credit(price) if maker else price + taker_fee(price)
    return cost * 100


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wr", type=float, default=WR)
    args = ap.parse_args()
    wr = args.wr

    print(f"винрейт стратегии: {wr}%")
    print(f"наблюдаемый рынок: bid 0.46 / ask 0.55 (медиана по 26 рынкам)\n")

    print("=" * 78)
    print("1. ТЕЙКЕР vs МЕЙКЕР ПРИ РАЗНЫХ ЦЕНАХ")
    print("=" * 78)
    print(f"{'цена':>6} {'порог тейкер':>14} {'перевес':>9} "
          f"{'порог мейкер':>14} {'перевес':>9}")
    for p in (0.44, 0.46, 0.48, 0.50, 0.51, 0.52, 0.55):
        bt = breakeven(p, maker=False)
        bm = breakeven(p, maker=True)
        print(f"{p:>6.2f} {bt:>13.2f}% {wr-bt:>+8.2f} "
              f"{bm:>13.2f}% {wr-bm:>+8.2f}")

    print("\n" + "=" * 78)
    print("2. КРИТИЧЕСКАЯ ЦЕНА")
    print("=" * 78)
    for maker in (False, True):
        lo, hi = 0.30, 0.70
        for _ in range(60):
            mid = (lo + hi) / 2
            if breakeven(mid, maker=maker) < wr:
                lo = mid
            else:
                hi = mid
        role = "мейкер" if maker else "тейкер"
        print(f"   {role}: покупать дешевле {lo:.4f}")
    print(f"\n   рыночный ask 0.55 → тейкером НЕЛЬЗЯ")
    print(f"   рыночный bid 0.46 → мейкером МОЖНО (если исполнится)")

    print("\n" + "=" * 78)
    print("3. ДОХОДНОСТЬ МЕЙКЕРА ПРИ РАЗНОЙ ДОЛЕ ИСПОЛНЕНИЯ")
    print("=" * 78)
    print("   (97 сигналов в сутки; неисполненные заявки = пропущенные сделки)")
    print(f"{'fill rate':>10} {'сделок/сутки':>14} {'EV/сделка':>11} "
          f"{'за месяц при ставке 0.25%':>26}")
    for fr in (1.0, 0.75, 0.5, 0.3, 0.2, 0.1):
        ev = ev_maker(wr, 0.46)
        trades = 97 * fr
        # рост за 30 дней при ставке 0.25% от депозита
        n = int(trades * 30)
        growth = (1 + 0.0025 * ev) ** n
        print(f"{fr:>9.0%} {trades:>13.0f} {ev:>+10.2%} {growth:>25.2f}×")

    print("\n" + "=" * 78)
    print("4. ЧТО ЕСЛИ ВСТАВАТЬ НЕ НА 0.46, А ВЫШЕ (быстрее исполнится)")
    print("=" * 78)
    print(f"{'цена лимитника':>16} {'перевес мейкера':>17} {'EV/сделка':>11}")
    for p in (0.46, 0.48, 0.50, 0.51, 0.52, 0.53):
        bm = breakeven(p, maker=True)
        e = wr - bm
        ev = ev_maker(wr, p)
        flag = "" if e > 0 else "  ← убыток"
        print(f"{p:>16.2f} {e:>+16.2f} {ev:>+10.2%}{flag}")

    print("\n" + "=" * 78)
    print("5. СКОЛЬКО ВИНРЕЙТА НУЖНО ПРИ ПОКУПКЕ ПО ASK 0.55")
    print("=" * 78)
    need_t = breakeven(0.55, maker=False)
    need_m = breakeven(0.55, maker=True)
    print(f"   тейкером по 0.55: нужен винрейт > {need_t:.2f}%")
    print(f"   мейкером по 0.55: нужен винрейт > {need_m:.2f}%")
    print(f"   у нас: {wr}%  → не хватает {need_t-wr:.2f} п.п. (тейкер)")
    print(f"\n   чтобы торговать тейкером, винрейт должен вырасти с "
          f"{wr}% до {need_t:.1f}%")
    print(f"   это +{need_t-wr:.1f} п.п. — фильтры такого прироста "
          f"не дали (проверено в updown_final.py)")


if __name__ == "__main__":
    main()
