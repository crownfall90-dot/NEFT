"""Что происходит с ценой в АКТИВНОМ окне — там, где реально идёт торговля.

Наблюдение из сбора: будущие рынки стоят на заглушке 0.55, а живые цены
появляются только когда окно становится текущим. Значит, оценивать
исполнимость надо по активному рынку, а не по всей выдаче.

Скрипт находит ближайшее по времени BTC-окно и следит только за ним,
записывая полный стакан (не только лучшие цены).

    python scripts/predict_active_window.py --minutes 12
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from scripts.predict_probe import MAIN, api_key

OUT = Path(__file__).resolve().parents[1] / "logs" / "active_window.jsonl"
# Предел прибыльности мейкера при винрейте 52.3%
MAX_OK = 0.52


def markets(key: str) -> list[dict]:
    out, cursor, seen = [], None, set()
    for _ in range(6):
        p = {"first": 100, "status": "OPEN", "marketVariant": "CRYPTO_UP_DOWN"}
        if cursor:
            p["after"] = cursor
        r = requests.get(f"{MAIN}/markets", headers={"x-api-key": key},
                         params=p, timeout=30)
        if r.status_code != 200:
            break
        j = r.json()
        for m in j.get("data", []):
            if m.get("id") in seen:
                continue
            seen.add(m.get("id"))
            if "bitcoin" in (m.get("categorySlug") or ""):
                out.append(m)
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


def orderbook(key: str, mid: int) -> dict | None:
    r = requests.get(f"{MAIN}/markets/{mid}/orderbook",
                     headers={"x-api-key": key}, timeout=25)
    if r.status_code != 200:
        return None
    return r.json().get("data")


def pick_active(ms: list[dict]) -> dict | None:
    """Активным считаем рынок, где стакан не выглядит заглушкой.

    Заглушка — это ask ровно 0.55 и bid 0.46 без движения. Живой рынок
    обычно имеет более узкий спред.
    """
    best, best_spread = None, 9.0
    for m in ms:
        outs = {o["name"]: o for o in m.get("outcomes", [])}
        up = outs.get("Up", {})
        b, a = up.get("bestBid"), up.get("bestAsk")
        if not b or not a:
            continue
        spread = float(a["price"]) - float(b["price"])
        # игнорируем уже решённые рынки (цена у краёв)
        mid_px = (float(a["price"]) + float(b["price"])) / 2
        if mid_px < 0.15 or mid_px > 0.85:
            continue
        if spread < best_spread:
            best, best_spread = m, spread
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=12)
    ap.add_argument("--every", type=int, default=10)
    args = ap.parse_args()

    key = api_key()
    if not key:
        print("нет PREDICT_API_KEY")
        return

    ms = markets(key)
    m = pick_active(ms)
    if not m:
        print("не нашёл активного рынка")
        return
    mid = m["id"]
    print(f"активный рынок: {m.get('categorySlug')}  (id={mid})")
    print(f"слежу {args.minutes} мин, снимок раз в {args.every} сек\n")
    print(f"{'время':>9} {'лучший ask':>11} {'размер':>9} "
          f"{'лучший bid':>11} {'спред':>7}  доступно до 0.52")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    end = time.time() + args.minutes * 60
    hits, total = 0, 0
    with OUT.open("a", encoding="utf-8") as f:
        while time.time() < end:
            ob = orderbook(key, mid)
            if ob:
                asks = sorted(ob.get("asks") or [], key=lambda x: x[0])
                bids = sorted(ob.get("bids") or [], key=lambda x: -x[0])
                rec = {"t": datetime.now(timezone.utc).isoformat(),
                       "id": mid, "asks": asks, "bids": bids}
                f.write(json.dumps(rec) + "\n")
                f.flush()
                total += 1
                if asks:
                    ba, bsz = asks[0][0], asks[0][1]
                    bb = bids[0][0] if bids else None
                    # сколько долларов можно купить не дороже 0.52
                    cheap = sum(p * s for p, s in asks if p <= MAX_OK)
                    if cheap > 0:
                        hits += 1
                    sp = (ba - bb) if bb is not None else float("nan")
                    print(f"{datetime.now().strftime('%H:%M:%S'):>9} "
                          f"{ba:>11.2f} {bsz:>9.1f} "
                          f"{(bb if bb is not None else 0):>11.2f} {sp:>7.2f}"
                          f"   ${cheap:>8.0f}")
            time.sleep(args.every)

    print(f"\nснимков: {total}, из них с доступной ценой <= {MAX_OK}: "
          f"{hits} ({hits/max(total,1):.0%})")
    print(f"данные: {OUT}")


if __name__ == "__main__":
    main()
