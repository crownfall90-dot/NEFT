"""SonikPulse v6 — deep pullback + EMA slope + near fractal pivot.

Дефолт: prior<=-0.6, slope>=0.15, near opposite pivot (<=1.5 ATR), London 07–09.
Volume/trail опциональны. Amplify WR~87% с M1 OHLC не воспроизводится.
"""
from __future__ import annotations

import pandas as pd

from neft.core.indicators import atr, ema
from neft.core.models import Side
from neft.core.strategy import Bar, Signal, Strategy


class SonikPulse(Strategy):
    name = "sonik_pulse"

    def __init__(
        self,
        session_from: tuple[int, int] = (7, 0),
        session_until: tuple[int, int] = (9, 0),
        session2_from: tuple[int, int] | None = None,
        session2_until: tuple[int, int] | None = None,
        atr_period: int = 14,
        sl_points: float = 2.5,
        tp_points: float = 4.0,
        min_atr: float = 0.15,
        max_atr: float = 8.0,
        min_body_atr: float = 0.5,
        require_break: bool = True,
        pullback_bars: int = 5,
        max_prior_along: float = -0.6,
        min_ema_slope: float = 0.15,
        ema_fast: int = 21,
        ema_slow: int = 55,
        min_vol_r: float = 0.0,
        require_near_pivot: bool = True,
        max_pivot_atr: float = 1.5,
        # trailing: 0 = выкл
        trail_arm: float = 0.0,
        trail_dist: float = 1.5,
        be_lock: float = 0.2,
        max_trades_day: int = 1,
        cooldown_bars: int = 15,
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        spec=None,
        commission_per_lot: float = 0.0,
        require_pullback: bool = True,
        min_compress: float | None = None,
        use_htf_ema: bool = True,
        htf_ema: int = 55,
    ):
        self.session_from = session_from
        self.session_until = session_until
        self.session2_from = session2_from
        self.session2_until = session2_until
        self.atr_period = atr_period
        self.sl_points = sl_points
        self.tp_points = tp_points
        self.min_atr = min_atr
        self.max_atr = max_atr
        self.min_body_atr = min_body_atr
        self.require_break = require_break
        self.pullback_bars = pullback_bars
        self.max_prior_along = max_prior_along
        self.min_ema_slope = min_ema_slope
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.min_vol_r = min_vol_r
        self.require_near_pivot = require_near_pivot
        self.max_pivot_atr = max_pivot_atr
        self.trail_arm = trail_arm
        self.trail_dist = trail_dist
        self.be_lock = be_lock
        self.max_trades_day = max_trades_day
        self.cooldown_bars = cooldown_bars
        self.risk_pct, self.risk_manager = risk_pct, risk_manager
        self.base_volume, self.spec = base_volume, spec
        self.commission_per_lot = float(commission_per_lot)
        self.equity = 0.0
        self.df: pd.DataFrame | None = None
        self._day: dict = {}
        self._last_entry_i = -10_000
        self._trail_best: float | None = None
        self._trail_armed = False
        self.setups_seen = 0
        self.skipped = 0

    def set_equity(self, equity: float) -> None:
        self.equity = equity

    @staticmethod
    def _hm(hour: int, minute: int) -> int:
        return hour * 60 + minute

    def _in_session(self, mins: int) -> bool:
        if self._hm(*self.session_from) <= mins < self._hm(*self.session_until):
            return True
        if self.session2_from and self.session2_until:
            if self._hm(*self.session2_from) <= mins < self._hm(*self.session2_until):
                return True
        return False

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy().reset_index(drop=True)
        d["atr"] = atr(d, self.atr_period)
        d["ema_f"] = ema(d.close, self.ema_fast)
        d["ema_s"] = ema(d.close, self.ema_slow)
        d["day"] = d.time.dt.date
        d["mins"] = d.time.dt.hour * 60 + d.time.dt.minute
        d["prev_high"] = d.high.shift(1)
        d["prev_low"] = d.low.shift(1)
        # fractal pivot confirmed on bar i for price at i-2 (no lookahead)
        d["piv_hi"] = (
            (d.high.shift(2) > d.high.shift(3)) & (d.high.shift(2) > d.high.shift(4))
            & (d.high.shift(2) > d.high.shift(1)) & (d.high.shift(2) > d.high)
        )
        d["piv_lo"] = (
            (d.low.shift(2) < d.low.shift(3)) & (d.low.shift(2) < d.low.shift(4))
            & (d.low.shift(2) < d.low.shift(1)) & (d.low.shift(2) < d.low)
        )
        d["piv_hi_px"] = d.high.shift(2).where(d.piv_hi)
        d["piv_lo_px"] = d.low.shift(2).where(d.piv_lo)
        d["last_piv_hi"] = d["piv_hi_px"].ffill()
        d["last_piv_lo"] = d["piv_lo_px"].ffill()
        vol_ma = d.tick_volume.rolling(20).mean() if "tick_volume" in d.columns else None
        if vol_ma is not None:
            d["vol_r"] = d.tick_volume / vol_ma.replace(0, pd.NA)
        else:
            d["vol_r"] = 1.0
        self.df = d
        self._day = {}
        self._last_entry_i = -10_000
        self._trail_best = None
        self._trail_armed = False
        return d

    def _st(self, day):
        st = self._day.get(day)
        if st is None:
            st = {"n": 0}
            self._day[day] = st
        return st

    def _volume(self, sl_distance: float) -> float:
        if self.risk_pct is None or self.risk_manager is None or not self.equity:
            return self.base_volume
        sp = self.spec
        return self.risk_manager.volume_for_risk(
            self.equity, sl_distance, risk_pct=self.risk_pct,
            contract_size=sp.contract_size if sp else 100.0,
            volume_step=sp.volume_step if sp else 0.01,
            volume_min=sp.volume_min if sp else 0.01,
        )

    def on_trade_closed(self, trade) -> None:
        self._trail_best = None
        self._trail_armed = False

    def manage_position(self, bar, side, entry, sl, tp):
        if self.trail_arm <= 0:
            return None
        buy = side is Side.BUY
        if self._trail_best is None:
            self._trail_best = entry
            self._trail_armed = False
        if buy:
            self._trail_best = max(self._trail_best, bar.high)
            if not self._trail_armed and self._trail_best >= entry + self.trail_arm:
                self._trail_armed = True
                sl = max(sl, entry + self.be_lock)
            if self._trail_armed:
                sl = max(sl, self._trail_best - self.trail_dist)
        else:
            self._trail_best = min(self._trail_best, bar.low)
            if not self._trail_armed and self._trail_best <= entry - self.trail_arm:
                self._trail_armed = True
                sl = min(sl, entry - self.be_lock)
            if self._trail_armed:
                sl = min(sl, self._trail_best + self.trail_dist)
        return sl, tp

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if self.df is None or in_position:
            return None
        i = bar.index
        need = max(self.atr_period, self.ema_slow, self.pullback_bars, 6) + 2
        if i < need or i - self._last_entry_i < self.cooldown_bars:
            return None

        row = self.df.iloc[i]
        st = self._st(row["day"])
        if st["n"] >= self.max_trades_day:
            return None
        if not self._in_session(int(row["mins"])):
            return None

        a = float(row["atr"])
        if not a or a != a or a < self.min_atr or a > self.max_atr:
            return None

        close = float(row["close"])
        open_ = float(row["open"])
        if abs(close - open_) < a * self.min_body_atr:
            return None

        vr = float(row["vol_r"]) if row["vol_r"] == row["vol_r"] else 1.0
        if vr < self.min_vol_r:
            return None

        ph, pl = float(row["prev_high"]), float(row["prev_low"])
        if close > open_ and (not self.require_break or (ph == ph and close > ph)):
            side, signed = Side.BUY, 1
        elif close < open_ and (not self.require_break or (pl == pl and close < pl)):
            side, signed = Side.SELL, -1
        else:
            return None

        prev = self.df.iloc[i - self.pullback_bars:i]
        prior_along = (
            (float(prev.close.iloc[-1]) - float(prev.close.iloc[0])) / a
        ) * signed
        if prior_along > self.max_prior_along:
            return None

        e_now = float(row["ema_f"])
        e_prev = float(self.df.iloc[i - 5]["ema_f"])
        slope = ((e_now - e_prev) / a) * signed
        if slope < self.min_ema_slope:
            return None

        if self.require_near_pivot:
            if side is Side.BUY:
                lvl = row["last_piv_lo"]
            else:
                lvl = row["last_piv_hi"]
            if lvl != lvl:
                return None
            if abs(close - float(lvl)) > a * self.max_pivot_atr:
                return None

        sl = close - signed * self.sl_points
        tp = close + signed * self.tp_points
        st["n"] += 1
        self._last_entry_i = i
        self._trail_best = None
        self._trail_armed = False
        self.setups_seen += 1
        return Signal(
            side=side, volume=self._volume(self.sl_points),
            sl=sl, tp=tp, reason="sonik-pulse-v6",
            rr=self.tp_points / self.sl_points,
        )


class SonikOpen(SonikPulse):
    name = "sonik_open"
