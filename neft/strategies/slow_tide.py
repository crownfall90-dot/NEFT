"""SlowTide — медленный тренд + откат к «приливу».

Идея: быстрая EMA(21) и медленная EMA(55). Торгуем только когда канал
достаточно широкий (разделение > atr_sep·ATR) — есть направленный поток.
Вход: откат цены к медленной EMA (касание зоны) + закрытие обратно по
тренду быстрой EMA. SL за зону на ATR, TP = rr·риск.

Не скальп по каждой свече и не мартингейл: одна логика «плыть с приливом
после короткого отката».
"""
from __future__ import annotations

import pandas as pd

from neft.core.indicators import atr, ema
from neft.core.models import Side
from neft.core.strategy import Bar, Signal, Strategy


class SlowTide(Strategy):
    name = "slow_tide"

    def __init__(
        self,
        fast: int = 21,
        slow: int = 55,
        atr_period: int = 14,
        atr_sep: float = 0.45,     # |ema_fast-ema_slow| / ATR
        pullback_atr: float = 0.28,
        sl_atr: float = 0.95,
        rr: float = 2.0,
        session: tuple[int, int] | None = (15, 20),
        cooldown_bars: int = 16,
        max_per_day: int = 2,
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        spec=None,
    ):
        self.fast, self.slow = fast, slow
        self.atr_period = atr_period
        self.atr_sep = atr_sep
        self.pullback_atr = pullback_atr
        self.sl_atr = sl_atr
        self.rr = rr
        self.session = session
        self.cooldown_bars = cooldown_bars
        self.risk_pct, self.risk_manager = risk_pct, risk_manager
        self.base_volume, self.spec = base_volume, spec
        self.equity = 0.0
        self.max_per_day = max_per_day
        self.df: pd.DataFrame | None = None
        self._last_i = -10**9
        self._day_count: dict = {}
        self.setups_seen = 0
        self.skipped = 0

    def set_equity(self, equity: float) -> None:
        self.equity = equity

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy().reset_index(drop=True)
        d["ema_fast"] = ema(d.close, self.fast)
        d["ema_slow"] = ema(d.close, self.slow)
        d["atr"] = atr(d, self.atr_period)
        d["hour"] = d.time.dt.hour
        d["day"] = d.time.dt.date
        self.df = d
        self._day_count = {}
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

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if self.df is None or in_position:
            return None
        i = bar.index
        if i < self.slow + self.atr_period + 2:
            return None
        if i - self._last_i < self.cooldown_bars:
            return None
        row = self.df.iloc[i]
        prev = self.df.iloc[i - 1]
        day = row["day"]
        if self._day_count.get(day, 0) >= self.max_per_day:
            return None
        if self.session is not None:
            lo, hi = self.session
            if not (lo <= int(row["hour"]) < hi):
                return None

        a = float(row["atr"])
        ef, es = float(row["ema_fast"]), float(row["ema_slow"])
        ef_prev = float(prev["ema_fast"])
        if not a or a != a or ef != ef or es != es or a <= 0:
            self.skipped += 1
            return None
        if abs(ef - es) < a * self.atr_sep:
            self.skipped += 1
            return None

        close = float(row["close"])
        open_ = float(row["open"])
        low, high = float(row["low"]), float(row["high"])
        zone = a * self.pullback_atr
        side = None
        sl = tp = None

        # Бычий прилив: fast растёт и выше slow, касание slow, close > fast.
        if (ef > es and ef > ef_prev and low <= es + zone
                and close > es and close > ef and close > open_):
            side = Side.BUY
            sl = min(low, es) - a * self.sl_atr
            risk = close - sl
            if risk <= 0:
                return None
            tp = close + risk * self.rr
        elif (ef < es and ef < ef_prev and high >= es - zone
              and close < es and close < ef and close < open_):
            side = Side.SELL
            sl = max(high, es) + a * self.sl_atr
            risk = sl - close
            if risk <= 0:
                return None
            tp = close - risk * self.rr
        else:
            return None

        vol = self._volume(risk)
        self.setups_seen += 1
        self._last_i = i
        self._day_count[day] = self._day_count.get(day, 0) + 1
        return Signal(
            side=side, volume=vol, sl=sl, tp=tp,
            reason="slow-tide pb", rr=self.rr,
        )
