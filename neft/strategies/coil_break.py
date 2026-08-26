"""CoilBreak — азиатская «спираль» и выход в Лондоне.

Идея: ночью (02–08 сервер UTC+3) индекс/металл сжимается в узкий диапазон.
В лондонское окно (11–15) берём первый выход тела за границу Asia-range,
если ширина койла была в ATR-коридоре (не мёртвый рынок и не уже пробой).

Отличие от OrbPulse (NY OR) и от London Breakout (бокс 10–16 + вход 16:30):
здесь койл именно азиатский, вход днём в Лондоне, рыночный по закрытию.
"""
from __future__ import annotations

import pandas as pd

from neft.core.indicators import atr
from neft.core.models import Side
from neft.core.strategy import Bar, Signal, Strategy


class CoilBreak(Strategy):
    name = "coil_break"

    def __init__(
        self,
        coil=(2, 8),
        entry=(11, 15),
        rr: float = 1.7,
        atr_period: int = 14,
        min_coil_atr: float = 0.25,
        max_coil_atr: float = 1.6,
        sl_atr_buf: float = 0.12,
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        spec=None,
    ):
        self.coil, self.entry = coil, entry
        self.rr = rr
        self.atr_period = atr_period
        self.min_coil_atr = min_coil_atr
        self.max_coil_atr = max_coil_atr
        self.sl_atr_buf = sl_atr_buf
        self.risk_pct, self.risk_manager = risk_pct, risk_manager
        self.base_volume, self.spec = base_volume, spec
        self.equity = 0.0
        self.df: pd.DataFrame | None = None
        self._zones: dict = {}
        self.setups_seen = 0
        self.skipped = 0

    def set_equity(self, equity: float) -> None:
        self.equity = equity

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy().reset_index(drop=True)
        d["atr"] = atr(d, self.atr_period)
        d["day"] = d.time.dt.date
        d["hour"] = d.time.dt.hour
        clo, chi = self.coil
        coil = d[(d.hour >= clo) & (d.hour < chi)]
        zones = {}
        for day, g in coil.groupby("day"):
            if len(g) < 6:
                continue
            zones[day] = {
                "hi": float(g.high.max()),
                "lo": float(g.low.min()),
                "traded": False,
            }
        self._zones = zones
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

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if self.df is None or in_position:
            return None
        row = self.df.iloc[bar.index]
        day = row["day"]
        z = self._zones.get(day)
        if z is None or z["traded"]:
            return None
        elo, ehi = self.entry
        if not (elo <= int(row["hour"]) < ehi):
            return None
        a = float(row["atr"])
        if not a or a != a or a <= 0:
            return None
        width = z["hi"] - z["lo"]
        if width < a * self.min_coil_atr or width > a * self.max_coil_atr:
            self.skipped += 1
            return None

        close = float(row["close"])
        open_ = float(row["open"])
        buf = a * self.sl_atr_buf
        side = sl = tp = None
        if close > z["hi"] and close > open_:
            side = Side.BUY
            sl = z["lo"] - buf
            risk = close - sl
            if risk <= 0:
                return None
            tp = close + risk * self.rr
        elif close < z["lo"] and close < open_:
            side = Side.SELL
            sl = z["hi"] + buf
            risk = sl - close
            if risk <= 0:
                return None
            tp = close - risk * self.rr
        else:
            return None

        z["traded"] = True
        self.setups_seen += 1
        return Signal(
            side=side, volume=self._volume(abs(close - sl)),
            sl=sl, tp=tp, reason="coil-break", rr=self.rr,
        )
