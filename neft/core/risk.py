"""Риск-слой: последний рубеж между стратегией и деньгами.

Ни один сигнал не попадает на площадку, минуя эти проверки.
"""
import logging
from dataclasses import dataclass, field

from neft.core.strategy import ClosedTrade, Signal

log = logging.getLogger(__name__)


@dataclass
class RiskLimits:
    # Главное правило проекта: риск на сделку всегда в коридоре 0.5–3% депозита.
    risk_per_trade_pct: float = 1.0    # целевой риск, от него считается объём
    max_risk_per_trade_pct: float = 3.0  # жёсткий потолок, выше — отказ
    min_risk_per_trade_pct: float = 0.5  # ниже — сделка не стоит комиссии

    max_volume: float = 1.0            # потолок объёма одной сделки, лот
    max_daily_loss_pct: float = 10.0   # дневной стоп, % от стартового баланса
    max_drawdown_pct: float = 30.0     # общий стоп: kill-switch
    max_open_positions: int = 1
    min_free_margin_pct: float = 20.0  # не входить, если свободной маржи меньше


@dataclass
class RiskManager:
    start_balance: float
    limits: RiskLimits = field(default_factory=RiskLimits)
    # Новостной гейт выключен по умолчанию — существующие бэктесты не должны
    # молча измениться из-за появления нового правила. Включается явно через
    # set_news_gate() в скрипте, где известен часовой пояс данных (MT5-сервер
    # и биржи крипты живут в разных поясах). Инструмент НЕ хранится здесь —
    # один RiskManager в forward.py обслуживает сразу несколько символов
    # (портфельный дневной лимит и просадка на реальном счёте общие), поэтому
    # символ передаётся отдельно на каждый вызов approve()/news_blocked().
    news_gate: object = None
    news_utc_offset_hours: float = 0.0
    _day_start_balance: float = 0.0
    _current_day: object = None
    peak_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    def __post_init__(self):
        self._day_start_balance = self.start_balance
        self.peak_equity = self.start_balance

    def reset_book(self, balance: float) -> None:
        """Бумажный счёт к депозиту: дневной стоп и пик считаются заново."""
        self.start_balance = float(balance)
        self._day_start_balance = float(balance)
        self.peak_equity = float(balance)
        self.halted = False
        self.halt_reason = ""

    def new_day(self, day, balance: float) -> None:
        if day != self._current_day:
            self._current_day = day
            self._day_start_balance = balance

    def update(self, equity: float) -> None:
        self.peak_equity = max(self.peak_equity, equity)
        dd = (self.peak_equity - equity) / self.peak_equity * 100
        if dd >= self.limits.max_drawdown_pct:
            self._halt(f"просадка {dd:.1f}% >= {self.limits.max_drawdown_pct}%")

    def _halt(self, reason: str) -> None:
        if not self.halted:
            self.halted = True
            self.halt_reason = reason
            log.error("KILL-SWITCH: %s", reason)

    def volume_for_risk(
        self, equity: float, sl_distance: float, *,
        contract_size: float = 100_000.0, volume_step: float = 0.01,
        volume_min: float = 0.01, risk_pct: float | None = None,
    ) -> float:
        """Объём из риска, а не наоборот.

        Сколько лотов можно взять, чтобы срабатывание стопа стоило ровно
        risk_pct процентов депозита.
        """
        risk_pct = risk_pct if risk_pct is not None else self.limits.risk_per_trade_pct
        risk_money = equity * risk_pct / 100
        loss_per_lot = sl_distance * contract_size
        if loss_per_lot <= 0:
            return volume_min
        raw = risk_money / loss_per_lot
        stepped = round(raw / volume_step) * volume_step
        return max(volume_min, round(stepped, 2))

    def trade_risk_pct(self, volume: float, sl_distance: float, equity: float,
                       contract_size: float = 100_000.0) -> float:
        """Сколько процентов депозита теряем, если стоп сработает."""
        if equity <= 0:
            return float("inf")
        return volume * sl_distance * contract_size / equity * 100

    def set_news_gate(self, gate, utc_offset_hours: float = 0.0) -> None:
        """Включает проверку по новостям для этого счёта.

        utc_offset_hours — на сколько часов данные, которыми кормится
        бэктест или форвард-тест, опережают UTC (MT5-сервер Bybit: +3;
        биржи крипты через ccxt отдают время уже в UTC, там 0).
        """
        self.news_gate = gate
        self.news_utc_offset_hours = utc_offset_hours

    def news_blocked(self, when, symbol: str | None) -> tuple[bool, str]:
        """Проверка только по новостям — переиспользуется и в approve(), и в
        точке фактического исполнения отложенного ордера (см. engine.py):
        approve() решает, ставить ли ОТЛОЖЕННЫЙ ордер, а срабатывает он позже,
        на другом баре, который approve() не видел."""
        if self.news_gate is None or when is None or symbol is None:
            return False, ""
        import pandas as pd
        when_utc = when - pd.Timedelta(hours=self.news_utc_offset_hours)
        return self.news_gate.blocked(symbol, when_utc)

    def news_pressure(self, when, symbol: str | None,
                      soft_minutes: float = 90.0) -> tuple[float, str]:
        """0..1 для штрафа сканера. Жёсткий блок по-прежнему news_blocked()."""
        if self.news_gate is None or when is None or symbol is None:
            return 0.0, ""
        import pandas as pd
        when_utc = when - pd.Timedelta(hours=self.news_utc_offset_hours)
        pressure = getattr(self.news_gate, "pressure", None)
        if pressure is None:
            return 0.0, ""
        return pressure(symbol, when_utc, soft_minutes=soft_minutes)

    def approve(
        self, signal: Signal, *, equity: float, free_margin: float,
        required_margin: float, open_positions: int,
        sl_distance: float | None = None, contract_size: float = 100_000.0,
        when=None, symbol: str | None = None,
    ) -> tuple[bool, str]:
        """Возвращает (пропустить, причина отказа)."""
        if self.halted:
            return False, f"остановлен: {self.halt_reason}"

        blocked, why = self.news_blocked(when, symbol)
        if blocked:
            return False, f"новость: {why}"

        if open_positions >= self.limits.max_open_positions:
            return False, "лимит открытых позиций"

        if signal.volume > self.limits.max_volume:
            return False, f"объём {signal.volume} > потолка {self.limits.max_volume}"

        # Потолок риска — правило, которое не может обойти ни одна стратегия.
        if sl_distance:
            risk = self.trade_risk_pct(signal.volume, sl_distance, equity, contract_size)
            # Допуск 1e-9: риск ровно по потолку не должен отсекаться
            # накопленной погрешностью float.
            if risk > self.limits.max_risk_per_trade_pct + 1e-9:
                return False, (f"риск {risk:.2f}% > потолка "
                               f"{self.limits.max_risk_per_trade_pct}%")

        day_loss = (self._day_start_balance - equity) / self._day_start_balance * 100
        if day_loss >= self.limits.max_daily_loss_pct:
            return False, f"дневной убыток {day_loss:.1f}%"

        if required_margin > free_margin:
            return False, "не хватает маржи"

        if equity and (free_margin - required_margin) / equity * 100 < \
                self.limits.min_free_margin_pct:
            return False, "свободной маржи меньше порога"

        return True, ""

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        pass
