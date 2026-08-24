"""Общие типы для всех площадок: MT5 и крипта говорят на одном языке."""
from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class Account:
    login: str
    server: str
    currency: str
    balance: float
    equity: float
    is_demo: bool
    # Свободно под маржу / уже занято / кошелёк без uPnL (Bybit UTA).
    available: float = 0.0
    used_margin: float = 0.0
    wallet: float = 0.0


@dataclass(frozen=True)
class Position:
    symbol: str
    side: Side
    volume: float
    entry: float
    pnl: float
    ticket: str


class BrokerError(RuntimeError):
    """Любая проблема на стороне площадки: сеть, отказ, запрет предохранителя."""
