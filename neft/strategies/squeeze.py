"""Squeeze (Joovier, Trading for Dummies Day 9).

На 15м цена сжимается в треугольник: одновременно lower highs и higher lows.
Нужны минимум две отбойные точки сверху и две снизу. Вход не по касанию
линии, а когда цена ЛОМАЕТ и трендовую, и ближайший свинг (как в видео).
Идентификация на 15м, вход на 5м — в движке это один ряд 5-минуток:
свинги считаем шире, чтобы они совпадали с картиной 15м.

SL — за последний противоположный свинг. TP — ближайшая цель со стороны
пробоя, но не хуже min_rr (у автора обычно не ниже 2:1, исключения ~1.8).
"""
from __future__ import annotations

import pandas as pd

from neft.core.models import Side
from neft.core.strategy import Bar, Signal, Strategy
from neft.core.structure import swings


class Squeeze(Strategy):
    name = "squeeze"

    def __init__(
        self,
        swing_left: int = 2,
        swing_right: int = 2,
        min_taps: int = 2,
        min_rr: float = 2.0,
        expire_bars: int = 8,
        session: tuple[int, int] | None = None,
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        spec=None,
    ):
        self.swing_left, self.swing_right = swing_left, swing_right
        self.min_taps = min_taps
        self.min_rr = min_rr
        self.expire_bars = expire_bars
        # Как у HSS/London S/R: None = круглосуточно, (16, 19) = только окно.
        self.session = session
        self.risk_pct, self.risk_manager = risk_pct, risk_manager
        self.base_volume, self.spec = base_volume, spec
        self.equity = 0.0
        self.df: pd.DataFrame | None = None
        self._used: set[tuple] = set()
        self.setups_seen = 0
        self.skipped_rr = 0
        self.skipped_session = 0

    def set_equity(self, equity: float) -> None:
        self.equity = equity

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy().reset_index(drop=True)
        sw = swings(d, self.swing_left, self.swing_right)
        self.df = pd.concat([d, sw], axis=1)
        return self.df

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

    def _taps(self, col: str, i: int) -> list[tuple[int, float]]:
        d = self.df
        pts = []
        for j in range(i + 1):
            v = d.iloc[j][col]
            if pd.notna(v):
                pts.append((j, float(v)))
        return pts[-self.min_taps:]

    @staticmethod
    def _line(pts: list[tuple[int, float]], i: int) -> float:
        (i1, p1), (i2, p2) = pts[-2], pts[-1]
        if i2 == i1:
            return p2
        return p1 + (p2 - p1) * (i - i1) / (i2 - i1)

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if self.df is None or in_position:
            return None
        if self.session is not None:
            lo, hi = self.session
            h = int(pd.Timestamp(bar.time).hour)
            if not (lo <= h < hi):
                self.skipped_session += 1
                return None
        i = bar.index
        if i < 40:
            return None
        highs = self._taps("swing_high", i)
        lows = self._taps("swing_low", i)
        if len(highs) < self.min_taps or len(lows) < self.min_taps:
            return None
        # Lower highs + higher lows = сжатие.
        if not (highs[-2][1] > highs[-1][1] and lows[-2][1] < lows[-1][1]):
            return None
        key = (highs[-1][0], lows[-1][0])
        if key in self._used:
            return None

        upper = self._line(highs, i)
        lower = self._line(lows, i)
        if upper <= lower:
            return None
        last_hi, last_lo = highs[-1][1], lows[-1][1]
        self.setups_seen += 1

        side = None
        sl = tp_struct = None
        # Лонг: слом верхней линии И последнего хая. Шорт — зеркало.
        if bar.close > upper and bar.close > last_hi:
            side, sl, tp_struct = Side.BUY, last_lo, None
            older = [p for p in highs if p[1] > last_hi]
            tp_struct = older[-1][1] if older else last_hi + (last_hi - last_lo) * self.min_rr
        elif bar.close < lower and bar.close < last_lo:
            side, sl, tp_struct = Side.SELL, last_hi, None
            older = [p for p in lows if p[1] < last_lo]
            tp_struct = older[-1][1] if older else last_lo - (last_hi - last_lo) * self.min_rr
        else:
            return None

        sl_dist = abs(bar.close - sl)
        if sl_dist <= 0:
            return None
        rr_struct = abs(tp_struct - bar.close) / sl_dist
        if rr_struct < self.min_rr:
            tp = (bar.close + sl_dist * self.min_rr if side is Side.BUY
                  else bar.close - sl_dist * self.min_rr)
            if abs(tp - bar.close) / sl_dist < 1.8:
                self.skipped_rr += 1
                return None
        else:
            tp = tp_struct

        vol = self._volume(sl_dist)
        self._used.add(key)
        return Signal(
            side=side, volume=vol, sl=sl, tp=tp, entry=bar.close,
            entry_type="market", expire_bars=self.expire_bars,
            reason="squeeze-break", rr=abs(tp - bar.close) / sl_dist,
        )
