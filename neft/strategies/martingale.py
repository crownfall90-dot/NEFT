"""Мартингейл с нормальным входом: тренд EMA + откат, ATR-стоп, TP с запасом
под спред и комиссию Bybit.

Правила:
  1. Сессия (по умолчанию NY cash для индексов) — вне окна не торгуем.
  2. Тренд по EMA: выше — только лонг-сетапы, ниже — только шорт.
  3. Вход на откате к EMA (касание зоны) + закрытие обратно по тренду.
  4. SL = ATR × k; TP ≥ SL + подушка комиссий/спреда, чтобы win перекрывал loss.
  5. После убытка: та же сторона, лот × multiplier (серия).
  6. После прибыли или потолка шагов — сброс к базовому лоту.
  7. Пауза после сделки и после срыва серии — не долбим рынок подряд.

Старый API (tp_pips / sl_pips / tp_points / sl_points) сохранён: если заданы
фиксированные дистанции и use_atr=False — работаем как раньше.
"""
from __future__ import annotations

import pandas as pd

from neft.core.indicators import atr, ema
from neft.core.models import Side
from neft.core.strategy import Bar, ClosedTrade, Signal, Strategy


class Martingale(Strategy):
    name = "martingale"

    def __init__(
        self,
        base_volume: float = 0.01,
        risk_pct: float | None = None,
        risk_manager=None,
        spec=None,
        # Фиксированные дистанции (опционально).
        tp_points: float | None = None,
        sl_points: float | None = None,
        tp_pips: float = 10,
        sl_pips: float = 10,
        multiplier: float = 2.0,
        max_steps: int = 5,
        point: float = 1e-05,
        pip: float = 1e-04,
        contract_size: float | None = None,
        volume_step: float | None = None,
        volume_min: float | None = None,
        # Нормальный режим.
        use_atr: bool = True,
        atr_period: int = 14,
        sl_atr: float = 1.25,
        rr: float = 1.15,
        ema_period: int = 50,
        pullback_atr: float = 0.35,
        session: tuple[int, int] | None = None,
        cooldown_bars: int = 3,
        reset_cooldown_bars: int = 12,
        min_atr: float = 0.0,
        commission_per_lot: float = 0.0,
        # Доп. пункты в TP сверх RR: 2×fee/cs перекрывает round-trip edge.
        cost_pad_mult: float = 2.0,
    ):
        self.base_volume = base_volume
        self.risk_pct = risk_pct
        self.risk_manager = risk_manager
        self.spec = spec
        self.equity = 0.0
        self.multiplier = multiplier
        self.max_steps = max_steps
        self.use_atr = use_atr
        self.atr_period = atr_period
        self.sl_atr = sl_atr
        self.rr = rr
        self.ema_period = ema_period
        self.pullback_atr = pullback_atr
        self.session = session
        self.cooldown_bars = cooldown_bars
        self.reset_cooldown_bars = reset_cooldown_bars
        self.min_atr = min_atr
        self.commission_per_lot = float(commission_per_lot)
        self.cost_pad_mult = cost_pad_mult

        sp = spec
        self.point = float(getattr(sp, "point", None) or point)
        self.pip = float(getattr(sp, "pip", None) or pip)
        self.contract_size = float(
            contract_size if contract_size is not None
            else getattr(sp, "contract_size", 100_000.0)
        )
        self.volume_step = float(
            volume_step if volume_step is not None
            else getattr(sp, "volume_step", 0.01)
        )
        self.volume_min = float(
            volume_min if volume_min is not None
            else getattr(sp, "volume_min", 0.01)
        )

        # Фикс-режим: явные points или старые pips.
        if tp_points is not None or sl_points is not None:
            self._fixed_tp = float(tp_points if tp_points is not None else sl_points)
            self._fixed_sl = float(sl_points if sl_points is not None else tp_points)
            self.use_atr = False
        else:
            self._fixed_tp = tp_pips * self.pip
            self._fixed_sl = sl_pips * self.pip

        self.volume = base_volume
        self.step = 0
        self.series_side: Side | None = None
        self.series_debt = 0.0
        self.blocked_by_risk = 0
        self.max_step_seen = 0
        self.resets = 0
        self.setups_seen = 0
        self.skipped_session = 0
        self.skipped_filter = 0
        self.skipped_cooldown = 0

        self.df: pd.DataFrame | None = None
        self._cooldown_until = -1
        self._last_setup_i = -10**9

    # ── prepare / equity ──────────────────────────────────────────

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy().reset_index(drop=True)
        d["ema"] = ema(d.close, self.ema_period)
        d["atr"] = atr(d, self.atr_period)
        d["hour"] = d.time.dt.hour
        self.df = d
        return d

    def set_equity(self, equity: float) -> None:
        self.equity = equity

    # ── sizing ────────────────────────────────────────────────────

    def _cost_pad(self) -> float:
        """Сколько пунктов цены нужно сверху SL, чтобы win ≥ loss после fee."""
        if self.contract_size <= 0:
            return 0.0
        return self.cost_pad_mult * self.commission_per_lot / self.contract_size

    def _distances(self, i: int, spread_points: float) -> tuple[float, float] | None:
        spread_px = float(spread_points) * self.point
        pad = self._cost_pad() + spread_px
        if self.use_atr and self.df is not None:
            a = float(self.df.iloc[i]["atr"])
            if not a or a != a or a <= 0:
                return None
            if self.min_atr > 0 and a < self.min_atr:
                return None
            sl = max(a * self.sl_atr, spread_px * 3)
            tp = max(sl * self.rr, sl + pad)
            return sl, tp
        sl = self._fixed_sl
        tp = max(self._fixed_tp, sl + pad) if pad > 0 else self._fixed_tp
        return sl, tp

    def _round_lot(self, raw: float) -> float:
        stepped = round(raw / self.volume_step) * self.volume_step
        return max(self.volume_min, round(stepped, 2))

    def _base_lot(self, sl_distance: float) -> float:
        if self.risk_pct is None or self.risk_manager is None or not self.equity:
            return self.base_volume
        return self.risk_manager.volume_for_risk(
            self.equity, sl_distance, risk_pct=self.risk_pct,
            contract_size=self.contract_size,
            volume_step=self.volume_step,
            volume_min=self.volume_min,
        )

    def _lot_for_step(self, sl_distance: float) -> float:
        base = self._base_lot(sl_distance)
        return self._round_lot(base * self.multiplier ** self.step)

    # ── entry filter ──────────────────────────────────────────────

    def _in_session(self, bar: Bar) -> bool:
        if self.session is None:
            return True
        lo, hi = self.session
        h = int(pd.Timestamp(bar.time).hour)
        return lo <= h < hi

    def _setup_side(self, i: int) -> Side | None:
        """Откат к EMA по тренду. Возвращает сторону или None."""
        d = self.df
        if d is None or i < self.ema_period + self.atr_period + 2:
            return None
        row = d.iloc[i]
        prev = d.iloc[i - 1]
        a = float(row["atr"])
        e = float(row["ema"])
        if not a or a != a or e != e:
            return None

        zone = a * self.pullback_atr
        close = float(row["close"])
        low = float(row["low"])
        high = float(row["high"])
        prev_close = float(prev["close"])

        # Лонг: цена над EMA (тренд), был заход в зону EMA снизу, закрытие выше EMA.
        if close > e and prev_close >= e * 0.999:
            touched = low <= e + zone
            bounced = close > e and close > float(row["open"])
            if touched and bounced:
                return Side.BUY
        # Шорт: зеркало.
        if close < e and prev_close <= e * 1.001:
            touched = high >= e - zone
            bounced = close < e and close < float(row["open"])
            if touched and bounced:
                return Side.SELL
        return None

    # ── signals ───────────────────────────────────────────────────

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if in_position or self.df is None:
            return None
        i = bar.index
        if i < self._cooldown_until:
            self.skipped_cooldown += 1
            return None
        if not self._in_session(bar):
            self.skipped_session += 1
            return None

        dists = self._distances(i, bar.spread_points)
        if dists is None:
            self.skipped_filter += 1
            return None
        sl_dist, tp_dist = dists

        # В серии — только та же сторона; новый сетап должен совпасть.
        setup = self._setup_side(i)
        if setup is None:
            self.skipped_filter += 1
            return None

        if self.series_side is not None:
            side = self.series_side
            if setup is not side:
                self.skipped_filter += 1
                return None
        else:
            side = setup

        # Анти-спам: не чаще чем раз в cooldown на новый сетап.
        if self.step == 0 and i - self._last_setup_i < self.cooldown_bars:
            self.skipped_cooldown += 1
            return None

        if side is Side.BUY:
            sl, tp = bar.close - sl_dist, bar.close + tp_dist
        else:
            sl, tp = bar.close + sl_dist, bar.close - tp_dist

        volume = self._lot_for_step(sl_dist + float(bar.spread_points) * self.point)
        self.setups_seen += 1
        self._last_setup_i = i
        return Signal(
            side=side,
            volume=volume,
            sl=sl,
            tp=tp,
            reason=f"mg EMA-pb step {self.step}",
            rr=tp_dist / sl_dist if sl_dist else self.rr,
        )

    def on_signal_rejected(self, signal, reason: str) -> None:
        if "риск" in reason or "марж" in reason or "объём" in reason:
            self.blocked_by_risk += 1
            self._reset_series(hard=True)

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        i = 0
        if self.df is not None and trade.closed_at is not None:
            hits = self.df.index[self.df.time == trade.closed_at]
            if len(hits):
                i = int(hits[0])
        self._cooldown_until = i + self.cooldown_bars

        if trade.pnl > 0:
            self._reset_series(hard=False)
            return

        self.series_debt += abs(float(trade.pnl))
        self.series_side = trade.side
        self.step += 1
        self.max_step_seen = max(self.max_step_seen, self.step)
        if self.step >= self.max_steps:
            self.resets += 1
            self._reset_series(hard=True)
            self._cooldown_until = i + self.reset_cooldown_bars

    def _reset_series(self, *, hard: bool) -> None:
        self.step = 0
        self.series_side = None
        self.series_debt = 0.0
        self.volume = self.base_volume
        if hard:
            pass
