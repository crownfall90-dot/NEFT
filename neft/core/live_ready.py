"""Готовность к боевому счёту. Ничего не торгует, секреты не отдаёт."""
from __future__ import annotations

from neft.core.config import settings
from neft.core.machine_config import load as load_bot


def checklist() -> dict:
    cfg = load_bot()
    venue = cfg.get("venue") or "crypto"
    keys_bn = bool(settings.binance_api_key and settings.binance_api_secret)
    keys_bb = bool(settings.bybit_api_key and settings.bybit_api_secret)
    mt5_login = bool(settings.mt5_login)
    items = [
        {
            "id": "demo_only",
            "ok": not settings.demo_only,
            "title": "DEMO_ONLY",
            "now": "включён — бой заблокирован" if settings.demo_only
            else "снят",
            "need": "В .env: DEMO_ONLY=false.",
        },
        {
            "id": "crypto_keys",
            "ok": keys_bn or keys_bb,
            "title": "ключи Bybit",
            "now": "есть Bybit" if keys_bb else ("есть Binance" if keys_bn else "нет ключей"),
            "need": "BYBIT_API_KEY/SECRET в .env — только фьючерсы, без вывода.",
        },
        {
            "id": "crypto_testnet",
            "ok": not settings.crypto_testnet,
            "title": "CRYPTO_TESTNET",
            "now": "песочница" if settings.crypto_testnet else "боевая биржа",
            "need": "Для боя: CRYPTO_TESTNET=false.",
        },
        {
            "id": "mt5",
            "ok": mt5_login,
            "title": "MT5",
            "now": "логин задан" if mt5_login else "не задан",
            "need": "Терминал открыт, Алготрейдинг вкл.",
        },
        {
            "id": "risk",
            "ok": float(cfg.get("risk_pct") or 0) <= 1.0
            and int(cfg.get("max_open_positions") or 1) <= 2,
            "title": "риск",
            "now": f"{cfg.get('risk_pct')}% · {cfg.get('max_open_positions')} поз. · "
                   f"день {cfg.get('max_daily_loss_pct')}% · DD {cfg.get('max_drawdown_pct')}%",
            "need": "На первый бой: риск ≤ 1%, 1–2 позиции.",
        },
    ]
    crypto_ok = (not settings.demo_only) and (keys_bn or keys_bb) and (not settings.crypto_testnet)
    mt5_ok = (not settings.demo_only) and mt5_login
    if venue == "crypto":
        ready = crypto_ok
    elif venue == "mt5":
        ready = mt5_ok
    else:
        ready = crypto_ok and mt5_ok
    return {
        "ready": ready,
        "can_press_live": ready,
        "venue": venue,
        "exchange": cfg.get("exchange") or "bybit",
        "demo_only": bool(settings.demo_only),
        "items": items,
        "next": _next(items, venue),
    }


def _next(items: list[dict], venue: str) -> str:
    order = ["demo_only", "crypto_keys", "crypto_testnet"]
    if venue == "mt5":
        order = ["demo_only", "mt5"]
    elif venue == "both":
        order = ["demo_only", "crypto_keys", "crypto_testnet", "mt5"]
    by = {i["id"]: i for i in items}
    for k in order:
        it = by.get(k)
        if it and not it["ok"]:
            return it["need"]
    risk = by.get("risk")
    if risk and not risk["ok"]:
        return risk["need"]
    return "Можно нажать LIVE в панели." if venue != "mt5" else "Можно запускать MT5."
