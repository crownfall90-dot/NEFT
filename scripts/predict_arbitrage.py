"""Поиск арбитража: сумма ask(Up) + ask(Down) < 1.00.

ИДЕЯ. Контракт платит ровно $1.00 победителю. Если купить обе стороны
дешевле $1.00 суммарно, прибыль гарантирована независимо от исхода —
это не прогноз, а арифметика.

Порог с учётом комиссии тейкера 2%*min(p,1-p) со скидкой 10%:
покупка обеих сторон стоит ask_up + fee(ask_up) + ask_dn + fee(ask_dn),
и эта сумма должна быть меньше 1.00.

    python scripts/predict_arbitrage.py            # по собранным снимкам
    python scripts/predict_arbitrage.py --live 10  # живой поиск 10 минут
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
import requests

from scripts.predict_probe import MAIN, api_key

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "logs" / "book_snapshots.jsonl"
ARB = ROOT / "logs" / "arbitrage_hits.jsonl"


def taker_fee(p: float, disc: float = 0.10) -> float:
    return 0.02 * min(p, 1 - p) * (1 - disc)


def arb_profit(ask_up: float, ask_dn: float) -> float:
    """Прибыль на $1 выплаты при покупке обеих сторон. >0 = арбитраж."""
    cost = ask_up + taker_fee(ask_up) + ask_dn + taker_fee(ask_dn)
    return 1.0 - cost


def scan_snapshots() -> None:
    if not SNAP.exists():
        print(f"нет {SNAP}")
        return
    rows = []
    with SNAP.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    d = pd.DataFrame(rows)
    d = d[d.Up_ask.notna() & d.Down_ask.notna()].copy()
    if d.empty:
        print("нет снимков с обеими котировками")
        return

    d["sum_ask"] = d.Up_ask + d.Down_ask
    d["profit"] = [arb_profit(u, dn) for u, dn in zip(d.Up_ask, d.Down_ask)]

    print(f"снимков с обеими сторонами: {len(d)}")
    print(f"рынков: {d.id.nunique()}\n")

    print("=" * 74)
    print("СУММА ЦЕН ПОКУПКИ (ask Up + ask Down)")
    print("=" * 74)
    print(f"   медиана:  {d.sum_ask.median():.4f}")
    print(f"   минимум:  {d.sum_ask.min():.4f}")
    print(f"   максимум: {d.sum_ask.max():.4f}")
    print(f"   ниже 1.00: {(d.sum_ask < 1.0).sum()} снимков "
          f"({(d.sum_ask < 1.0).mean():.1%})")

    hits = d[d.profit > 0]
    print(f"\n   С УЧЁТОМ КОМИССИИ прибыльных: {len(hits)} "
          f"({len(hits)/len(d):.2%})")

    if len(hits):
        print("\n" + "=" * 74)
        print("НАЙДЕННЫЕ ВОЗМОЖНОСТИ")
        print("=" * 74)
        show = hits.nlargest(15, "profit")[
            ["t", "slug", "Up_ask", "Up_ask_sz", "Down_ask", "Down_ask_sz",
             "sum_ask", "profit"]]
        for r in show.itertuples():
            # сколько можно вложить: ограничено меньшей стороной
            cap = min(r.Up_ask * r.Up_ask_sz, r.Down_ask * r.Down_ask_sz)
            print(f"   {str(r.t)[11:19]}  {r.slug[:38]:<38}")
            print(f"      Up {r.Up_ask:.2f}×{r.Up_ask_sz:.0f}  "
                  f"Down {r.Down_ask:.2f}×{r.Down_ask_sz:.0f}  "
                  f"сумма {r.sum_ask:.3f}  прибыль {r.profit:+.2%}  "
                  f"объём до ${cap:.0f}")
        total = 0.0
        for r in hits.itertuples():
            cap = min(r.Up_ask * r.Up_ask_sz, r.Down_ask * r.Down_ask_sz)
            total += cap * r.profit
        print(f"\n   суммарная теоретическая прибыль по всем находкам: "
              f"${total:.2f}")
        print("   (без учёта того, что одни и те же заявки видны в разных снимках)")
    else:
        print("\n   Арбитражных возможностей НЕ НАЙДЕНО.")
        print("   Маркетмейкер держит сумму цен выше 1.00 — это его заработок.")

    print("\n" + "=" * 74)
    print("РАСПРЕДЕЛЕНИЕ СУММЫ ЦЕН")
    print("=" * 74)
    for lo, hi in [(0, 1.0), (1.0, 1.05), (1.05, 1.1), (1.1, 1.2), (1.2, 9)]:
        n = ((d.sum_ask >= lo) & (d.sum_ask < hi)).sum()
        bar = "#" * int(40 * n / len(d))
        print(f"   {lo:.2f}-{hi:.2f}: {n:>5} ({n/len(d):>5.1%}) {bar}")


def live_scan(minutes: int) -> None:
    key = api_key()
    if not key:
        print("нет PREDICT_API_KEY")
        return
    print(f"живой поиск арбитража {minutes} мин\n")
    end = time.time() + minutes * 60
    found, checks = 0, 0
    ARB.parent.mkdir(parents=True, exist_ok=True)

    while time.time() < end:
        try:
            cursor, seen = None, set()
            for _ in range(4):
                p = {"first": 100, "status": "OPEN",
                     "marketVariant": "CRYPTO_UP_DOWN"}
                if cursor:
                    p["after"] = cursor
                r = requests.get(f"{MAIN}/markets", headers={"x-api-key": key},
                                 params=p, timeout=30)
                if r.status_code != 200:
                    break
                j = r.json()
                for m in j.get("data", []):
                    mid = m.get("id")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    outs = {o["name"]: o for o in m.get("outcomes", [])}
                    ua = outs.get("Up", {}).get("bestAsk")
                    da = outs.get("Down", {}).get("bestAsk")
                    if not ua or not da:
                        continue
                    checks += 1
                    u, dn = float(ua["price"]), float(da["price"])
                    prof = arb_profit(u, dn)
                    if prof > 0:
                        found += 1
                        cap = min(u * float(ua.get("size", 0)),
                                  dn * float(da.get("size", 0)))
                        rec = {"t": datetime.now(timezone.utc).isoformat(),
                               "slug": m.get("categorySlug"), "up": u,
                               "down": dn, "profit": prof, "cap": cap}
                        with ARB.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(rec) + "\n")
                        print(f"  НАЙДЕНО: {m.get('categorySlug')[:44]}  "
                              f"Up {u:.2f} + Down {dn:.2f} = {u+dn:.3f}  "
                              f"прибыль {prof:+.2%}  до ${cap:.0f}")
                cursor = j.get("cursor")
                if not cursor:
                    break
        except Exception as e:  # noqa: BLE001
            print(f"  ошибка: {e}")
        time.sleep(15)

    print(f"\nпроверок: {checks}, найдено возможностей: {found}")
    if checks:
        print(f"частота: {found/checks:.3%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", type=int, help="живой поиск, минут")
    args = ap.parse_args()
    if args.live:
        live_scan(args.live)
    else:
        scan_snapshots()


if __name__ == "__main__":
    main()
