"""Крипто-скальп 5m/15m: набор узких сетапов под разные режимы рынка.

Каждый сетап можно включить отдельно. Режим `gate_regime=True` сам отключает
то, что сейчас не к месту (тренд vs флет). Это не «индикаторный суп» и не
YouTube с винрейтом: только проверяемые на OHLCV правила.

Сессии в UTC (свечи Binance). Сессионные сетапы — только Нью-Йорк.
Трендовый откат и фейд диапазона работают круглосуточно.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from neft.core.indicators import atr, ema, grouped_vwap
from neft.core.models import Side
from neft.core.strategy import Bar, ClosedTrade, Signal, Strategy

ALL_SETUPS = (
    "asian_sweep", "failed_orb", "range_fade",
    "vwap_reclaim", "ema_pull", "orb_follow",
)
# Ядро после прогона 5m BTC: остальные сетапы остаются, но не в blend.
CORE = ("failed_orb", "orb_follow", "vwap_reclaim")
SETUPS = ALL_SETUPS
NY_ONLY = frozenset({"asian_sweep", "failed_orb", "vwap_reclaim", "orb_follow"})
REGIME_SETUPS = {
    "trend": ("orb_follow", "vwap_reclaim"),
    "range": ("failed_orb",),
    "mixed": ("vwap_reclaim", "failed_orb"),
}
BUDGET = {
    "asian_sweep": 1, "failed_orb": 1, "orb_follow": 1,
    "vwap_reclaim": 3, "ema_pull": 4, "range_fade": 3,
}


class SessionFlow(Strategy):
    name = "session_flow"

    def __init__(
        self,
        setups: tuple[str, ...] | list[str] | str = "blend",
        gate_regime: bool = True,
        rr: float = 1.0,
        ny: tuple[int, int] = (13, 21),
        asian: tuple[int, int] = (0, 8),
        orb_hour: int = 13,
        orb_minute: int = 30,
        orb_minutes: int = 15,
        vol_mult: float = 1.1,
        min_atr_pct: float = 0.0004,
        min_sl_atr: float = 0.4,
        max_sl_atr: float = 2.5,
        expire_bars: int = 8,
        max_per_setup_day: dict | int | None = None,
        session: tuple[int, int] | None = None,
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        spec=None,
    ):
        if isinstance(setups, str):
            if setups == "blend":
                setups = CORE
            elif setups in ("all", "*"):
                setups = ALL_SETUPS
            else:
                setups = (setups,)
        self.setups = tuple(s for s in setups if s in ALL_SETUPS) or CORE
        self.gate_regime = gate_regime
        self.rr = rr
        self.ny = session or ny
        self.asian = asian
        self.orb_hour, self.orb_minute = orb_hour, orb_minute
        self.orb_minutes = orb_minutes
        self.vol_mult = vol_mult
        self.min_atr_pct = min_atr_pct
        self.min_sl_atr, self.max_sl_atr = min_sl_atr, max_sl_atr
        self.expire_bars = expire_bars
        self.max_per_setup_day = max_per_setup_day or dict(BUDGET)
        self.risk_pct, self.risk_manager = risk_pct, risk_manager
        self.base_volume, self.spec = base_volume, spec
        self.equity = 0.0
        self.df: pd.DataFrame | None = None
        self._counts: dict[tuple, int] = {}
        self._orb_broke: dict = {}
        self._last_setup = ""
        self.setups_seen = 0
        self.by_setup: dict[str, int] = {s: 0 for s in SETUPS}
        self.skipped_sl = 0
        self.skipped_session = 0
        self.skipped_regime = 0
        # Жёсткий фильтр часов (как у London S/R), отдельно от ny-окон сетапов.
        # session=None → 24/7; session=(16,19) → только это окно.
        self.session_gate = session

    def set_equity(self, equity: float) -> None:
        self.equity = equity

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy().reset_index(drop=True)
        t = pd.to_datetime(d.time)
        d["date"] = t.dt.date
        d["hour"] = t.dt.hour
        d["minute"] = t.dt.minute
        d["atr"] = atr(d, 14)
        d["atr_slow"] = d.atr.rolling(100, min_periods=30).mean()
        d["ema20"] = ema(d.close, 20)
        d["ema200"] = ema(d.close, 200)
        d["vol_sma"] = d.tick_volume.rolling(20, min_periods=8).mean()
        d["vwap"] = grouped_vwap(d, d["date"])
        ny0, ny1 = self.ny
        in_ny = (d.hour >= ny0) & (d.hour < ny1)
        d["ny_vwap"] = grouped_vwap(d, d["date"], in_ny)
        d["ny_vwap"] = d.groupby("date")["ny_vwap"].ffill()

        bar_min = 5.0
        if len(t) > 2:
            bar_min = max(1.0, float(t.diff().dt.total_seconds().median() or 300) / 60)
        don = max(12, int(round(240 / bar_min)))
        d["dc_high"] = d.high.rolling(don, min_periods=don).max().shift(1)
        d["dc_low"] = d.low.rolling(don, min_periods=don).min().shift(1)

        sep = (d.ema20 - d.ema200).abs() / d.atr.replace(0, np.nan)
        trend = (sep > 1.0) & (d.atr > d.atr_slow * 0.85)
        d["regime"] = "mixed"
        d.loc[d.atr < d.atr_slow * 0.75, "regime"] = "range"
        d.loc[trend.fillna(False), "regime"] = "trend"

        a0, a1 = self.asian
        in_asia = (d.hour >= a0) & (d.hour < a1)
        asia = (d.loc[in_asia]
                .groupby("date")
                .agg(asia_high=("high", "max"), asia_low=("low", "min"),
                     asia_end=("time", "max")))
        d = d.merge(asia, left_on="date", right_index=True, how="left")
        d["asia_ready"] = d.time > d.asia_end

        om = self.orb_minute
        in_orb = ((d.hour == self.orb_hour)
                  & (d.minute >= om)
                  & (d.minute < om + self.orb_minutes))
        orb = (d.loc[in_orb]
               .groupby("date")
               .agg(orb_high=("high", "max"), orb_low=("low", "min"),
                    orb_end=("time", "max")))
        d = d.merge(orb, left_on="date", right_index=True, how="left")
        d["orb_ready"] = d.time > d.orb_end
        self.df = d
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

    def _in_ny(self, bar: Bar) -> bool:
        lo, hi = self.ny
        return lo <= bar.time.hour < hi

    def _limit(self, setup: str) -> int:
        b = self.max_per_setup_day
        if isinstance(b, dict):
            return int(b.get(setup, 1))
        return int(b)

    def _budget(self, day, setup: str) -> bool:
        return self._counts.get((day, setup), 0) < self._limit(setup)

    def _take(self, day, setup: str) -> None:
        self._counts[(day, setup)] = self._counts.get((day, setup), 0) + 1
        self.by_setup[setup] = self.by_setup.get(setup, 0) + 1
        self.setups_seen += 1
        self._last_setup = setup

    def _active(self, regime: str) -> tuple[str, ...]:
        if not self.gate_regime:
            return self.setups
        allowed = REGIME_SETUPS.get(str(regime), REGIME_SETUPS["mixed"])
        return tuple(s for s in self.setups if s in allowed)

    def _signal(self, side: Side, entry: float, sl: float, atr_now: float,
                setup: str) -> Signal | None:
        dist = abs(entry - sl)
        if dist <= 0 or atr_now <= 0:
            return None
        if not (self.min_sl_atr * atr_now <= dist <= self.max_sl_atr * atr_now):
            self.skipped_sl += 1
            return None
        tp = entry + dist * self.rr if side is Side.BUY else entry - dist * self.rr
        return Signal(
            side=side, volume=self._volume(dist), sl=float(sl), tp=float(tp),
            entry=None, entry_type="market", expire_bars=self.expire_bars,
            reason=setup, rr=self.rr,
        )

    def _vol_ok(self, row, mult: float | None = None) -> bool:
        m = self.vol_mult if mult is None else mult
        return (not pd.isna(row.vol_sma)
                and float(row.tick_volume) >= float(row.vol_sma) * m)

    def _asian_sweep(self, bar: Bar, row) -> Signal | None:
        if not bool(row.asia_ready) or pd.isna(row.asia_high):
            return None
        if bar.high > row.asia_high and bar.close < row.asia_high:
            return self._signal(Side.SELL, bar.close, float(bar.high),
                                float(row.atr), "asian_sweep")
        if bar.low < row.asia_low and bar.close > row.asia_low:
            return self._signal(Side.BUY, bar.close, float(bar.low),
                                float(row.atr), "asian_sweep")
        return None

    def _failed_orb(self, bar: Bar, row) -> Signal | None:
        if not bool(row.orb_ready) or pd.isna(row.orb_high):
            return None
        day = row.date
        broke = self._orb_broke.get(day)
        if broke in ("faded", "followed"):
            return None
        if broke is None:
            if bar.close > row.orb_high:
                self._orb_broke[day] = "up"
            elif bar.close < row.orb_low:
                self._orb_broke[day] = "down"
            return None
        if broke == "up" and bar.close < row.orb_high:
            self._orb_broke[day] = "faded"
            sl = max(float(bar.high), float(row.orb_high))
            return self._signal(Side.SELL, bar.close, sl, float(row.atr), "failed_orb")
        if broke == "down" and bar.close > row.orb_low:
            self._orb_broke[day] = "faded"
            sl = min(float(bar.low), float(row.orb_low))
            return self._signal(Side.BUY, bar.close, sl, float(row.atr), "failed_orb")
        return None

    def _orb_follow(self, bar: Bar, row) -> Signal | None:
        if not bool(row.orb_ready) or pd.isna(row.orb_high):
            return None
        day = row.date
        if self._orb_broke.get(day) is not None:
            return None
        if not self._vol_ok(row):
            return None
        if bar.close > row.orb_high:
            self._orb_broke[day] = "followed"
            sl = float(min(bar.low, row.orb_high))
            return self._signal(Side.BUY, bar.close, sl, float(row.atr), "orb_follow")
        if bar.close < row.orb_low:
            self._orb_broke[day] = "followed"
            sl = float(max(bar.high, row.orb_low))
            return self._signal(Side.SELL, bar.close, sl, float(row.atr), "orb_follow")
        return None

    def _vwap_reclaim(self, i: int, bar: Bar, row, d: pd.DataFrame) -> Signal | None:
        vwap = row.ny_vwap
        if pd.isna(vwap) or i < 3 or not self._vol_ok(row):
            return None
        prev = d.iloc[i - 1]
        window = d.iloc[i - 2:i + 1]
        if prev.close < vwap and bar.close > vwap and window.low.min() <= vwap:
            return self._signal(Side.BUY, bar.close, float(window.low.min()),
                                float(row.atr), "vwap_reclaim")
        if prev.close > vwap and bar.close < vwap and window.high.max() >= vwap:
            return self._signal(Side.SELL, bar.close, float(window.high.max()),
                                float(row.atr), "vwap_reclaim")
        return None

    def _ema_pull(self, i: int, bar: Bar, row, d: pd.DataFrame) -> Signal | None:
        if i < 2 or pd.isna(row.ema20) or pd.isna(row.ema200) or pd.isna(row.vwap):
            return None
        prev = d.iloc[i - 1]
        if bar.close > row.ema200 and bar.close > row.vwap:
            if prev.low <= row.ema20 and bar.close > row.ema20:
                sl = float(min(bar.low, prev.low))
                return self._signal(Side.BUY, bar.close, sl, float(row.atr), "ema_pull")
        if bar.close < row.ema200 and bar.close < row.vwap:
            if prev.high >= row.ema20 and bar.close < row.ema20:
                sl = float(max(bar.high, prev.high))
                return self._signal(Side.SELL, bar.close, sl, float(row.atr), "ema_pull")
        return None

    def _range_fade(self, bar: Bar, row) -> Signal | None:
        if pd.isna(row.dc_high) or pd.isna(row.dc_low):
            return None
        if bar.high > row.dc_high and bar.close < row.dc_high:
            return self._signal(Side.SELL, bar.close, float(bar.high),
                                float(row.atr), "range_fade")
        if bar.low < row.dc_low and bar.close > row.dc_low:
            return self._signal(Side.BUY, bar.close, float(bar.low),
                                float(row.atr), "range_fade")
        return None

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if in_position or self.df is None:
            return None
        if self.session_gate is not None:
            lo, hi = self.session_gate
            h = int(pd.Timestamp(bar.time).hour)
            if not (lo <= h < hi):
                self.skipped_session += 1
                return None
        i = bar.index
        if i < 40:
            return None
        row = self.df.iloc[i]
        atr_now = float(row.atr) if pd.notna(row.atr) else 0.0
        if atr_now <= 0 or atr_now / bar.close < self.min_atr_pct:
            return None
        day = row.date
        detectors = {
            "asian_sweep": lambda: self._asian_sweep(bar, row),
            "failed_orb": lambda: self._failed_orb(bar, row),
            "orb_follow": lambda: self._orb_follow(bar, row),
            "vwap_reclaim": lambda: self._vwap_reclaim(i, bar, row, self.df),
            "ema_pull": lambda: self._ema_pull(i, bar, row, self.df),
            "range_fade": lambda: self._range_fade(bar, row),
        }
        active = self._active(row.regime)
        if self.gate_regime and not active:
            self.skipped_regime += 1
            return None
        for setup in active:
            if setup in NY_ONLY and not self._in_ny(bar):
                self.skipped_session += 1
                continue
            if not self._budget(day, setup):
                continue
            sig = detectors[setup]()
            if sig is None:
                continue
            self._take(day, setup)
            return sig
        return None

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        if self._last_setup:
            trade.strategy = self._last_setup
