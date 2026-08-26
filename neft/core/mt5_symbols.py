"""Резолв MT5-символов под брокера (Bybit CFD / MetaQuotes-Demo).

Bybit: тикеры как в машине — NAS100, XAUUSD+, EURUSD+.
MetaQuotes-Demo: других имён на индексы часто нет; тогда резолв падает,
и forward требует переключить терминал на Bybit-Live (демо у Bybit нет).
"""
from __future__ import annotations

import MetaTrader5 as mt5

# Полный CFD-набор машины. NAS100 — обязателен для проверки бота.
# HSS M5 · 90д · plus_only (+$153, WR 63%, 67 сделок). Остальные — минус или шум.
HSS_PLUS_SYMBOLS: frozenset[str] = frozenset({
    "DJ30", "FRA40", "XAUUSD+", "UKOUSD", "GBPUSD+",
    "USDJPY+", "USDCAD+", "USDCHF+", "EURJPY+",
})
HSS_WEAK_SYMBOLS: frozenset[str] = frozenset({
    "NAS100", "GER40", "ES35", "UK100", "CHINA50", "USOUSD",
    "EURUSD+", "AUDUSD+", "NZDUSD+", "EURGBP+", "GBPJPY+",
})

CFD_SYMBOLS: list[str] = [
    "NAS100",
    "DJ30",
    "GER40",
    "FRA40",
    "ES35",
    "UK100",
    "CHINA50",
    "XAUUSD+",
    "UKOUSD",
    "USOUSD",
    "EURUSD+",
    "GBPUSD+",
    "USDJPY+",
    "AUDUSD+",
    "NZDUSD+",
    "USDCAD+",
    "USDCHF+",
    "EURJPY+",
    "EURGBP+",
    "GBPJPY+",
]

REQUIRED = ("NAS100",)

# Кандидаты на случай другого брокера (MetaQuotes / IC / …).
_ALIASES: dict[str, tuple[str, ...]] = {
    "NAS100": ("NAS100", "NAS100.f", "USTEC", "US100", "NASDAQ100", "#USNDAQ100"),
    "DJ30": ("DJ30", "US30", "US30.f", "DJIA", "#US30"),
    "GER40": ("GER40", "DE40", "DE40.f", "DAX40", "#DE40"),
    "FRA40": ("FRA40", "CAC40", "FRA40.f", "#FR40"),
    "ES35": ("ES35", "SPA35", "ES35.f", "#ES35"),
    "UK100": ("UK100", "UK100.f", "FTSE100", "#UK100"),
    "CHINA50": ("CHINA50", "CHINAH", "HK50", "#CN50"),
    "XAUUSD+": ("XAUUSD+", "XAUUSD", "XAUUSD.f", "XAUUSDp", "GOLD"),
    "UKOUSD": ("UKOUSD", "UKOIL", "UKOIL.f", "BRENT"),
    "USOUSD": ("USOUSD", "USOIL", "USOIL.f", "WTI", "CL-OIL"),
    "EURUSD+": ("EURUSD+", "EURUSD", "EURUSD.f"),
    "GBPUSD+": ("GBPUSD+", "GBPUSD", "GBPUSD.f"),
    "USDJPY+": ("USDJPY+", "USDJPY", "USDJPY.f"),
    "AUDUSD+": ("AUDUSD+", "AUDUSD", "AUDUSD.f"),
    "NZDUSD+": ("NZDUSD+", "NZDUSD", "NZDUSD.f"),
    "USDCAD+": ("USDCAD+", "USDCAD", "USDCAD.f"),
    "USDCHF+": ("USDCHF+", "USDCHF", "USDCHF.f"),
    "EURJPY+": ("EURJPY+", "EURJPY", "EURJPY.f"),
    "EURGBP+": ("EURGBP+", "EURGBP", "EURGBP.f"),
    "GBPJPY+": ("GBPJPY+", "GBPJPY", "GBPJPY.f"),
}


def candidates(symbol: str) -> tuple[str, ...]:
    s = symbol.strip()
    if s in _ALIASES:
        return _ALIASES[s]
    if s.endswith("+"):
        return (s, s[:-1])
    return (s, s + "+")


def hss_symbol_bias(symbol: str) -> float:
    """Сканер: приоритет plus_only из прогона HSS M5."""
    c = canonical(symbol)
    if c in HSS_PLUS_SYMBOLS:
        return 0.35
    if c in HSS_WEAK_SYMBOLS:
        return -0.6
    return 0.0


def canonical(symbol: str) -> str:
    """Логическое имя машины (для ROUTES / новостей), не тикер брокера."""
    s = symbol.strip()
    for key, alts in _ALIASES.items():
        if s == key or s in alts:
            return key
    return s


def resolve(symbol: str, *, tradable_only: bool = False) -> str | None:
    """Вернуть имя, которое реально есть у текущего брокера, или None."""
    for name in candidates(symbol):
        if not mt5.symbol_select(name, True):
            continue
        info = mt5.symbol_info(name)
        if info is None:
            continue
        if tradable_only and info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
            continue
        return info.name
    return None


def resolve_many(
    symbols: list[str],
    *,
    tradable_only: bool = False,
) -> tuple[list[tuple[str, str]], list[str]]:
    """[(requested, resolved), ...], missing requested."""
    ok: list[tuple[str, str]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        s = raw.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        got = resolve(s, tradable_only=tradable_only)
        if got is None:
            missing.append(s)
        else:
            ok.append((s, got))
    return ok, missing


def bybit_cfd_server(server: str | None) -> bool:
    return bool(server) and "bybit" in server.lower()
