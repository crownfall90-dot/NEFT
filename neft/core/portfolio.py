"""Единая машина: несколько стратегий под общим риск-слоем.

Каждая стратегия сама решает, есть ли у неё сетап. Портфель собирает их
сигналы, выбирает по приоритету и запоминает, чья сделка открыта, чтобы
результат вернулся именно её автору.

Позиция всегда одна: две стратегии не могут одновременно тянуть депозит
в разные стороны. Кто первый дал валидный сигнал — того и сделка.
"""
import logging
from dataclasses import dataclass, field

import pandas as pd

from neft.core.strategy import Bar, ClosedTrade, Signal, Strategy

log = logging.getLogger(__name__)


@dataclass
class Slot:
    strategy: Strategy
    name: str
    weight: float = 1.0        # множитель риска для этой стратегии
    enabled: bool = True
    signals: int = 0
    trades: int = 0
    wins: int = 0
    pnl: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades else 0.0


@dataclass
class Portfolio(Strategy):
    """Составная стратегия. Для движка неотличима от обычной."""

    slots: list[Slot] = field(default_factory=list)
    name: str = "portfolio"
    _owner: Slot | None = None

    def add(self, strategy: Strategy, name: str, weight: float = 1.0) -> "Portfolio":
        self.slots.append(Slot(strategy=strategy, name=name, weight=weight))
        return self

    # ── интерфейс стратегии ──────────────────────────────────────────
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        base = df.reset_index(drop=True)
        for s in self.slots:
            s.strategy.prepare(base)     # каждая считает своё, индексы общие
        return base

    def set_equity(self, equity: float) -> None:
        for s in self.slots:
            if hasattr(s.strategy, "set_equity"):
                s.strategy.set_equity(equity)

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        for s in self.slots:
            if not s.enabled:
                continue
            sig = s.strategy.on_bar(bar, in_position)
            if sig is None:
                continue
            s.signals += 1
            self._owner = s
            if s.weight != 1.0:
                sig = Signal(**{**sig.__dict__,
                                "volume": round(sig.volume * s.weight, 2)})
            return sig
        return None

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        owner = self._owner
        if owner is not None:
            owner.trades += 1
            owner.wins += trade.pnl > 0
            owner.pnl += trade.pnl
            owner.strategy.on_trade_closed(trade)
        # Остальным тоже сообщаем: их состояние может зависеть от того,
        # что позиция освободилась.
        for s in self.slots:
            if s is not owner:
                s.strategy.on_trade_closed(trade)
        self._owner = None

    def on_signal_rejected(self, signal: Signal, reason: str) -> None:
        if self._owner is not None:
            self._owner.strategy.on_signal_rejected(signal, reason)
            self._owner = None

    def report(self) -> list[dict]:
        return [{
            "name": s.name, "signals": s.signals, "trades": s.trades,
            "win_rate": s.win_rate, "pnl": s.pnl, "weight": s.weight,
        } for s in self.slots]
