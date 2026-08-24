"""Единый интерфейс площадки. Стратегия работает только через него
и не знает, MT5 под ней или биржа."""
from abc import ABC, abstractmethod

from neft.core.models import Account, Position, Quote, Side


class Broker(ABC):
    name: str

    @abstractmethod
    def connect(self) -> Account: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def account(self) -> Account: ...

    @abstractmethod
    def quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def positions(self) -> list[Position]: ...

    @abstractmethod
    def market_order(
        self,
        symbol: str,
        side: Side,
        volume: float,
        sl: float | None = None,
        tp: float | None = None,
        comment: str = "",
    ) -> str:
        """Отправляет рыночный ордер, возвращает id сделки."""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()
