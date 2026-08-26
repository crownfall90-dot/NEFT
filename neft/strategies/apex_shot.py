"""ApexShot — редкие A+ сетапы, минимум лузов.

Профиль: мало сделок, высокий фильтр качества, RR ≥ 2.

Условия одновременно:
  1) тренд: EMA21 и EMA55 согласованы, |разделение| ≥ sep·ATR;
  2) сжатие: текущий ATR ниже медианы ATR(50) — «спокойный» откат, не хаос;
  3) касание зоны медленной EMA + поглощающая свеча по тренду;
  4) только лучшие часы; ≤ 1 сделка в день; после убытка — пауза N баров.
"""
from __future__ import annotations

import pandas as pd

from neft.core.indicators import atr, ema
from neft.core.models import Side
from neft.core.strategy import Bar, ClosedTrade, Signal, Strategy


class ApexShot(Strategy):
    name = "apex_shot"

    def __init__(
        self,
        session: tuple[int, int] = (15, 20),
        fast: int = 21,
        slow: int = 55,
        atr_period: int = 14,
        atr_med_win: int = 40,
        sep_atr: float = 0.2,
        pullback_atr: float = 0.4,
        sl_atr: float = 0.8,
        rr: float = 2.0,
        atr_quiet: float = 1.2,
        cooldown_bars: int = 12,
        loss_cooldown_bars: int = 36,
        max_per_day: int = 1,
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        spec=None,
        commission_per_lot: float = 0.0,
    ):
        self.session = session
        self.fast, self.slow = fast, slow
        self.atr_period = atr_period
        self.atr_med_win = atr_med_win
        self.sep_atr = sep_atr
        self.pullback_atr = pullback_atr
        self.sl_atr = sl_atr
        self.rr = rr
        self.atr_quiet = atr_quiet
        self.cooldown_bars = cooldown_bars
        self.loss_cooldown_bars = loss_cooldown_bars
        self.max_per_day = max_per_day
        self.risk_pct, self.risk_manager = risk_pct, risk_manager
        self.base_volume, self.spec = base_volume, spec
        self.commission_per_lot = float(commission_per_lot)
        self.equity = 0.0
        self.df: pd.DataFrame | None = None
        self._last_i = -10**9
        self._cooldown_until = -1
        self._day_n: dict = {}
        self.setups_seen = 0
        self.skipped = 0

    def set_equity(self, equity: float) -> None:
        self.equity = equity

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy().reset_index(drop=True)
        d["ema_fast"] = ema(d.close, self.fast)
        d["ema_slow"] = ema(d.close, self.slow)
        d["atr"] = atr(d, self.atr_period)
        d["atr_med"] = d["atr"].rolling(self.atr_med_win, min_periods=20).median()
        d["hour"] = d.time.dt.hour
        d["day"] = d.time.dt.date
        self.df = d
        self._day_n = {}
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
        if cs <= 0:
            return 0.0
        return 2.0 * self.commission_per_lot / cs

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if self.df is None or in_position:
            return None
        i = bar.index
        warm = max(self.slow, self.atr_med_win) + 2
        if i < warm or i < self._cooldown_until:
            return None
        if i - self._last_i < self.cooldown_bars:
            return None
        row = self.df.iloc[i]
        day = row["day"]
        if self._day_n.get(day, 0) >= self.max_per_day:
            return None
        lo, hi = self.session
        if not (lo <= int(row["hour"]) < hi):
            return None

        a = float(row["atr"])
        am = float(row["atr_med"])
        ef, es = float(row["ema_fast"]), float(row["ema_slow"])
        if not a or a != a or not am or am != am or ef != ef or es != es:
            self.skipped += 1
            return None
        if a > am * self.atr_quiet:
            self.skipped += 1
            return None
        if abs(ef - es) < a * self.sep_atr:
            self.skipped += 1
            return None

        close = float(row["close"])
        open_ = float(row["open"])
        low, high = float(row["low"]), float(row["high"])
        zone = a * self.pullback_atr
        body = abs(close - open_)
        rng = high - low
        if rng <= 0 or body / rng < 0.45:
            # Нужна «сильная» свеча-поглощение, не дожи.
            self.skipped += 1
            return None

        side = None
        if ef > es and low <= es + zone and close > open_ and close > ef:
            side = Side.BUY
            sl = min(low, es) - a * self.sl_atr
            risk = close - sl
        elif ef < es and high >= es - zone and close < open_ and close < ef:
            side = Side.SELL
            sl = max(high, es) + a * self.sl_atr
            risk = sl - close
        else:
            return None
        if risk <= 0:
            return None

        pad = self._fee_pad()
        tp_dist = max(risk * self.rr, risk + pad)
        tp = close + tp_dist if side is Side.BUY else close - tp_dist
        vol = self._volume(risk)
        self.setups_seen += 1
        self._last_i = i
        self._day_n[day] = self._day_n.get(day, 0) + 1
        return Signal(
            side=side, volume=vol, sl=sl, tp=tp,
            reason="apex-shot", rr=tp_dist / risk,
        )

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        if trade.pnl > 0 or self.df is None or trade.closed_at is None:
            return
        hits = self.df.index[self.df.time == trade.closed_at]
        if not len(hits):
            return
        self._cooldown_until = int(hits[0]) + self.loss_cooldown_bars
