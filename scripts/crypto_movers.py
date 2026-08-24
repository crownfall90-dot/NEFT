"""Топ по обороту / росту / падению за 24ч — монеты вне статичного списка,
но с движением прямо сейчас.

Не отдельная стратегия: это только расширение списка пар для сканера. Вход
и выход всё равно решают HSS/London S/R/Flow/Squeeze/Breakout и риск-слой —
как и на основных 23 парах. Разница в том, что у mover'ов нет истории
бэктеста именно по ним: эдж стратегий на них не проверен, только
предполагается, что переносится с похожих по характеру пар.

    python scripts/crypto_movers.py            # топ-10 по каждой категории
    python scripts/crypto_movers.py --top 5
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

MIN_TURNOVER_USD = 5_000_000  # ниже — тонкий стакан, дорогое исполнение
BANNED = {"BTC", "ENA"}  # см. neft/core/routing.py — те же исключения


def fetch_tickers() -> list[dict]:
    url = "https://api.bybit.com/v5/market/tickers?category=linear"
    req = urllib.request.Request(url, headers={"User-Agent": "NEFT"})
    with urllib.request.urlopen(req, timeout=12) as r:
        data = json.loads(r.read())
    return (data.get("result") or {}).get("list") or []


def rank(top: int = 10) -> dict:
    rows = fetch_tickers()
    usable = []
    for r in rows:
        sym = str(r.get("symbol") or "")
        if not sym.endswith("USDT"):
            continue
        coin = sym[:-4]
        if coin in BANNED:
            continue
        try:
            turnover = float(r.get("turnover24h") or 0)
            pct = float(r.get("price24hPcnt") or 0) * 100
            last = float(r.get("lastPrice") or 0)
        except (TypeError, ValueError):
            continue
        if turnover < MIN_TURNOVER_USD or not last:
            continue
        usable.append({"symbol": sym, "coin": coin, "turnover24h": turnover,
                       "pct24h": round(pct, 2), "last": last})

    by_vol = sorted(usable, key=lambda x: -x["turnover24h"])[:top]
    by_gain = sorted(usable, key=lambda x: -x["pct24h"])[:top]
    by_loss = sorted(usable, key=lambda x: x["pct24h"])[:top]
    return {"volume": by_vol, "gainers": by_gain, "losers": by_loss,
            "universe_size": len(usable)}


def merge_into_config(data: dict, cap: int, cfg_path: Path) -> list[str]:
    """Добавляет mover'ов в crypto_symbols поверх текущего списка.

    Не перезапускает бота — уже запущенный процесс список пар на лету не
    перечитывает (см. docstring модуля), новый набор возьмётся со следующего
    старта. Решение когда рестартовать — за пользователем, как и «Стоп».
    """
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    core = list(cfg.get("crypto_symbols") or [])
    have = {s.split("/")[0] for s in core}
    added = []
    for group in ("volume", "gainers", "losers"):
        for r in data[group]:
            if len(core) >= cap:
                break
            if r["coin"] in have or r["coin"] in BANNED:
                continue
            sym = f"{r['coin']}/USDT:USDT"
            core.append(sym)
            have.add(r["coin"])
            added.append(r["coin"])
    cfg["crypto_symbols"] = core
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return added


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", default="")
    p.add_argument("--merge", action="store_true",
                    help="добавить mover'ов в data/bot_config.json (без рестарта бота)")
    p.add_argument("--cap", type=int, default=40,
                    help="потолок общего числа пар после слияния")
    a = p.parse_args()

    data = rank(a.top)
    print(f"ликвидных пар (оборот >= ${MIN_TURNOVER_USD:,.0f}/24ч): {data['universe_size']}\n")
    for label, key in (("ОБЪЁМ 24Ч", "volume"), ("РОСТ 24Ч", "gainers"), ("ПАДЕНИЕ 24Ч", "losers")):
        print(f"=== {label} ===")
        for r in data[key]:
            print(f"  {r['coin']:10} {r['pct24h']:+7.2f}%   оборот ${r['turnover24h']/1e6:8.1f}M   цена {r['last']}")
        print()

    if a.json:
        Path(a.json).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"сохранено -> {a.json}")

    if a.merge:
        added = merge_into_config(data, a.cap, Path("data/bot_config.json"))
        if added:
            print(f"добавлено в crypto_symbols: {', '.join(added)}")
            print("применится при следующем запуске бота (Тест/Демо в панели) — текущий рестарт не трогаем")
        else:
            print("нечего добавлять — все mover'ы уже в списке или потолок исчерпан")


if __name__ == "__main__":
    main()
