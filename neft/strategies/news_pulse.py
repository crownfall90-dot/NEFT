"""Торговля High-impact релизами: impulse / fade / straddle.

Направление — по M1-цене, не по «угадыванию макро». Surprise actual−forecast
только фильтр (если цифр нет — торгуем чисто по импульсу ≥ k·ATR).

Один EventTracker на символ: не больше одной сделки на событие.
Приоритет в Portfolio: Impulse → Fade → Straddle.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from neft.core.indicators import atr
from neft.core.models import Side
from neft.core.news import Event, events_for_instrument, has_material_surprise
from neft.core.strategy import Bar, ClosedTrade, Signal, Strategy


@dataclass
class EventTracker:
    """Общий учёт: какое событие уже взято / straddle вооружён."""
    traded: set[str] = field(default_factory=set)
    armed: set[str] = field(default_factory=set)

    def mark_traded(self, key: str) -> None:
        self.traded.add(key)
        self.armed.discard(key)


class _NewsBase(Strategy):
    def __init__(
        self,
        symbol: str,
        tracker: EventTracker,
        *,
        utc_offset_hours: float = 3.0,
        post_window_min: float = 15.0,
        pre_minutes: float = 2.0,
        impulse_atr: float = 0.8,
        atr_period: int = 14,
        rr: float = 1.5,
        surprise_min: float = 0.0,
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        spec=None,
    ):
        self.symbol = symbol
        self.tracker = tracker
        self.utc_offset = float(utc_offset_hours)
        self.post_window_min = float(post_window_min)
        self.pre_minutes = float(pre_minutes)
        self.impulse_atr = float(impulse_atr)
        self.atr_period = int(atr_period)
        self.rr = float(rr)
        self.surprise_min = float(surprise_min)
        self.risk_pct, self.risk_manager = risk_pct, risk_manager
        self.base_volume, self.spec = base_volume, spec
        self.equity = 0.0
        self.df: pd.DataFrame | None = None
        self.events: list[Event] = []
        # spike state for fade: event_key → {hi, lo, side, mid}
        self._spikes: dict[str, dict] = {}
        self._last_event_key: str = ""

    def set_equity(self, equity: float) -> None:
        self.equity = equity

    def set_events(self, events: list[Event]) -> None:
        self.events = list(events)

    def on_signal_rejected(self, signal: Signal, reason: str) -> None:
        key = self._last_event_key
        if key:
            self.tracker.traded.discard(key)
            self._last_event_key = ""

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy().reset_index(drop=True)
        d["atr"] = atr(d, self.atr_period)
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

    def _bar_utc(self, t: pd.Timestamp) -> pd.Timestamp:
        return pd.Timestamp(t) - pd.Timedelta(hours=self.utc_offset)

    def _relevant(self) -> list[Event]:
        return events_for_instrument(self.events, self.symbol)

    def _active_post(self, when_utc: pd.Timestamp) -> list[Event]:
        """События в окне [release, release+post_window]."""
        out = []
        for e in self._relevant():
            if e.key in self.tracker.traded:
                continue
            delta_m = (when_utc - e.time).total_seconds() / 60.0
            if 0 <= delta_m <= self.post_window_min:
                out.append(e)
        return out

    def _surprise_ok(self, e: Event) -> bool:
        """Если цифр нет — пропускаем фильтр; если есть — нужен ненулевой surprise."""
        flag = has_material_surprise(e, self.surprise_min)
        if flag is None:
            return True
        return bool(flag)

    def _impulse_move(self, row) -> tuple[Side | None, float]:
        """Направление и величина хода бара относительно ATR."""
        a = float(row["atr"])
        if not a or a != a or a <= 0:
            return None, 0.0
        body = float(row["close"]) - float(row["open"])
        rng = float(row["high"]) - float(row["low"])
        move = max(abs(body), rng * 0.85)
        if move < a * self.impulse_atr:
            return None, move
        if body > 0:
            return Side.BUY, move
        if body < 0:
            return Side.SELL, move
        # doji с большим range — по close vs mid
        mid = (float(row["high"]) + float(row["low"])) / 2
        if float(row["close"]) >= mid:
            return Side.BUY, move
        return Side.SELL, move

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        pass


class NewsImpulse(_NewsBase):
    name = "news_impulse"

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if self.df is None or in_position:
            return None
        row = self.df.iloc[bar.index]
        when = self._bar_utc(bar.time)
        active = self._active_post(when)
        if not active:
            return None
        side, _ = self._impulse_move(row)
        if side is None:
            return None
        # берём ближайшее к релизу событие
        e = min(active, key=lambda x: abs((when - x.time).total_seconds()))
        if not self._surprise_ok(e):
            return None

        close = float(row["close"])
        hi, lo = float(row["high"]), float(row["low"])
        if side is Side.BUY:
            sl = lo
            risk = close - sl
            if risk <= 0:
                return None
            tp = close + risk * self.rr
        else:
            sl = hi
            risk = sl - close
            if risk <= 0:
                return None
            tp = close - risk * self.rr

        self._last_event_key = e.key
        self.tracker.mark_traded(e.key)
        self._spikes[e.key] = {
            "hi": hi, "lo": lo, "side": side, "mid": (hi + lo) / 2,
        }
        vol = self._volume(risk)
        return Signal(
            side=side, volume=vol, sl=sl, tp=tp,
            reason=f"impulse:{e.currency} {e.title}",
            entry_type="market",
        )


class NewsFade(_NewsBase):
    name = "news_fade"

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if self.df is None or in_position:
            return None
        row = self.df.iloc[bar.index]
        when = self._bar_utc(bar.time)
        active = self._active_post(when)
        if not active:
            return None

        # Зафиксировать спайк, если ещё не торговали событие
        for e in active:
            if e.key in self.tracker.traded:
                continue
            if e.key in self._spikes:
                continue
            if not self._surprise_ok(e):
                continue
            side, _ = self._impulse_move(row)
            if side is None:
                continue
            self._spikes[e.key] = {
                "hi": float(row["high"]),
                "lo": float(row["low"]),
                "side": side,
                "mid": (float(row["high"]) + float(row["low"])) / 2,
                "armed_at": bar.index,
            }

        # Ищем разворот после спайка (следующая свеча против импульса)
        for e in active:
            if e.key in self.tracker.traded:
                continue
            sp = self._spikes.get(e.key)
            if not sp or sp.get("armed_at", -1) >= bar.index:
                continue  # спайк на этой же свече — ждём следующую
            spike_side: Side = sp["side"]
            close = float(row["close"])
            open_ = float(row["open"])
            # разворот: тело против спайка
            if spike_side is Side.BUY and not (close < open_):
                continue
            if spike_side is Side.SELL and not (close > open_):
                continue

            fade = Side.SELL if spike_side is Side.BUY else Side.BUY
            if fade is Side.SELL:
                sl = float(sp["hi"])
                risk = sl - close
                if risk <= 0:
                    continue
                mid_tp = float(sp["mid"])
                tp = mid_tp if mid_tp < close else close - risk * self.rr
            else:
                sl = float(sp["lo"])
                risk = close - sl
                if risk <= 0:
                    continue
                mid_tp = float(sp["mid"])
                tp = mid_tp if mid_tp > close else close + risk * self.rr

            self._last_event_key = e.key
            self.tracker.mark_traded(e.key)
            vol = self._volume(abs(close - sl))
            return Signal(
                side=fade, volume=vol, sl=sl, tp=tp,
                reason=f"fade:{e.currency} {e.title}",
                entry_type="market",
            )
        return None


class NewsStraddle(_NewsBase):
    """До релиза — пара buy-stop / sell-stop за pre-news high/low.

    Возвращает buy-leg как Signal; sell-leg кладёт в last_oco для Watcher (OCO).
    """
    name = "news_straddle"

    def __init__(self, *args, expire_bars: int = 20, **kwargs):
        super().__init__(*args, **kwargs)
        self.expire_bars = int(expire_bars)
        self.last_oco: tuple[Signal, Signal] | None = None

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        self.last_oco = None
        if self.df is None or in_position:
            return None
        row = self.df.iloc[bar.index]
        when = self._bar_utc(bar.time)
        a = float(row["atr"])
        if not a or a != a or a <= 0:
            return None

        # Ищем событие, до которого осталось ≤ pre_minutes и ещё не вооружены
        candidates = []
        for e in self._relevant():
            if e.key in self.tracker.traded or e.key in self.tracker.armed:
                continue
            mins_to = (e.time - when).total_seconds() / 60.0
            if 0 < mins_to <= self.pre_minutes:
                candidates.append((mins_to, e))
        if not candidates:
            return None
        _, e = min(candidates, key=lambda x: x[0])

        # Pre-news range: последние 5 закрытых баров до текущего
        i0 = max(0, bar.index - 5)
        window = self.df.iloc[i0:bar.index + 1]
        hi = float(window["high"].max())
        lo = float(window["low"].min())
        buf = a * 0.15
        buy_entry = hi + buf
        sell_entry = lo - buf
        if buy_entry <= sell_entry:
            return None

        # SL за противоположную границу, TP = RR
        buy_sl = sell_entry
        buy_risk = buy_entry - buy_sl
        sell_sl = buy_entry
        sell_risk = sell_sl - sell_entry
        if buy_risk <= 0 or sell_risk <= 0:
            return None
        buy_tp = buy_entry + buy_risk * self.rr
        sell_tp = sell_entry - sell_risk * self.rr
        vol = self._volume(min(buy_risk, sell_risk))

        buy_sig = Signal(
            side=Side.BUY, volume=vol, sl=buy_sl, tp=buy_tp,
            entry=buy_entry, entry_type="stop", expire_bars=self.expire_bars,
            reason=f"straddle:{e.currency} {e.title}",
        )
        sell_sig = Signal(
            side=Side.SELL, volume=vol, sl=sell_sl, tp=sell_tp,
            entry=sell_entry, entry_type="stop", expire_bars=self.expire_bars,
            reason=f"straddle:{e.currency} {e.title}",
        )
        self.last_oco = (buy_sig, sell_sig)
        self._last_event_key = e.key
        self.tracker.armed.add(e.key)
        return buy_sig

    def on_signal_rejected(self, signal: Signal, reason: str) -> None:
        key = self._last_event_key
        if key:
            self.tracker.armed.discard(key)
            self._last_event_key = ""
        self.last_oco = None

    def consume_oco(self) -> tuple[Signal, Signal] | None:
        pair = self.last_oco
        self.last_oco = None
        return pair

    def mark_filled(self) -> None:
        if self._last_event_key:
            self.tracker.mark_traded(self._last_event_key)
            self._last_event_key = ""
