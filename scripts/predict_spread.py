"""Реальные цены покупки BTC up/down: bid/ask, спред, ёмкость.

Ключевая проверка. В бэктесте цена контракта бралась 0.46 — но это BID
(цена продажи). Покупатель платит ASK, который заметно выше. Порог
безубытка считается от ЦЕНЫ ПОКУПКИ, поэтому спред напрямую съедает перевес.

Винрейт стратегии 52.3% (walk-forward 52.9%). Комиссия тейкера
2%*min(p,1-p) со скидкой 10%. Отсюда критическая цена покупки ~0.516:
дороже — торговля в минус.

    python scripts/predict_spread.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from scripts.predict_probe import MAIN, api_key, find_updown

WR = 52.3          # винрейт стратегии, %
WR_WF = 52.9       # он же по walk-forward


def taker_fee(p: float, disc: float = 0.10) -> float:
    return 0.02 * min(p, 1 - p) * (1 - disc)


def breakeven(price: float) -> float:
    """Винрейт, нужный чтобы выйти в ноль при покупке по этой цене."""
    return (price + taker_fee(price)) * 100


def px(o):
    return None if not o else float(o.get("price"))


def sz(o):
    return 0.0 if not o else float(o.get("size", 0))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="bitcoin")
    args = ap.parse_args()

    key = api_key()
    if not key:
        print("нет PREDICT_API_KEY в .env")
        return

    ms = [m for m in find_updown(MAIN, key)
          if args.asset in (m.get("categorySlug") or "")]
    print(f"рынков {args.asset}: {len(ms)}\n")

    rows = []
    for m in ms:
        outs = {o["name"]: o for o in m.get("outcomes", [])}
        up, dn = outs.get("Up", {}), outs.get("Down", {})
        ub, ua = px(up.get("bestBid")), px(up.get("bestAsk"))
        db, da = px(dn.get("bestBid")), px(dn.get("bestAsk"))
        if ua is None and da is None:
            continue
        rows.append({
            "slug": (m.get("categorySlug") or "")[:42],
            "up_bid": ub, "up_ask": ua, "dn_bid": db, "dn_ask": da,
            "up_ask_sz": sz(up.get("bestAsk")), "dn_ask_sz": sz(dn.get("bestAsk")),
        })

    print("=" * 96)
    print("ЦЕНЫ: bid = продать, ASK = КУПИТЬ (нас интересует ask)")
    print("=" * 96)
    print(f"{'рынок':<44} {'Up ask':>7} {'Down ask':>9} {'спред':>7} "
          f"{'порог б/у':>10} {'перевес':>9}")
    edges = []
    for r in rows[:26]:
        ua, da = r["up_ask"], r["dn_ask"]
        best = min([x for x in (ua, da) if x is not None], default=None)
        if best is None:
            continue
        spread = None
        if r["up_bid"] is not None and ua is not None:
            spread = ua - r["up_bid"]
        be = breakeven(best)
        edge = WR - be
        edges.append(edge)
        print(f"{r['slug']:<44} {str(ua):>7} {str(da):>9} "
              f"{('%.2f' % spread) if spread is not None else '—':>7} "
              f"{be:>9.2f}% {edge:>+8.2f}")

    if edges:
        arr = np.array(edges)
        print("\n" + "=" * 96)
        print("ИТОГ")
        print("=" * 96)
        print(f"   рынков с котировками: {len(arr)}")
        print(f"   медианный перевес: {np.median(arr):+.2f} п.п.")
        print(f"   рынков с ПОЛОЖИТЕЛЬНЫМ перевесом: {(arr > 0).sum()}/{len(arr)}")
        print(f"   рынков в минус: {(arr <= 0).sum()}/{len(arr)}")

    print("\n" + "=" * 96)
    print("ЧТО ЗНАЧИТ ЦЕНА ПОКУПКИ ДЛЯ СТРАТЕГИИ")
    print("=" * 96)
    print(f"{'цена покупки':>13} {'комиссия':>10} {'порог б/у':>11} "
          f"{'перевес(52.3%)':>15} {'вердикт':>10}")
    for p in (0.46, 0.48, 0.49, 0.50, 0.51, 0.52, 0.55):
        be = breakeven(p)
        e = WR - be
        print(f"{p:>13.2f} {taker_fee(p)*100:>9.3f}¢ {be:>10.2f}% "
              f"{e:>+14.2f} {'прибыль' if e > 0 else 'УБЫТОК':>10}")

    lo, hi = 0.30, 0.70
    for _ in range(60):
        mid = (lo + hi) / 2
        if breakeven(mid) < WR:
            lo = mid
        else:
            hi = mid
    print(f"\n   критическая цена покупки: {lo:.4f}")
    print(f"   дороже — стратегия убыточна при винрейте {WR}%")
    if rows:
        asks = [r["up_ask"] for r in rows if r["up_ask"] is not None]
        if asks:
            med = float(np.median(asks))
            print(f"   медианный Up ask на рынке: {med:.4f} "
                  f"→ {'ПРОХОДИТ' if med < lo else 'НЕ ПРОХОДИТ'}")


if __name__ == "__main__":
    main()
