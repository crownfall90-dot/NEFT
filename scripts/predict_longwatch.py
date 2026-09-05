"""Долгий сбор стакана активных BTC-рынков: день -> вечер -> окно 19-22 UTC.

Задача — сравнить исполнимость и цены ВНЕ окна и ВНУТРИ него.
Пишет по одной строке на снимок с полным стаканом и меткой,
попадает ли момент в целевое окно.

    python scripts/predict_longwatch.py --hours 6
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from scripts.predict_probe import MAIN, api_key

OUT = Path(__file__).resolve().parents[1] / "logs" / "longwatch.jsonl"
WIN_LO, WIN_HI = 19, 22


def active_btc(key: str) -> list[dict]:
    out, cursor, seen = [], None, set()
    for _ in range(4):
        p = {"first": 100, "status": "OPEN", "marketVariant": "CRYPTO_UP_DOWN"}
        if cursor:
            p["after"] = cursor
        try:
            r = requests.get(f"{MAIN}/markets", headers={"x-api-key": key},
                             params=p, timeout=30)
        except Exception:
            return out
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


def pick(ms: list[dict]) -> dict | None:
    """Активный рынок: узкий спред + исход ещё не определён."""
    best, bs = None, 9.0
    for m in ms:
        o = {x["name"]: x for x in m.get("outcomes", [])}
        up = o.get("Up", {})
        b, a = up.get("bestBid"), up.get("bestAsk")
        if not b or not a:
            continue
        pb, pa = float(b["price"]), float(a["price"])
        if not (0.20 < (pa + pb) / 2 < 0.80):
            continue
        if pa - pb < bs:
            best, bs = m, pa - pb
    return best


def book(key: str, mid: int) -> dict | None:
    try:
        r = requests.get(f"{MAIN}/markets/{mid}/orderbook",
                         headers={"x-api-key": key}, timeout=25)
    except Exception:
        return None
    return r.json().get("data") if r.status_code == 200 else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--every", type=int, default=20)
    args = ap.parse_args()

    key = api_key()
    if not key:
        print("нет PREDICT_API_KEY")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    end = time.time() + args.hours * 3600
    n = 0
    with OUT.open("a", encoding="utf-8") as f:
        while time.time() < end:
            now = datetime.now(timezone.utc)
            m = pick(active_btc(key))
            if m:
                ob = book(key, m["id"])
                if ob:
                    asks = sorted(ob.get("asks") or [], key=lambda x: x[0])
                    bids = sorted(ob.get("bids") or [], key=lambda x: -x[0])
                    if asks:
                        rec = {
                            "t": now.isoformat(),
                            "hour": now.hour,
                            "in_window": WIN_LO <= now.hour < WIN_HI,
                            "slug": m.get("categorySlug"),
                            "asks": asks[:8], "bids": bids[:8],
                            "best_ask": asks[0][0],
                            "best_bid": bids[0][0] if bids else None,
                            "avail_050": sum(p * s for p, s in asks if p <= 0.50),
                            "avail_052": sum(p * s for p, s in asks if p <= 0.52),
                            "avail_055": sum(p * s for p, s in asks if p <= 0.55),
                        }
                        f.write(json.dumps(rec) + "\n")
                        f.flush()
                        n += 1
            time.sleep(args.every)
    print(f"собрано {n} снимков в {OUT}")


if __name__ == "__main__":
    main()
