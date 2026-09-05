"""Потолок ёмкости: сколько реально можно заработать до упора в ликвидность.

Walk-forward даёт ×590 за месяц, но это результат сложного процента без учёта
того, что ставка растёт вместе с депозитом. Рынок с оборотом ~$41k не примет
заявку на $11k без сдвига цены, а сдвиг цены убивает перевес
(критическая цена тейкера ≈0.516 при винрейте 52.5%).

Здесь моделируем фиксированный потолок ставки и проскальзывание.

    python scripts/updown_capacity.py --days 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from scripts.updown_fees import breakeven_wr, taker_fee
from scripts.updown_reversion import prep, trade
from scripts.updown_data import load

CFG = dict(mom_col="mom10", thr=3.0, use_pos=False)
MARKET_VOLUME = 41157.0     # оборот одного 5m рынка (со страницы predict.fun)


def slip_price(base: float, stake: float, depth: float) -> float:
    """Грубая модель: цена растёт пропорционально доле съеденной ликвидности."""
    frac = stake / max(depth, 1.0)
    return base + 0.5 * frac      # 50% доли книги → +0.5 к цене (жёстко)


def sim(outs, *, base_price, stake_pct, cap_abs, depth, start=1000.0):
    """cap_abs — жёсткий потолок ставки в $ (ликвидность рынка)."""
    bal = start
    peak, dd = start, 0.0
    fills = []
    for o in outs:
        budget = min(bal * stake_pct / 100, cap_abs)
        px = slip_price(base_price, budget, depth)
        if px >= 0.99:
            continue
        fee = taker_fee(px)
        shares = budget / (px + fee)
        bal += (shares if o == "win" else 0.0) - budget
        peak = max(peak, bal)
        dd = min(dd, (bal - peak) / peak * 100)
        fills.append(px)
        if bal < start * 0.02:
            break
    return bal / start, dd, (np.mean(fills) if fills else base_price)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    d = prep(df)
    sig = trade(d, **CFG)
    outs = sig.outcome.tolist()
    w = outs.count("win")
    wr = w / len([o for o in outs if o in ("win", "loss")]) * 100
    print(f"винрейт {wr:.2f}%, сделок {len(outs)}")
    print(f"оборот одного 5m рынка: ${MARKET_VOLUME:,.0f}")
    print(f"критическая цена тейкера: ~0.516\n")

    print("─" * 70)
    print("1. БЕЗ ПОТОЛКА (как в walk-forward) — почему это нереально")
    x, dd, _ = sim(outs, base_price=0.46, stake_pct=2.0,
                   cap_abs=10**9, depth=10**9)
    print(f"   ×{x:,.1f}  — бесконечная ликвидность, чистая фантазия")
    final_stake = 1000 * x * 0.02
    print(f"   последняя ставка была бы ${final_stake:,.0f} "
          f"при обороте рынка ${MARKET_VOLUME:,.0f}")
    print(f"   это {final_stake/MARKET_VOLUME*100:,.0f}% всего оборота рынка\n")

    print("2. С ПОТОЛКОМ СТАВКИ (реалистично)")
    print(f"{'потолок':>10} {'глубина':>9} {'итог':>12} {'просадка':>10} {'ср.цена':>9}")
    for cap in (50, 100, 250, 500, 1000, 2500):
        depth = MARKET_VOLUME * 0.05      # доступная глубина ~5% оборота
        x, dd, avg = sim(outs, base_price=0.46, stake_pct=2.0,
                         cap_abs=cap, depth=depth)
        print(f"   ${cap:>7,} ${depth:>8,.0f} ×{x:>10,.2f} {dd:>9.1f}% {avg:>8.4f}")

    print("\n3. ГОДОВАЯ ДОХОДНОСТЬ ПРИ ФИКСИРОВАННОМ ПОТОЛКЕ")
    print("   (сколько $ в месяц даёт стратегия, упёршись в ликвидность)")
    n_month = len(outs) / 2      # ~месяц данных
    ev_share = (wr / 100) * 1.0 - (0.46 + taker_fee(0.46))
    for cap in (100, 250, 500, 1000):
        shares = cap / (0.46 + taker_fee(0.46))
        per_trade = ev_share * shares
        print(f"   ставка ${cap:>5,}: EV ${per_trade:>6.2f}/сделка → "
              f"${per_trade * n_month:>9,.0f}/мес при {n_month:.0f} сделках")

    print("\n4. ПРОСКАЛЬЗЫВАНИЕ: при какой ставке перевес умирает")
    depth = MARKET_VOLUME * 0.05
    for stake in (100, 500, 1000, 2000, 4000):
        px = slip_price(0.46, stake, depth)
        be = breakeven_wr(px, taker=True)
        print(f"   ставка ${stake:>5,}: цена входа {px:.4f}, порог {be:.2f}%, "
              f"перевес {wr-be:+.2f} п.п. {'✓' if wr > be else '✗ УБЫТОК'}")

    print("\n5. РЕАЛИСТИЧНЫЙ СЦЕНАРИЙ (депозит $1000, ставка 2%, потолок $500)")
    x, dd, avg = sim(outs, base_price=0.46, stake_pct=2.0,
                     cap_abs=500, depth=MARKET_VOLUME * 0.05)
    print(f"   за ~2 месяца: ×{x:.2f}  просадка {dd:.1f}%  ср.цена {avg:.4f}")
    print(f"   → ×1.8–2.0 достижимо, но потолок роста наступает быстро")


if __name__ == "__main__":
    main()
