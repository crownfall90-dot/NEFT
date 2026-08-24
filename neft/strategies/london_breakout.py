"""5-Minute London Breakout.

Диапазон лондонской сессии 03:00-09:00 ET служит границей: куда цена из него
выйдет после открытия американского рынка, туда и торгуем.

  1. Бокс = хай и лоу с 03:00 до 09:00 ET (= 10:00-16:00 на сервере UTC+3).
  2. Ждём выхода пятиминутной свечи за границу бокса.
  3. Вход сразу по пробою, но не раньше 09:30 ET (16:30 сервера) — до открытия
     рынка объёма нет, и автор такие пробои пропускает.
  4. Стоп за свечу, которая пробила границу.
  5. Тейк 2:1.
  6. Одна сделка в день: какая сторона пробилась первой, та и торгуется.
  7. После 11:00 ET (18:00 сервера) входов нет — объём падает.
"""
import pandas as pd

from neft.core.models import Side
from neft.core.strategy import Bar, ClosedTrade, Signal, Strategy
from neft.core.structure import session_range


class LondonBreakout(Strategy):
    name = "london_breakout"

    def __init__(
        self,
        box=(10, 16),            # часы сервера = 03:00-09:00 ET
        entry_from: int = 16,    # 09:00 ET; открытие рынка — 16:30
        entry_from_minute: int = 30,
        entry_until: int = 18,   # 11:00 ET
        rr: float = 2.0,
        sl_buffer_points: float = 1.0,
        expire_bars: int = 200,
        session: tuple[int, int] | None = None,
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        spec=None,
    ):
        self.box = box
        self.entry_from, self.entry_from_minute = entry_from, entry_from_minute
        self.entry_until = entry_until
        self.rr = rr
        self.sl_buffer_points = sl_buffer_points
        self.expire_bars = expire_bars
        # Внешний фильтр поверх авторского окна 16:30–18. Если задан —
        # вход только на пересечении с ним (для сравнения 24/7 vs 16-19).
        self.session = session
        self.risk_pct, self.risk_manager = risk_pct, risk_manager
        self.base_volume, self.spec = base_volume, spec
        self.point = spec.point if spec else 1e-05
        self.equity = 0.0

        self.df: pd.DataFrame | None = None
        self.zones: dict = {}
        self._traded: set = set()
        self._late_days: set = set()
        self._cur_day = None
        self.boxes_seen = 0
        self.skipped_early = 0
        self.skipped_late = 0
        self.skipped_session = 0

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy().reset_index(drop=True)
        d["day"] = d.time.dt.date
        d["hour"] = d.time.dt.hour
        sr = session_range(df, *self.box)
        self.zones = {r.day: r for r in sr.itertuples()}
        self.boxes_seen = len(self.zones)
        self.df = d
        return d

    def set_equity(self, equity: float) -> None:
        self.equity = equity

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
        row = self.df.iloc[i]
        day = row.day
        if day != self._cur_day:
            self._cur_day = day
        if day in self._traded:
            return None

        z = self.zones.get(day)
        if z is None:
            return None

        t = bar.time
        if self.session is not None:
            lo, hi = self.session
            if not (lo <= t.hour < hi):
                self.skipped_session += 1
                return None
        # Пока бокс формируется, его границы касаются цены по определению —
        # искать пробой можно только после закрытия окна.
        if t.hour < self.box[1]:
            return None

        after_open = (t.hour > self.entry_from
                      or (t.hour == self.entry_from and t.minute >= self.entry_from_minute))
        if not after_open:
            # Пробой случился до открытия рынка — день пропускаем целиком,
            # как это делает автор.
            if row.high > z.high or row.low < z.low:
                self._traded.add(day)
                self.skipped_early += 1
            return None
        if t.hour >= self.entry_until:
            if day not in self._late_days:
                self._late_days.add(day)
                self.skipped_late += 1
            return None

        buf = self.sl_buffer_points * self.point
        # Какая сторона пробьётся первой, та и торгуется.
        if row.high >= z.high:
            side, level = Side.BUY, z.high
        elif row.low <= z.low:
            side, level = Side.SELL, z.low
        else:
            return None

        self._traded.add(day)
        # Стоп и тейк посчитает движок по свече, которая исполнит ордер.
        approx_risk = abs(row.high - row.low) or buf
        return Signal(
            side=side, volume=self._volume(approx_risk),
            sl=None, tp=None,
            entry=float(level), entry_type="stop",
            expire_bars=self.expire_bars,
            sl_mode="trigger_bar", rr=self.rr, sl_buffer=buf,
            reason=f"London box {side.value} RR1:{self.rr}",
        )

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        pass
