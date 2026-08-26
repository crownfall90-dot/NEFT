"""VwapSnap — возврат к сессионному VWAP после перерастяжения.

Идея: внутри торгового дня цена редко «висит» далеко от VWAP. Когда
отклонение > k·ATR и свеча разворачивается обратно к VWAP (закрытие против
экстремума), входим к магниту. TP — сам VWAP (или часть пути), SL — за
экстремум + ATR-буфер.

Не mean-reversion ради MR: берём только ликвидное окно и требуем, чтобы
цель давала min_rr к стопу.
"""
from __future__ import annotations

import pandas as pd

from neft.core.indicators import atr, grouped_vwap
from neft.core.models import Side
from neft.core.strategy import Bar, Signal, Strategy


class VwapSnap(Strategy):
    name = "vwap_snap"

    def __init__(
        self,
        session: tuple[int, int] = (16, 20),
        stretch_atr: float = 1.85,
        sl_atr: float = 0.45,
        min_rr: float = 1.5,
        atr_period: int = 14,
        cooldown_bars: int = 14,
        max_per_day: int = 1,
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        spec=None,
    ):
        self.session = session
        self.stretch_atr = stretch_atr
        self.sl_atr = sl_atr
        self.min_rr = min_rr
        self.atr_period = atr_period
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
        d["day"] = d.time.dt.date
        d["hour"] = d.time.dt.hour
        d["atr"] = atr(d, self.atr_period)
        d["vwap"] = grouped_vwap(d, d["day"])
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
        if i < 2 or i - self._last_i < self.cooldown_bars:
            return None
        row = self.df.iloc[i]
        prev = self.df.iloc[i - 1]
        day = row["day"]
        if self._day_count.get(day, 0) >= self.max_per_day:
            return None
        lo, hi = self.session
        if not (lo <= int(row["hour"]) < hi):
            return None
        a = float(row["atr"])
        v = float(row["vwap"])
        if not a or a != a or not v or v != v or a <= 0:
            self.skipped += 1
            return None

        close = float(row["close"])
        open_ = float(row["open"])
        dist = close - v
        prev_dist = float(prev["close"]) - float(prev["vwap"])
        side = None
        sl = tp = None

        # Пик перерастяжения был на предыдущем баре, сейчас разворот к VWAP.
        if (dist >= a * self.stretch_atr and prev_dist >= dist
                and close < open_ and close < float(prev["close"])):
            side = Side.SELL
            sl = max(float(row["high"]), float(prev["high"])) + a * self.sl_atr
            tp = v
            risk = sl - close
            reward = close - tp
        elif (dist <= -a * self.stretch_atr and prev_dist <= dist
              and close > open_ and close > float(prev["close"])):
            side = Side.BUY
            sl = min(float(row["low"]), float(prev["low"])) - a * self.sl_atr
            tp = v
            risk = close - sl
            reward = tp - close
        else:
            return None

        if risk <= 0 or reward / risk < self.min_rr:
            self.skipped += 1
            return None

        vol = self._volume(risk)
        self.setups_seen += 1
        self._last_i = i
        self._day_count[day] = self._day_count.get(day, 0) + 1
        return Signal(
            side=side, volume=vol, sl=sl, tp=tp,
            reason="vwap-snap", rr=reward / risk,
        )
