"""Сохраняемые настройки машины — то, что меняется с панели, не секреты.

Секреты остаются в .env. Этот файл — депозит, риск, RR, сессии, пары.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from neft.core.config import ROOT
from neft.core.routing import (
    CRYPTO_STARTER, banned, kit_from_config, norm_crypto_symbol,
)

PATH = ROOT / "data" / "bot_config.json"

ACCOUNT_KEYS = (
    "deposit", "risk_pct", "leverage_crypto",
    "max_daily_loss_pct", "max_drawdown_pct",
    "max_open_positions", "interval_sec",
)

ACCOUNT_DEFAULT: dict = {
    "deposit": 1000.0,
    "risk_pct": 0.5,
    "leverage_crypto": 5,
    "max_daily_loss_pct": 4.0,
    "max_drawdown_pct": 12.0,
    "max_open_positions": 1,
    "interval_sec": 5.0,
}

# У каждого режима свой профиль счёта — тест / демо / LIVE.
MODE_ACCOUNT_DEFAULTS: dict[str, dict] = {
    "paper": {**ACCOUNT_DEFAULT, "risk_pct": 0.5},
    "demo": {**ACCOUNT_DEFAULT, "risk_pct": 1.0},
    "live": {**ACCOUNT_DEFAULT, "risk_pct": 1.0},
}

DEFAULT: dict = {
    "deposit": 1000.0,
    "leverage_mt5": 500,
    "leverage_crypto": 5,
    "objective": "winrate_risk",  # не доходность: винрейт и короткий риск
    "risk_pct": 0.5,           # панель может сменить; в load() 0.1–15%.
    "rr": 1.0,
    "pullback_bars": 2,
    "hss_session": [16, 19],
    "hss_24h": False,
    "london": [11, 16],
    "ny": [16, 23],
    "news_filter": True,
    "news_buffer_min": 5,
    "max_daily_loss_pct": 4.0,
    "max_drawdown_pct": 12.0,
    "max_open_positions": 1,
    "interval_sec": 5.0,
    "venue": "crypto",
    "exchange": "bybit",
    "mt5_symbols": [
        "NAS100", "DJ30", "GER40",
        "EURUSD", "EURUSD+", "XAUUSD", "XAUUSD+", "GBPUSD", "GBPUSD+",
    ],
    "crypto_symbols": list(CRYPTO_STARTER),
    "crypto_routes": {},
    "scanner": {"enabled": True, "top_k": 1, "min_score": 1.5},
    "allow_live": False,
    "news": {
        "gate": True,
        "buffer_min": 5,
        "high_only": True,
        "block_usd_indices": True,
        "block_usd_metals": True,
        "block_usd_crypto": True,
        "block_forex": True,
        "telegram_briefing": True,
    },
    "strategies": {
        "hss": {
            "enabled": True, "rr": 1.0, "tf": "M1", "pullback": 2,
            "session": [16, 19], "all_day": False, "ema": 100,
        },
        "london_sr": {
            "enabled": True, "min_rr": 1.0, "tf": "M1",
            "london": [11, 16], "ny": [16, 23],
        },
        "breakout": {
            "enabled": False, "rr": 1.5, "tf": "M5",
            "box": [10, 16], "entry_from": 16, "entry_until": 18,
        },
        "martingale": {
            "enabled": False, "max_steps": 6, "multiplier": 2.0,
            "tp_pips": 10, "sl_pips": 10,
        },
        "squeeze": {
            "enabled": False, "min_rr": 2.0, "tf": "M5", "min_taps": 2,
        },
        "session_flow": {
            "enabled": True, "rr": 1.0, "tf": "5m",
            "setups": "blend", "gate_regime": True,
        },
        "playbook": {
            "enabled": False, "tf": "5m",
        },
    },
}


def _norm_account(raw: dict, mode: str = "demo") -> dict:
    base = deepcopy(MODE_ACCOUNT_DEFAULTS.get(mode, ACCOUNT_DEFAULT))
    base.update({k: raw[k] for k in ACCOUNT_KEYS if k in raw})
    base["deposit"] = float(base.get("deposit") or ACCOUNT_DEFAULT["deposit"])
    if base["deposit"] <= 0:
        base["deposit"] = ACCOUNT_DEFAULT["deposit"]
    base["risk_pct"] = max(0.1, min(15.0, float(base.get("risk_pct") or 0.5)))
    base["leverage_crypto"] = max(1, min(20, int(base.get("leverage_crypto") or 5)))
    base["max_open_positions"] = max(1, min(8, int(base.get("max_open_positions") or 1)))
    base["max_daily_loss_pct"] = max(0.5, min(50.0, float(base.get("max_daily_loss_pct") or 4)))
    base["max_drawdown_pct"] = max(1.0, min(80.0, float(base.get("max_drawdown_pct") or 12)))
    base["interval_sec"] = max(3.0, min(60.0, float(base.get("interval_sec") or 5)))
    return base


def _account_slice(cfg: dict) -> dict:
    return {k: cfg.get(k, ACCOUNT_DEFAULT.get(k)) for k in ACCOUNT_KEYS}


def _apply_account_to_cfg(cfg: dict, mode: str) -> None:
    acc = cfg.get("accounts", {}).get(mode) or MODE_ACCOUNT_DEFAULTS.get(mode, ACCOUNT_DEFAULT)
    for k in ACCOUNT_KEYS:
        cfg[k] = acc[k]


def _migrate_accounts(cfg: dict) -> None:
    legacy = _account_slice(cfg)
    accounts: dict = dict(cfg.get("accounts") or {})
    for mode in ("paper", "demo", "live"):
        if mode not in accounts:
            accounts[mode] = _norm_account({**MODE_ACCOUNT_DEFAULTS[mode], **legacy}, mode)
        else:
            accounts[mode] = _norm_account(
                {**MODE_ACCOUNT_DEFAULTS[mode], **accounts[mode]}, mode)
    cfg["accounts"] = accounts
    pref = cfg.get("account_mode") or "demo"
    if pref not in accounts:
        pref = "demo"
    cfg["account_mode"] = pref
    _apply_account_to_cfg(cfg, pref)


def account_for_mode(cfg: dict, mode: str) -> dict:
    """Настройки счёта для режима paper | demo | live."""
    mode = mode if mode in MODE_ACCOUNT_DEFAULTS else "demo"
    accounts = cfg.get("accounts") or {}
    return _norm_account(
        {**MODE_ACCOUNT_DEFAULTS[mode], **accounts.get(mode, {})}, mode)


def _norm_session(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return [int(v[0]), int(v[1])]
    if isinstance(v, str) and "-" in v:
        a, b = v.replace("–", "-").split("-", 1)
        return [int(a), int(b)]
    return v


def load() -> dict:
    cfg = deepcopy(DEFAULT)
    if PATH.exists():
        saved = json.loads(PATH.read_text(encoding="utf-8"))
        for k, v in saved.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                merged = deepcopy(cfg[k])
                merged.update(v)
                cfg[k] = merged
            else:
                cfg[k] = v
    _migrate_accounts(cfg)
    cfg["exchange"] = "bybit"
    cfg["rr"] = max(0.5, float(cfg.get("strategies", {}).get("hss", {}).get("rr") or cfg.get("rr") or 1.0))
    cfg["hss_session"] = _norm_session(cfg.get("hss_session")) or [16, 19]
    cfg["london"] = _norm_session(cfg.get("london")) or [11, 16]
    cfg["ny"] = _norm_session(cfg.get("ny")) or [16, 23]
    cfg.setdefault("scanner", {"enabled": True, "top_k": 2, "min_score": 1.2})
    raw_syms = cfg.get("crypto_symbols") or CRYPTO_STARTER
    syms = []
    for s in raw_syms:
        n = norm_crypto_symbol(s)
        if n and not banned(n) and n not in syms:
            syms.append(n)
    if not syms:
        syms = list(CRYPTO_STARTER)
    cfg["crypto_symbols"] = syms
    all_day = bool((cfg.get("strategies") or {}).get("hss", {}).get("all_day")
                   or cfg.get("hss_24h"))
    cfg["hss_24h"] = all_day
    cfg.setdefault("strategies", {}).setdefault("hss", {})["all_day"] = all_day
    kit = kit_from_config(cfg.get("strategies") or {}, hss_24h=all_day)
    cfg["crypto_routes"] = {s: [dict(r) for r in kit] for s in syms}
    return cfg


def save(cfg: dict) -> dict:
    merged = load()
    mode = str(cfg.get("account_mode") or merged.get("account_mode") or "demo")
    if mode not in MODE_ACCOUNT_DEFAULTS:
        mode = "demo"
    incoming_acc = _norm_account({**merged["accounts"][mode], **_account_slice(cfg)}, mode)
    if mode == "live":
        prev = merged["accounts"]["live"].get("deposit")
        if prev is not None:
            incoming_acc["deposit"] = float(prev)
    merged["accounts"][mode] = incoming_acc
    merged["account_mode"] = mode
    _apply_account_to_cfg(merged, mode)
    for k, v in cfg.items():
        if k in ("account_mode", "accounts") or k in ACCOUNT_KEYS:
            continue
        if k == "strategies" and isinstance(v, dict):
            for sk, sv in v.items():
                if isinstance(sv, dict):
                    merged["strategies"][sk] = {**merged["strategies"].get(sk, {}), **sv}
                else:
                    merged["strategies"][sk] = sv
        elif k == "news" and isinstance(v, dict):
            merged["news"] = {**merged.get("news", {}), **v}
        elif k == "scanner" and isinstance(v, dict):
            merged["scanner"] = {
                **{"enabled": True, "top_k": 2, "min_score": 1.2},
                **merged.get("scanner", {}),
                **v,
            }
            merged["scanner"]["enabled"] = bool(merged["scanner"].get("enabled", True))
            merged["scanner"]["top_k"] = max(1, min(8, int(merged["scanner"].get("top_k") or 2)))
            merged["scanner"]["min_score"] = float(merged["scanner"].get("min_score") or 1.2)
        elif k == "crypto_symbols" and isinstance(v, list):
            cleaned = []
            for s in v:
                n = norm_crypto_symbol(s)
                if n and not banned(n) and n not in cleaned:
                    cleaned.append(n)
            merged[k] = cleaned or list(CRYPTO_STARTER)
        else:
            merged[k] = v
    merged["accounts"][mode] = _norm_account(merged["accounts"][mode], mode)
    _apply_account_to_cfg(merged, mode)
    merged["exchange"] = "bybit"
    all_day = bool((merged.get("strategies") or {}).get("hss", {}).get("all_day")
                   or merged.get("hss_24h"))
    merged["hss_24h"] = all_day
    merged.setdefault("strategies", {}).setdefault("hss", {})["all_day"] = all_day
    kit = kit_from_config(merged.get("strategies") or {},
                          hss_24h=bool(merged.get("hss_24h")))
    merged["crypto_routes"] = {s: [dict(r) for r in kit] for s in merged["crypto_symbols"]}
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def hss_session(cfg: dict) -> tuple[int, int] | None:
    if cfg.get("hss_24h"):
        return None
    s = cfg.get("hss_session") or [16, 19]
    return int(s[0]), int(s[1])


def sess(pair) -> tuple[int, int]:
    return int(pair[0]), int(pair[1])
