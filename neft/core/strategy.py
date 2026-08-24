"""База для стратегий. Стратегия НЕ отправляет ордера сама — она возвращает
сигнал, а риск-слой решает, пропускать его или нет."""
from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

from neft.core.models import Side


@dataclass(frozen=True)
class Signal:
    side: Side
    volume: float
    sl: float | None = None
    tp: float | None = None
    reason: str = ""
    entry: float | None = None       # None = по рынку
    entry_type: str = "market"       # "market" | "stop"
    expire_bars: int = 3             # сколько баров ждать срабатывания стопа
    # "fixed" — sl/tp заданы заранее; "trigger_bar" — считаются по той свече,
    # которая фактически исполнила отложенный ордер (правило London Breakout).
    sl_mode: str = "fixed"
    rr: float = 2.0                  # используется при sl_mode="trigger_bar"
    sl_buffer: float = 0.0


@dataclass
class Bar:
    time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    spread_points: int
    index: int = 0          # позиция бара в подготовленной таблице
    volume: float = 0.0


@dataclass
class ClosedTrade:
    side: Side
    volume: float
    entry: float
    exit: float
    pnl: float
    reason: str  # 'tp' | 'sl' | 'stop_out'
    bars_held: int
    opened_at: pd.Timestamp | None = None
    closed_at: pd.Timestamp | None = None
    strategy: str = ""


class Strategy(ABC):
    name: str = "base"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Считает индикаторы один раз до прогона. Движок использует
        возвращённую таблицу дальше, поэтому колонки можно добавлять."""
        return df

    @abstractmethod
    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        """Вызывается на каждом закрытом баре. None = ничего не делаем."""

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        """Результат сделки. Мартингейлу это нужно, чтобы удвоить лот."""

    def on_signal_rejected(self, signal: Signal, reason: str) -> None:
        """Риск-слой не пропустил сигнал. Без этого стратегия, наращивающая
        объём, зависает: сигнал отклонён, шаг серии не меняется, и следующий
        сигнал отклоняется снова."""
