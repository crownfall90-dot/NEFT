"""London Support & Resistance.

Хай и лоу лондонской сессии — опорные уровни для нью-йоркской. Когда цена
возвращается к ним, ждём слом структуры на M1 и входим против движения,
которое привело цену к уровню.

Отличие от HSS: цель берётся не от фиксированного RR, а от ближайшего
подтверждённого свинга — как рисует автор.

Роль зоны определяется тем, с какой стороны цена подошла: пробитая поддержка
становится сопротивлением (пробой и ретест).
"""
import pandas as pd

from neft.core.models import Side
from neft.core.strategy import Bar, ClosedTrade, Signal, Strategy
from neft.core.structure import session_range, swings


class LondonSR(Strategy):
    name = "london_sr"

    def __init__(
        self,
        london=(11, 16),          # часы сервера = 04:00-09:00 ET
        ny=(16, 23),              # окно поиска входа
        swing_left: int = 3,
        swing_right: int = 3,
        zone_pad_ratio: float = 0.15,   # запас к зоне, доля её высоты
        min_rr: float = 1.0,            # ниже этого сделку не берём
        fallback_rr: float = 2.0,       # если подходящего свинга нет
        max_wait_bars: int = 120,       # сколько ждать слом после касания
        expire_bars: int = 5,
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        spec=None,
    ):
        self.london, self.ny = london, ny
        self.swing_left, self.swing_right = swing_left, swing_right
        self.zone_pad_ratio = zone_pad_ratio
        self.min_rr, self.fallback_rr = min_rr, fallback_rr
        self.max_wait_bars = max_wait_bars
        self.expire_bars = expire_bars
        self.risk_pct, self.risk_manager = risk_pct, risk_manager
        self.base_volume, self.spec = base_volume, spec
        self.equity = 0.0

        self.df: pd.DataFrame | None = None
        self.zones: dict = {}
        self._tap = None            # активное касание: (роль, бар, уровень зоны)
        self._traded_today: set = set()
        self.taps_seen = 0
        self.skipped_no_shift = 0
        self.skipped_rr = 0

    # ── подготовка ───────────────────────────────────────────────────
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy().reset_index(drop=True)
        sw = swings(d, self.swing_left, self.swing_right)
        d = pd.concat([d, sw], axis=1)
        d["day"] = d.time.dt.date
        d["hour"] = d.time.dt.hour

        sr = session_range(df, *self.london)
        self.zones = {r.day: r for r in sr.itertuples()}
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

    # ── логика ───────────────────────────────────────────────────────
    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if self.df is None:
            return None
        i = bar.index
        d = self.df
        row = d.iloc[i]
        day = row.day

        if day != getattr(self, "_cur_day", None):
            self._cur_day = day
            self._tap = None            # каждый день зоны новые

        z = self.zones.get(day)
        if z is None or not (self.ny[0] <= row.hour < self.ny[1]):
            return None
        if in_position or day in self._traded_today:
            return None

        # Зоны с запасом: цена редко разворачивается ровно по уровню.
        hi_h = z.high - z.high_zone_bot
        lo_h = z.low_zone_top - z.low
        hi_top = z.high + hi_h * self.zone_pad_ratio
        hi_bot = z.high_zone_bot - hi_h * self.zone_pad_ratio
        lo_top = z.low_zone_top + lo_h * self.zone_pad_ratio
        lo_bot = z.low - lo_h * self.zone_pad_ratio

        # 1. Касание зоны. Роль определяется стороной подхода.
        if self._tap is None:
            prev = d.iloc[i - 1]
            if hi_bot <= row.high and row.low <= hi_top:
                role = "resistance" if prev.close < hi_bot else "support"
                self._tap = (role, i, z.high if role == "resistance" else z.high_zone_bot)
                self.taps_seen += 1
            elif lo_bot <= row.high and row.low <= lo_top:
                role = "support" if prev.close > lo_top else "resistance"
                self._tap = (role, i, z.low if role == "support" else z.low_zone_top)
                self.taps_seen += 1
            return None

        role, tap_i, level = self._tap
        if i - tap_i > self.max_wait_bars:
            self._tap = None
            self.skipped_no_shift += 1
            return None

        # 2. Слом структуры на M1 подтверждает разворот от уровня.
        # От сопротивления ждём продажу: пробой ближайшего подтверждённого лоу.
        window = d.iloc[tap_i:i + 1]
        if role == "resistance":
            trigger = row.last_low
            stop_ref = window.high.max()
            if pd.isna(trigger) or row.low > trigger:
                return None                     # лоу ещё не пробит
            side, entry, sl = Side.SELL, float(trigger), float(stop_ref)
            targets = d.iloc[max(0, i - 600):i].swing_low.dropna()
            targets = targets[targets < entry]
            tp = float(targets.iloc[-1]) if len(targets) else None
        else:
            trigger = row.last_high
            stop_ref = window.low.min()
            if pd.isna(trigger) or row.high < trigger:
                return None
            side, entry, sl = Side.BUY, float(trigger), float(stop_ref)
            targets = d.iloc[max(0, i - 600):i].swing_high.dropna()
            targets = targets[targets > entry]
            tp = float(targets.iloc[-1]) if len(targets) else None

        risk = abs(entry - sl)
        if risk <= 0:
            self._tap = None
            return None

        # 3. Цель — ближайший свинг; если его нет, отступаем к фиксированному RR.
        if tp is None:
            tp = entry - risk * self.fallback_rr if side is Side.SELL \
                else entry + risk * self.fallback_rr
        if abs(tp - entry) / risk < self.min_rr:
            self._tap = None
            self.skipped_rr += 1
            return None

        self._tap = None
        self._traded_today.add(day)
        return Signal(
            side=side, volume=self._volume(risk),
            sl=sl, tp=tp, entry=entry, entry_type="stop",
            expire_bars=self.expire_bars,
            reason=f"London {role} RR{abs(tp - entry) / risk:.1f}",
        )

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        pass
