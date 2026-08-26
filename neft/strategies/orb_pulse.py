"""OrbPulse — пробой NY opening range.

Идея: первые `orb_bars` пятиминуток после открытия кэша США (16:30 сервер
UTC+3) рисуют «коробку». Дальше торгуем только ПЕРВЫЙ чистый выход за
границу с телом свечи, пока объём дня ещё живой (до `entry_until`).

Это не London Breakout: бокс строится на открытии NY, а не на лондонской
сессии; вход рыночный по закрытию пробойной свечи; стоп — за противоположную
сторону OR + буфер ATR.
"""
from __future__ import annotations

import pandas as pd

from neft.core.indicators import atr
from neft.core.models import Side
from neft.core.strategy import Bar, Signal, Strategy


class OrbPulse(Strategy):
    name = "orb_pulse"

    def __init__(
        self,
        orb_start_hour: int = 16,
        orb_start_minute: int = 30,
        orb_bars: int = 6,          # 6×M5 = 30 минут
        entry_until: int = 20,      # до 20:00 сервера
        rr: float = 1.8,
        atr_period: int = 14,
        sl_atr_buf: float = 0.15,
        min_orb_atr: float = 0.35,  # OR слишком узкий = шум
        max_orb_atr: float = 2.2,   # OR слишком широкий = уже ушёл ход
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        spec=None,
    ):
        self.orb_start_hour = orb_start_hour
        self.orb_start_minute = orb_start_minute
        self.orb_bars = orb_bars
        self.entry_until = entry_until
        self.rr = rr
        self.atr_period = atr_period
        self.sl_atr_buf = sl_atr_buf
        self.min_orb_atr = min_orb_atr
        self.max_orb_atr = max_orb_atr
        self.risk_pct, self.risk_manager = risk_pct, risk_manager
        self.base_volume, self.spec = base_volume, spec
        self.equity = 0.0
        self.df: pd.DataFrame | None = None
        self._day_state: dict = {}
        self.setups_seen = 0
        self.skipped = 0

    def set_equity(self, equity: float) -> None:
        self.equity = equity

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy().reset_index(drop=True)
        d["atr"] = atr(d, self.atr_period)
        d["day"] = d.time.dt.date
        d["hour"] = d.time.dt.hour
        d["minute"] = d.time.dt.minute
        self.df = d
        self._day_state = {}
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

    def _state(self, day):
        st = self._day_state.get(day)
        if st is None:
            st = {"hi": None, "lo": None, "built": False, "traded": False, "n": 0}
            self._day_state[day] = st
        return st

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if self.df is None or in_position:
            return None
        i = bar.index
        row = self.df.iloc[i]
        day = row["day"]
        st = self._state(day)
        if st["traded"]:
            return None

        h, m = int(row["hour"]), int(row["minute"])
        after_open = (h > self.orb_start_hour) or (
            h == self.orb_start_hour and m >= self.orb_start_minute
        )
        if not after_open:
            return None

        # Набор OR.
        if not st["built"]:
            hi = float(row["high"]) if st["hi"] is None else max(st["hi"], float(row["high"]))
            lo = float(row["low"]) if st["lo"] is None else min(st["lo"], float(row["low"]))
            st["hi"], st["lo"] = hi, lo
            st["n"] += 1
            if st["n"] >= self.orb_bars:
                st["built"] = True
            return None

        if h >= self.entry_until:
            return None

        a = float(row["atr"])
        if not a or a != a or a <= 0:
            self.skipped += 1
            return None
        width = st["hi"] - st["lo"]
        if width < a * self.min_orb_atr or width > a * self.max_orb_atr:
            self.skipped += 1
            return None

        close = float(row["close"])
        open_ = float(row["open"])
        buf = a * self.sl_atr_buf
        side = None
        sl = tp = None
        # Чистый пробой: закрытие за границей + тело в сторону пробоя.
        if close > st["hi"] and close > open_:
            side = Side.BUY
            sl = st["lo"] - buf
            risk = close - sl
            if risk <= 0:
                return None
            tp = close + risk * self.rr
        elif close < st["lo"] and close < open_:
            side = Side.SELL
            sl = st["hi"] + buf
            risk = sl - close
            if risk <= 0:
                return None
            tp = close - risk * self.rr
        else:
            return None

        vol = self._volume(abs(close - sl))
        st["traded"] = True
        self.setups_seen += 1
        return Signal(
            side=side, volume=vol, sl=sl, tp=tp,
            reason="orb-pulse break", rr=self.rr,
        )
