"""Комиссии Bybit TradFi (MT5 CFD), режим Tight-Spread.

Источник: help Bybit «TradFi: Fees Explained» + публичные сводки тарифов.
  • Forex / металлы: $6 / лот
  • Нефть / commodities: $3 / лот
  • Крупные индексы: $3 / лот (NAS100, DJ30, GER40, …)
  • CHINA50 ≈ HK50: $1.5 / лот

Комиссия списывается при ОТКРЫТИИ позиции (как у Bybit), не на закрытии.
Zero-Fee mode зашивает стоимость в спред — мы моделируем Tight-Spread
явно, чтобы скальп не рисовал плюс без реальных издержек. Спред из
истории MT5 при этом всё равно учитывается отдельно.
"""
from __future__ import annotations

from neft.backtest.engine import Costs
from neft.core import mt5_symbols as mt5sym

# $/лот при открытии (Tight-Spread).
FOREX_METAL = 6.0
OIL_COMMODITY = 3.0
INDEX_MAJOR = 3.0
INDEX_ASIA = 1.5  # CHINA50 / HK50-класс

_OIL = frozenset({"UKOUSD", "USOUSD"})
_METAL = frozenset({"XAUUSD+", "XAUUSD", "XAGUSD+", "XAGUSD"})
_INDEX = frozenset({
    "NAS100", "DJ30", "GER40", "FRA40", "ES35", "UK100", "CHINA50",
    "US500", "US30", "DE40", "USTEC",
})
_FOREX_SUFFIX = ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF")


def commission_per_lot(symbol: str) -> float:
    """$/лот Bybit TradFi для логического или брокерского тикера."""
    name = mt5sym.canonical(symbol)
    if name in _OIL:
        return OIL_COMMODITY
    if name in _METAL or name.startswith("XAU") or name.startswith("XAG"):
        return FOREX_METAL
    if name == "CHINA50":
        return INDEX_ASIA
    if name in _INDEX:
        return INDEX_MAJOR
    # Форекс: EURUSD+, GBPJPY+, …
    base = name.replace("+", "")
    if len(base) == 6 and base[:3].isalpha() and base[3:].isalpha():
        return FOREX_METAL
    if any(base.endswith(s) or base.startswith(s) for s in _FOREX_SUFFIX):
        if name in _INDEX:
            return INDEX_MAJOR
        return FOREX_METAL
    # Неизвестный CFD — консервативно как индекс.
    return INDEX_MAJOR


def costs_for(spec, *, leverage: int | None = None,
              spread_points: float | None = None) -> Costs:
    """Costs со спредом инструмента + комиссией Bybit (открытие)."""
    rate = commission_per_lot(getattr(spec, "name", "") or "")
    return Costs(
        spread_points=float(
            spread_points if spread_points is not None
            else getattr(spec, "default_spread", 1.0)
        ),
        contract_size=float(getattr(spec, "contract_size", 100_000.0)),
        point=float(getattr(spec, "point", 1e-5)),
        commission_per_lot=rate,
        commission_maker_per_lot=0.0,
        commission_on_close=False,  # Bybit: только при открытии
        leverage=int(leverage if leverage is not None else 500),
    )
