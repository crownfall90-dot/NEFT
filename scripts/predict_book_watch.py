"""Сбор живого стакана BTC up/down: исполнится ли лимитный ордер мейкера.

Замер показал: медианный ask 0.55 при bid 0.46. Как тейкер стратегия
убыточна (порог 55.8% против винрейта 52.3%). Единственный шанс —
встать мейкером: комиссия 0%, ребейт 25%, покупка по своей цене.

Вопрос, на который отвечает этот скрипт: доходит ли рыночная цена
до уровня, где лимитник исполнится, и как часто.

Пишет снимки в logs/book_snapshots.jsonl для последующего анализа.

    python scripts/predict_book_watch.py --minutes 30
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

OUT = Path(__file__).resolve().parents[1] / "logs" / "book_snapshots.jsonl"


def fetch_markets(key: str, asset: str = "bitcoin") -> list[dict]:
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
            mid = m.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            if asset in (m.get("categorySlug") or ""):
                out.append(m)
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


def snapshot(key: str, asset: str) -> list[dict]:
    now = datetime.now(timezone.utc)
    rows = []
    for m in fetch_markets(key, asset):
        outs = {o["name"]: o for o in m.get("outcomes", [])}
        rec = {"t": now.isoformat(), "id": m.get("id"),
               "slug": m.get("categorySlug")}
        for side in ("Up", "Down"):
            o = outs.get(side, {})
            b, a = o.get("bestBid"), o.get("bestAsk")
            rec[f"{side}_bid"] = float(b["price"]) if b else None
            rec[f"{side}_bid_sz"] = float(b.get("size", 0)) if b else 0.0
            rec[f"{side}_ask"] = float(a["price"]) if a else None
            rec[f"{side}_ask_sz"] = float(a.get("size", 0)) if a else 0.0
        rows.append(rec)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--every", type=int, default=20, help="секунд между снимками")
    ap.add_argument("--asset", default="bitcoin")
    args = ap.parse_args()

    key = api_key()
    if not key:
        print("нет PREDICT_API_KEY в .env")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    end = time.time() + args.minutes * 60
    n = 0
    print(f"сбор стакана {args.asset}, {args.minutes} мин, "
          f"снимок раз в {args.every} сек")
    print(f"пишу в {OUT}\n")

    with OUT.open("a", encoding="utf-8") as f:
        while time.time() < end:
            try:
                rows = snapshot(key, args.asset)
            except Exception as e:  # noqa: BLE001
                print(f"  ошибка: {e}")
                time.sleep(args.every)
                continue
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            n += len(rows)
            live = [r for r in rows if r["Up_ask"] is not None]
            if live:
                sample = min(live, key=lambda r: r["Up_ask"])
                print(f"  {datetime.now().strftime('%H:%M:%S')}  "
                      f"рынков {len(rows)}, снимков всего {n}   "
                      f"лучший Up ask {sample['Up_ask']} "
                      f"(bid {sample['Up_bid']})")
            time.sleep(args.every)

    print(f"\nготово: {n} записей в {OUT}")


if __name__ == "__main__":
    main()
