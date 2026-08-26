"""PulseClip — частые сделки по тренду, низкая просадка.

Профиль: много клипов, риск маленький, DD минимальный.

Не fade против импульса (комиссии съедают), а продолжение:
цена выше/ниже EMA34, откат 1–2 бара к EMA, закрытие снова по тренду.
TP ≈ 0.9·ATR, SL ≈ 0.55·ATR (+подушка комиссии). Дневной лимит лузов.
"""
from __future__ import annotations

import pandas as pd

from neft.core.indicators import atr, ema
from neft.core.models import Side
from neft.core.strategy import Bar, ClosedTrade, Signal, Strategy


class PulseClip(Strategy):
    name = "pulse_clip"

    def __init__(
        self,
        session: tuple[int, int] = (16, 20),
        ema_period: int = 34,
        atr_period: int = 14,
        sl_atr: float = 0.55,
        tp_atr: float = 0.95,
        touch_atr: float = 0.35,
        cooldown_bars: int = 3,
        max_per_day: int = 6,
        max_day_losses: int = 2,
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        spec=None,
        commission_per_lot: float = 0.0,
    ):
        self.session = session
        self.ema_period = ema_period
        self.atr_period = atr_period
        self.sl_atr = sl_atr
        self.tp_atr = tp_atr
        self.touch_atr = touch_atr
        self.cooldown_bars = cooldown_bars
        self.max_per_day = max_per_day
        self.max_day_losses = max_day_losses
        self.risk_pct, self.risk_manager = risk_pct, risk_manager
        self.base_volume, self.spec = base_volume, spec
        self.commission_per_lot = float(commission_per_lot)
        self.equity = 0.0
        self.df: pd.DataFrame | None = None
        self._last_i = -10**9
        self._day_n: dict = {}
        self._day_losses: dict = {}
        self.setups_seen = 0
        self.skipped = 0

    def set_equity(self, equity: float) -> None:
        self.equity = equity

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy().reset_index(drop=True)
        d["ema"] = ema(d.close, self.ema_period)
        d["atr"] = atr(d, self.atr_period)
        d["hour"] = d.time.dt.hour
        d["day"] = d.time.dt.date
        self.df = d
        self._day_n, self._day_losses = {}, {}
        return d

    def _volume(self, sl_distance: float) -> float:
        if self.risk_pct is None or self.risk_manager is None or not self.equity:
            return self.base_volume
        sp = self.spec
        return self.risk_manager.volume_for_risk(
            self.equity, sl_distance, risk_pct=self.risk_pct,
            contract_size=sp.contract_size if sp else 100_000.0,
            volume_step=sp.volume_step if sp else 0.01,
            volume_min=sp.volume_min if sp else 0.01,
        )

    def _fee_pad(self) -> float:
        cs = float(getattr(self.spec, "contract_size", 100_000.0) or 100_000.0)
        return (2.0 * self.commission_per_lot / cs) if cs > 0 else 0.0

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if self.df is None or in_position:
            return None
        i = bar.index
        if i < self.ema_period + self.atr_period + 2:
            return None
        if i - self._last_i < self.cooldown_bars:
            return None
        row = self.df.iloc[i]
        prev = self.df.iloc[i - 1]
        day = row["day"]
        if self._day_n.get(day, 0) >= self.max_per_day:
            return None
        if self._day_losses.get(day, 0) >= self.max_day_losses:
            return None
        lo, hi = self.session
        if not (lo <= int(row["hour"]) < hi):
            return None

        a = float(row["atr"])
        e = float(row["ema"])
        if not a or a != a or e != e or a <= 0:
            return None

        close = float(row["close"])
        open_ = float(row["open"])
        low, high = float(row["low"]), float(row["high"])
        prev_close = float(prev["close"])
        zone = a * self.touch_atr
        pad = self._fee_pad()
        side = None
        sl = tp = None

        # Лонг по тренду: close и prev над EMA, касание зоны, бычье закрытие.
        if (close > e and prev_close > e and low <= e + zone
                and close > open_ and close >= prev_close):
            side = Side.BUY
            sl = min(low, e) - a * self.sl_atr
            risk = close - sl
            if risk <= 0:
                return None
            tp = close + max(a * self.tp_atr, risk + pad)
        elif (close < e and prev_close < e and high >= e - zone
              and close < open_ and close <= prev_close):
            side = Side.SELL
            sl = max(high, e) + a * self.sl_atr
            risk = sl - close
            if risk <= 0:
                return None
            tp = close - max(a * self.tp_atr, risk + pad)
        else:
            return None

        vol = self._volume(abs(close - sl))
        self.setups_seen += 1
        self._last_i = i
        self._day_n[day] = self._day_n.get(day, 0) + 1
        return Signal(side=side, volume=vol, sl=sl, tp=tp, reason="pulse-clip")

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        if trade.pnl > 0 or self.df is None or trade.closed_at is None:
            return
        hits = self.df.index[self.df.time == trade.closed_at]
        if not len(hits):
            return
        day = self.df.iloc[int(hits[0])]["day"]
        self._day_losses[day] = self._day_losses.get(day, 0) + 1
