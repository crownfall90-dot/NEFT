"""Пересчёт перевеса с реальной комиссией predict.fun.

Тейкер платит: fee_per_share = 2% × min(price, 1 - price), скидка 10% по инвайту.
При цене ~0.46 это ≈0.92¢ на акцию — прямо в точке максимума кривой.
Мейкер комиссии не платит и получает 25% ребейта от тейкерской в up/down.

    python scripts/updown_fees.py --days 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from scripts.updown_reversion import prep, trade
from scripts.updown_data import load

CFG = dict(mom_col="mom10", thr=3.0, use_pos=False)


def taker_fee(price: float, discount: float = 0.10) -> float:
    """$ комиссии на 1 акцию номиналом $1."""
    return 0.02 * min(price, 1 - price) * (1 - discount)


def ev_per_share(wr_pct: float, price: float, *, taker: bool,
                 discount: float = 0.10) -> float:
    """Ожидаемая прибыль на 1 акцию (номинал $1), с учётом комиссии."""
    p = wr_pct / 100
    fee = taker_fee(price, discount) if taker else 0.0
    # выигрыш: получаем 1.00, потратили price; проигрыш: теряем price
    return p * (1 - price) - (1 - p) * price - fee


def breakeven_wr(price: float, *, taker: bool, discount: float = 0.10) -> float:
    """Винрейт, при котором EV = 0."""
    fee = taker_fee(price, discount) if taker else 0.0
    return (price + fee) * 100


def sim(outcomes, price, stake_pct, *, taker: bool, start=1000.0, max_trades=None):
    """Ставка = stake_pct% депозита, комиссия списывается при покупке."""
    bal = start
    peak, dd = start, 0.0
    fee_share = taker_fee(price, 0.10) if taker else 0.0
    total_fees = 0.0
    for k, o in enumerate(outcomes):
        if max_trades and k >= max_trades:
            break
        budget = bal * stake_pct / 100
        # на budget покупаем shares по (price + fee) за штуку
        cost_per = price + fee_share
        shares = budget / cost_per
        total_fees += shares * fee_share
        payout = shares * 1.0 if o == "win" else (shares * 0.5 * price if o == "tie" else 0.0)
        bal += payout - budget
        peak = max(peak, bal)
        dd = min(dd, (bal - peak) / peak * 100)
        if bal < start * 0.01:
            return 0.0, dd, k + 1, total_fees
    return bal / start, dd, len(outcomes), total_fees


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    d = prep(df)
    sig = trade(d, **CFG)
    outs = sig.outcome.tolist()
    w = outs.count("win"); l = outs.count("loss")
    wr = w / (w + l) * 100
    n = len(outs)

    print(f"винрейт стратегии: {wr:.2f}%  на {n} сделках\n")
    print("КОМИССИЯ predict.fun: тейкер 2% × min(p, 1-p), −10% по инвайту")
    print("                      мейкер 0% + 25% ребейт от тейкерской\n")

    print("─" * 72)
    print("1. ПОРОГ БЕЗУБЫТКА С УЧЁТОМ КОМИССИИ")
    print(f"{'цена':>6} {'fee/шт':>8} {'порог тейкер':>14} {'порог мейкер':>14} {'перевес(T)':>12}")
    for p in (0.44, 0.46, 0.48, 0.50, 0.52):
        f = taker_fee(p)
        bt = breakeven_wr(p, taker=True)
        bm = breakeven_wr(p, taker=False)
        print(f"{p:>6.2f} {f*100:>7.3f}¢ {bt:>13.2f}% {bm:>13.2f}% "
              f"{wr - bt:>+11.2f}")

    print("\n2. КРИТИЧЕСКАЯ ЦЕНА (где перевес обнуляется)")
    for taker in (True, False):
        lo, hi = 0.30, 0.70
        for _ in range(60):
            mid = (lo + hi) / 2
            if breakeven_wr(mid, taker=taker) < wr:
                lo = mid
            else:
                hi = mid
        role = "тейкер" if taker else "мейкер"
        print(f"   {role}: покупать дешевле {lo:.4f}  "
              f"(запас от 0.46: {(lo-0.46)*100:+.2f}¢)")

    print("\n3. EV НА СДЕЛКУ (на $100 ставки)")
    for p in (0.46, 0.48, 0.50):
        for taker in (True, False):
            ev = ev_per_share(wr, p, taker=taker)
            shares = 100 / (p + (taker_fee(p) if taker else 0))
            role = "тейкер" if taker else "мейкер"
            print(f"   цена {p:.2f} {role:7}: EV = ${ev*shares:+6.2f} на $100")

    print("\n4. РОСТ ДЕПОЗИТА С КОМИССИЕЙ (цена 0.46)")
    print(f"{'ставка':>7} {'роль':>8} {'100 сд.':>9} {'300 сд.':>9} {'просадка':>10} {'комиссий':>10}")
    for stake in (0.5, 1.0, 2.0, 3.0):
        for taker in (True, False):
            x1, _, _, _ = sim(outs, 0.46, stake, taker=taker, max_trades=100)
            x3, dd, _, fees = sim(outs, 0.46, stake, taker=taker, max_trades=300)
            role = "тейкер" if taker else "мейкер"
            print(f"{stake:>6}% {role:>8} {x1:>8.3f}× {x3:>8.3f}× "
                  f"{dd:>9.1f}% ${fees:>9.0f}")

    print("\n5. ЦЕЛЬ ×1.8–2.0 С КОМИССИЕЙ (цена 0.46, тейкер)")
    for stake in (1.0, 2.0, 3.0):
        for target in (1.8, 2.0):
            bal, k, hit = 1000.0, 0, None
            fee_share = taker_fee(0.46)
            for o in outs:
                budget = bal * stake / 100
                shares = budget / (0.46 + fee_share)
                bal += (shares if o == "win" else 0.0) - budget
                k += 1
                if bal >= 1000 * target:
                    hit = k
                    break
            if hit:
                print(f"   ставка {stake}% → ×{target}: {hit} сделок "
                      f"(~{hit/97.1:.1f} суток)")
            else:
                print(f"   ставка {stake}% → ×{target}: НЕ достигнуто за {n} сделок "
                      f"(итог ×{bal/1000:.2f})")

    print("\n6. ЗАПАС ПРОЧНОСТИ: насколько может упасть винрейт")
    for p in (0.46, 0.48):
        be = breakeven_wr(p, taker=True)
        print(f"   цена {p}: порог {be:.2f}%, винрейт {wr:.2f}% → "
              f"запас {wr-be:.2f} п.п.")
        se = np.sqrt(0.25 / n) * 100
        print(f"      это {(wr-be)/se:.1f}σ от текущей оценки "
              f"(σ={se:.2f} п.п. при n={n})")


if __name__ == "__main__":
    main()
