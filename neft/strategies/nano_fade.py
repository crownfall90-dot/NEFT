"""NanoFade — частые мелкие развороты, упор на низкую просадку.

Профиль: много сделок, маленький риск на штуку, быстрый TP.

Логика: в ликвидном окне после 3 однонаправленных свечей подряд ждём
разворотную (поглощение/против тренда микро-импульса) и берём короткий
отскок. SL компактный (ATR), TP чуть дальше риска, чтобы после комиссии
оставался плюс. Жёсткий дневной стоп по числу убытков подряд.
"""
from __future__ import annotations

import pandas as pd

from neft.core.indicators import atr, ema
from neft.core.models import Side
from neft.core.strategy import Bar, ClosedTrade, Signal, Strategy


class NanoFade(Strategy):
    name = "nano_fade"

    def __init__(
        self,
        session: tuple[int, int] = (16, 20),
        streak: int = 3,
        atr_period: int = 14,
        sl_atr: float = 0.55,
        rr: float = 1.25,
        ema_period: int = 34,
        cooldown_bars: int = 2,
        max_per_day: int = 8,
        max_day_losses: int = 3,
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        spec=None,
        commission_per_lot: float = 0.0,
    ):
        self.session = session
        self.streak = streak
        self.atr_period = atr_period
        self.sl_atr = sl_atr
        self.rr = rr
        self.ema_period = ema_period
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
        d["atr"] = atr(d, self.atr_period)
        d["ema"] = ema(d.close, self.ema_period)
        d["hour"] = d.time.dt.hour
        d["day"] = d.time.dt.date
        d["bull"] = d.close > d.open
        d["bear"] = d.close < d.open
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
        if cs <= 0:
            return 0.0
        return 2.0 * self.commission_per_lot / cs

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if self.df is None or in_position:
            return None
        i = bar.index
        need = self.streak + 1
        if i < max(need, self.ema_period + self.atr_period):
            return None
        if i - self._last_i < self.cooldown_bars:
            return None
        row = self.df.iloc[i]
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
        if not a or a != a or a <= 0 or e != e:
            self.skipped += 1
            return None

        window = self.df.iloc[i - self.streak: i]
        close = float(row["close"])
        open_ = float(row["open"])
        side = None
        # 3 бычьих → медвежий fade (шорт), только если уже выше EMA (перегрев).
        if bool(window["bull"].all()) and bool(row["bear"]) and close > e:
            side = Side.SELL
            sl = float(row["high"]) + a * self.sl_atr
            risk = sl - close
        elif bool(window["bear"].all()) and bool(row["bull"]) and close < e:
            side = Side.BUY
            sl = float(row["low"]) - a * self.sl_atr
            risk = close - sl
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
            reason="nano-fade", rr=tp_dist / risk,
        )

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        if trade.pnl > 0 or self.df is None or trade.closed_at is None:
            return
        hits = self.df.index[self.df.time == trade.closed_at]
        if not len(hits):
            return
        day = self.df.iloc[int(hits[0])]["day"]
        self._day_losses[day] = self._day_losses.get(day, 0) + 1
