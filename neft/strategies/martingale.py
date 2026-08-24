"""Мартингейл: после убытка удваиваем лот, после прибыли возвращаемся к базовому.

Логика входа намеренно простая — направление предыдущей свечи. Суть мартингейла
не во входе, а в управлении размером: серия убытков компенсируется одной удачной
сделкой увеличенного объёма.

Опасность встроена в саму схему: объём растёт как 2^n, а депозит конечен.
Параметр max_steps ограничивает серию — при его достижении лот сбрасывается,
и убыток серии фиксируется вместо попытки отыграть его любой ценой.
"""
from neft.core.models import Side
from neft.core.strategy import Bar, ClosedTrade, Signal, Strategy


class Martingale(Strategy):
    name = "martingale"

    def __init__(
        self,
        base_volume: float = 0.01,
        risk_pct: float | None = None,   # если задан — базовый лот считается от риска
        risk_manager=None,
        tp_pips: float = 10,
        sl_pips: float = 10,
        multiplier: float = 2.0,
        max_steps: int = 6,
        point: float = 1e-05,
        pip: float = 1e-04,
    ):
        self.base_volume = base_volume
        self.risk_pct = risk_pct
        self.risk_manager = risk_manager
        self.equity = 0.0
        self.tp_pips = tp_pips
        self.sl_pips = sl_pips
        self.multiplier = multiplier
        self.max_steps = max_steps
        self.point = point
        self.pip = pip

        self.volume = base_volume
        self.step = 0
        self.blocked_by_risk = 0   # сколько раз потолок риска обрезал серию            # длина текущей серии убытков
        self.max_step_seen = 0
        self.resets = 0          # сколько раз серия упёрлась в потолок
        self._prev: Bar | None = None

    def set_equity(self, equity: float) -> None:
        """Депозит меняется — базовый лот пересчитывается под него."""
        self.equity = equity

    def _base_lot(self, spread_points: float = 0.0) -> float:
        """Реальная дистанция до стопа = SL плюс спред: вход по ask, выход по bid.
        Не учесть спред здесь — значит систематически завышать объём."""
        if self.risk_pct is None or self.risk_manager is None or not self.equity:
            return self.base_volume
        sl_distance = self.sl_pips * self.pip + spread_points * self.point
        return self.risk_manager.volume_for_risk(
            self.equity, sl_distance, risk_pct=self.risk_pct
        )

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        prev, self._prev = self._prev, bar
        if in_position or prev is None:
            return None

        # Вход по направлению предыдущей свечи.
        if bar.close > bar.open:
            side = Side.BUY
        elif bar.close < bar.open:
            side = Side.SELL
        else:
            return None

        tp_dist = self.tp_pips * self.pip
        sl_dist = self.sl_pips * self.pip
        if side is Side.BUY:
            sl, tp = bar.close - sl_dist, bar.close + tp_dist
        else:
            sl, tp = bar.close + sl_dist, bar.close - tp_dist

        base = self._base_lot(bar.spread_points)
        volume = round(base * self.multiplier ** self.step, 2)
        return Signal(
            side=side,
            volume=volume,
            sl=sl,
            tp=tp,
            reason=f"шаг {self.step}",
        )

    def on_signal_rejected(self, signal, reason: str) -> None:
        """Потолок риска обрезал серию. Отыгрываться дальше нечем — сбрасываем
        объём к базовому и фиксируем убыток серии."""
        if "риск" in reason:
            self.blocked_by_risk += 1
            self.step = 0
            self.volume = self._base_lot()

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        if trade.pnl > 0:
            self.volume = self._base_lot()
            self.step = 0
            return

        self.step += 1
        self.max_step_seen = max(self.max_step_seen, self.step)
        if self.step >= self.max_steps:
            # Потолок серии: фиксируем убыток и начинаем заново.
            self.step = 0
            self.resets += 1
        self.volume = round(self._base_lot() * self.multiplier ** self.step, 2)
