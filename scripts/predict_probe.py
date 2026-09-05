"""Замер реальных условий торговли на predict.fun: стакан, ликвидность, тайминг.

Отвечает на вопросы, которые бэктест показать не может:
  1. Какая реальная глубина стакана у 5-минутных BTC-рынков.
  2. Сколько можно поставить, не сдвинув цену выше критической (0.516).
  3. Когда именно закрывается вход перед экспирацией.
  4. Какова фактическая цена Up/Down (в бэктесте предполагалось 0.46).

Ключ берётся из переменной окружения PREDICT_API_KEY или из .env
(строка PREDICT_API_KEY=...). Без ключа mainnet отдаёт 401 — тогда
работает только --testnet, где данные нерелевантны (старые суточные рынки).

    python scripts/predict_probe.py --check          # что доступно
    python scripts/predict_probe.py --markets        # найти 5m BTC рынки
    python scripts/predict_probe.py --depth <id>     # стакан и ёмкость
    python scripts/predict_probe.py --watch <id>     # тайминг закрытия входа
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
MAIN = "https://api.predict.fun/v1"
TEST = "https://api-testnet.predict.fun/v1"

# Критическая цена из бэктеста: дороже — перевес умирает (винрейт 52.9%).
CRITICAL_PRICE = 0.516
ASSUMED_PRICE = 0.46


def api_key() -> str | None:
    k = os.environ.get("PREDICT_API_KEY")
    if k:
        return k.strip()
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("PREDICT_API_KEY"):
                _, _, v = line.partition("=")
                v = v.strip().strip('"').strip("'")
                if v:
                    return v
    return None


def get(path: str, *, base: str, key: str | None, **params):
    h = {"x-api-key": key} if key else {}
    r = requests.get(f"{base}{path}", headers=h, params=params, timeout=30)
    return r


def cmd_check(base: str, key: str | None) -> None:
    print(f"база: {base}")
    print(f"ключ: {'есть' if key else 'НЕТ (mainnet вернёт 401)'}\n")
    for path, params in [("/markets", {"limit": 1}), ("/categories", {}),
                         ("/search", {"query": "BTC"})]:
        try:
            r = get(path, base=base, key=key, **params)
            note = ""
            if r.status_code == 200:
                try:
                    j = r.json()
                    n = len(j.get("data", [])) if isinstance(j, dict) else 0
                    note = f"  записей: {n}"
                except Exception:
                    pass
            print(f"  GET {path:14} → {r.status_code}{note}")
        except Exception as e:  # noqa: BLE001
            print(f"  GET {path:14} → ошибка: {e}")


def find_updown(base: str, key: str | None, limit_pages: int = 12) -> list[dict]:
    """Ищем ОТКРЫТЫЕ крипто up/down рынки.

    Без status=OPEN эндпоинт отдаёт архив (закрытые рынки прошлых месяцев),
    а marketVariant=CRYPTO_UP_DOWN отсекает спорт и прочие шаблоны.
    Пагинация тут first/after, а не limit/cursor.
    """
    found, cursor, seen_ids, seen_cursors = [], None, set(), set()
    for _ in range(limit_pages):
        p = {"first": 100, "status": "OPEN", "marketVariant": "CRYPTO_UP_DOWN"}
        if cursor:
            p["after"] = cursor
        r = get("/markets", base=base, key=key, **p)
        if r.status_code != 200:
            print(f"  /markets → {r.status_code}: {r.text[:200]}")
            return found
        j = r.json()
        for m in j.get("data", []):
            mid = m.get("id")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            found.append(m)
        cursor = j.get("cursor")
        # курсор может повторяться — тогда пагинация зациклилась
        if not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
    return found


def cmd_markets(base: str, key: str | None) -> None:
    ms = find_updown(base, key)
    print(f"открытых крипто up/down рынков: {len(ms)}\n")
    rows = []
    for m in ms:
        outs = {o["name"]: o for o in m.get("outcomes", [])}
        up, dn = outs.get("Up", {}), outs.get("Down", {})
        slug = m.get("categorySlug") or ""
        rows.append({
            "id": m.get("id"), "slug": slug,
            "short": "5m" if "5m" in slug or "5-minutes" in slug else
                     ("15m" if "15" in slug else "?"),
            "up_bid": up.get("bestBid"), "up_ask": up.get("bestAsk"),
            "dn_bid": dn.get("bestBid"), "dn_ask": dn.get("bestAsk"),
        })
    live = [r for r in rows if r["up_ask"] is not None or r["dn_ask"] is not None]
    for r in rows[:30]:
        mark = " ←есть котировки" if r in live else ""
        print(f"  id={r['id']:<9} {r['short']:<4} {r['slug'][:44]:<44} "
              f"Up {str(r['up_bid']):>5}/{str(r['up_ask']):<5} "
              f"Down {str(r['dn_bid']):>5}/{str(r['dn_ask']):<5}{mark}")
    print(f"\nс активными котировками: {len(live)} из {len(rows)}")
    if live:
        print(f"замерить стакан: python scripts/predict_probe.py --depth "
              f"{live[0]['id']}")


def cmd_depth(mid: int, base: str, key: str | None) -> None:
    r = get(f"/markets/{mid}", base=base, key=key)
    if r.status_code != 200:
        print(f"/markets/{mid} → {r.status_code}: {r.text[:300]}")
        return
    m = r.json().get("data", r.json())
    print(f"рынок: {m.get('question')}")
    print(f"slug:  {m.get('categorySlug')}")
    print(f"комиссия feeRateBps: {m.get('feeRateBps')}\n")

    ro = get(f"/markets/{mid}/orderbook", base=base, key=key)
    if ro.status_code != 200:
        print(f"/orderbook → {ro.status_code}: {ro.text[:300]}")
        return
    ob = ro.json()
    ob = ob.get("data", ob)
    print(json.dumps(ob, indent=2)[:1200])

    # Ёмкость: сколько $ можно купить, не перескочив критическую цену.
    for side_name in ("Up", "Down"):
        book = None
        if isinstance(ob, dict):
            book = ob.get(side_name) or ob.get(side_name.lower())
        if not book:
            continue
        asks = book.get("asks") or []
        spent, shares, worst = 0.0, 0.0, 0.0
        for lvl in sorted(asks, key=lambda x: float(x.get("price", 1))):
            px = float(lvl.get("price"))
            sz = float(lvl.get("size", 0))
            if px > CRITICAL_PRICE:
                break
            spent += px * sz
            shares += sz
            worst = px
        print(f"\n{side_name}: до критической {CRITICAL_PRICE} можно купить "
              f"${spent:,.0f} ({shares:,.0f} акций), худшая цена {worst}")
        if spent:
            print(f"   при ставке $2.50 (0.25% от $1000) запас: "
                  f"{spent/2.5:,.0f}× — ёмкости хватает")


def cmd_watch(mid: int, base: str, key: str | None, secs: int = 420) -> None:
    """Следим за рынком до закрытия входа — засекаем момент отсечки."""
    print(f"наблюдение за рынком {mid}, {secs} сек, опрос раз в 5 сек")
    print(f"{'время':>8}  {'статус':<22} {'Up bid/ask':<16} {'Down bid/ask'}")
    t0 = time.time()
    last_status = None
    while time.time() - t0 < secs:
        r = get(f"/markets/{mid}", base=base, key=key)
        if r.status_code != 200:
            print(f"  → {r.status_code}")
            break
        m = r.json().get("data", r.json())
        outs = {o["name"]: o for o in m.get("outcomes", [])}
        up, dn = outs.get("Up", {}), outs.get("Down", {})
        st = up.get("status") or m.get("status")
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        mark = "  ← СМЕНА" if st != last_status else ""
        print(f"{stamp:>8}  {str(st):<22} "
              f"{str(up.get('bestBid')):>6}/{str(up.get('bestAsk')):<8} "
              f"{str(dn.get('bestBid')):>6}/{str(dn.get('bestAsk')):<8}{mark}")
        last_status = st
        time.sleep(5)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--markets", action="store_true")
    ap.add_argument("--depth", type=int)
    ap.add_argument("--watch", type=int)
    ap.add_argument("--testnet", action="store_true")
    ap.add_argument("--secs", type=int, default=420)
    args = ap.parse_args()

    base = TEST if args.testnet else MAIN
    key = api_key()

    if args.check or not any([args.markets, args.depth, args.watch]):
        cmd_check(base, key)
        if not key and not args.testnet:
            print("\nБез ключа mainnet недоступен. Получить: developers.predict.fun")
            print("Затем: положить в .env строку PREDICT_API_KEY=...")
        return
    if args.markets:
        cmd_markets(base, key)
    elif args.depth:
        cmd_depth(args.depth, base, key)
    elif args.watch:
        cmd_watch(args.watch, base, key, args.secs)


if __name__ == "__main__":
    main()
