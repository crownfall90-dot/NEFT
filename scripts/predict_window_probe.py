"""Замер исполнимости мейкерских заявок в окне 19-22 UTC.

ЗАЧЕМ. Анализ по часам (scripts/updown_hours.py) нашёл окно 19-22 UTC
(22-01 мск) с винрейтом 58.04% против базы 52.33%. При цене мейкера 0.47
это перевес +11.25 п.п. с нижней границей 95% ДИ +7.59 — запас есть.

Но прежние замеры стакана шли ДНЁМ и показали 0 исполнений из 60 снимков.
Ночью ликвидность и спред могут отличаться. Этот скрипт снимает стакан
именно в целевом окне и считает, реально ли купить дешевле порога.

Пороги при винрейте 58.04%:
    мейкер прибылен до ~0.575
    тейкер прибылен до ~0.565
Берём консервативно 0.52 (запас на adverse selection) и 0.55.

    python scripts/predict_window_probe.py            # ждёт окна и мерит
    python scripts/predict_window_probe.py --now      # мерит прямо сейчас
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from scripts.predict_probe import MAIN, api_key

OUT = Path(__file__).resolve().parents[1] / "logs" / "window_probe.jsonl"
WIN_LO, WIN_HI = 19, 22          # UTC
LEVELS = (0.50, 0.52, 0.55, 0.57)


def active_btc(key: str) -> list[dict]:
    out, cursor, seen = [], None, set()
    for _ in range(4):
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


def book(key: str, mid: int) -> dict | None:
    r = requests.get(f"{MAIN}/markets/{mid}/orderbook",
                     headers={"x-api-key": key}, timeout=25)
    return r.json().get("data") if r.status_code == 200 else None


def pick(ms: list[dict]) -> dict | None:
    """Рынок с самым узким спредом и неопределённым исходом."""
    best, bs = None, 9.0
    for m in ms:
        o = {x["name"]: x for x in m.get("outcomes", [])}
        up = o.get("Up", {})
        b, a = up.get("bestBid"), up.get("bestAsk")
        if not b or not a:
            continue
        pb, pa = float(b["price"]), float(a["price"])
        mid_px = (pa + pb) / 2
        if mid_px < 0.20 or mid_px > 0.80:
            continue
        if pa - pb < bs:
            best, bs = m, pa - pb
    return best


def measure(key: str, minutes: int) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    end = time.time() + minutes * 60
    stats = {lv: 0 for lv in LEVELS}
    total = 0
    print(f"{'время':>9} {'рынок':<34} {'ask':>6} {'bid':>6} "
          + "".join(f"{'<=' + str(lv):>9}" for lv in LEVELS))
    with OUT.open("a", encoding="utf-8") as f:
        while time.time() < end:
            ms = active_btc(key)
            m = pick(ms)
            if not m:
                time.sleep(10)
                continue
            ob = book(key, m["id"])
            if not ob:
                time.sleep(10)
                continue
            asks = sorted(ob.get("asks") or [], key=lambda x: x[0])
            bids = sorted(ob.get("bids") or [], key=lambda x: -x[0])
            if not asks:
                time.sleep(10)
                continue
            total += 1
            avail = {}
            for lv in LEVELS:
                v = sum(p * s for p, s in asks if p <= lv)
                avail[lv] = v
                if v > 0:
                    stats[lv] += 1
            rec = {"t": datetime.now(timezone.utc).isoformat(),
                   "slug": m.get("categorySlug"), "asks": asks[:6],
                   "bids": bids[:6], "avail": {str(k): v for k, v in avail.items()}}
            f.write(json.dumps(rec) + "\n")
            f.flush()
            print(f"{datetime.now().strftime('%H:%M:%S'):>9} "
                  f"{(m.get('categorySlug') or '')[:34]:<34} "
                  f"{asks[0][0]:>6.2f} {(bids[0][0] if bids else 0):>6.2f} "
                  + "".join(f"${avail[lv]:>8.0f}" for lv in LEVELS))
            time.sleep(12)

    print(f"\nснимков: {total}")
    for lv in LEVELS:
        pct = stats[lv] / max(total, 1)
        print(f"  ликвидность <= {lv}: {stats[lv]}/{total} ({pct:.0%})")
    if total and stats[0.55] == 0:
        print("\n  Ни разу не было цены <= 0.55 — окно не спасает исполнимость.")
    elif total:
        print(f"\n  Есть исполнимые уровни — мейкерский сценарий возможен.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="не ждать окна")
    ap.add_argument("--minutes", type=int, default=25)
    args = ap.parse_args()

    key = api_key()
    if not key:
        print("нет PREDICT_API_KEY")
        return

    now = datetime.now(timezone.utc)
    if not args.now:
        target = now.replace(hour=WIN_LO, minute=2, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        print(f"сейчас {now:%H:%M} UTC, окно {WIN_LO}-{WIN_HI} UTC")
        if wait > 0:
            print(f"ждать {wait/60:.0f} мин. Запустите с --now, "
                  f"чтобы мерить сразу.")
            return
    print(f"замер {args.minutes} мин\n")
    measure(key, args.minutes)


if __name__ == "__main__":
    main()
