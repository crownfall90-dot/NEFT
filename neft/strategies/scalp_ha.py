"""HSS — Hybrid Super Scalping. Чеклист автора, без отклонений.

Только M1. Сигналы по Heikin Ashi, стоп / тейк / вход — по реальным ценам.

1. Chart setup: Heikin Ashi + EMA 100.
2. Market structure: покупки только если свечи выше EMA 100, продажи — ниже.
   Doji не должна касаться EMA: вся HA-свеча с одной стороны линии
   (buy: ha_low > EMA; sell: ha_high < EMA).
   Первый откат после пересечения EMA — структура, не вход.
3. Clean pullback: минимум 2 чистые свечи против тренда сразу до doji.
   Покупка: красные без верхнего фитиля. Продажа: зелёные без нижнего.
4. Entry: объёмная doji сразу по закрытию минутки.
   Doji = маленькое тело и фитили сверху и снизу.
   Объёмная = размер ≥ хотя бы одной из последних 1–3 свечей.
   Цвет doji совпадать с направлением желательно, но не обязательно.
   Если в этом откате до сигнальной уже была мелкая doji — сетап снят.
   CFD: стоп-вход от high (buy) / low (sell) doji, ≤3 бара на исполнение.
   Крипта sr_doji — по рынку на close doji (отдельно в factory).
5. SL: покупка под фитилём doji, продажа над фитилём.
6. TP: минимум 1:1 к риску (сладкое пятно 1:1.5–1:2, задаётся в панели).
   7. Комиссия Bybit: если при TP (после комиссии и спреда) сделка ≤ 0 —
   в сетап не входим (мелкая doji не стоит издержек).
8. ТА (опционально): вход только если doji касается свингового уровня —
   покупка у поддержки, продажа у сопротивления. Режим `sr_rsi` /
   `sr_room` / `full` добавляет RSI и запас до встречного уровня.
"""
import pandas as pd

from neft.core.indicators import add_features
from neft.core.models import Side
from neft.core.strategy import Bar, ClosedTrade, Signal, Strategy
from neft.core.ta_scan import enrich_ta

# Жёстко по чеклисту — панель не должна опускать ниже.
MIN_PULLBACK_BARS = 2
STOP_EXPIRE_BARS = 3


def _looks_crypto(name: str) -> bool:
    n = (name or "").upper()
    return "/" in n or ":" in n or n.endswith("USDT")


class ScalpHA(Strategy):
    name = "scalp_ha"

    def __init__(
        self,
        ema_period: int = 100,
        pullback_bars: int = MIN_PULLBACK_BARS,
        rr: float = 1.0,
        doji_body_pct: float = 0.10,
        clean_wick_pct: float = 0.05,
        sl_buffer_points: float = 2.0,
        min_sl_ratio: float = 0.25,   # доля медианного размера бара
        max_sl_ratio: float = 4.0,
        require_matching_doji: bool = False,
        require_structure: bool = True,
        min_bars_since_cross: int = 0,
        block_after_small_doji: bool = True,
        entry_mode: str = "stop",         # CFD: stop @ high/low doji; crypto → market
        expire_bars: int = STOP_EXPIRE_BARS,
        vol_mode: str = "min",
        vol_window: int = 3,
        tp_from_extreme: bool = True,
        session: tuple[int, int] | None = None,   # часы сервера, [от, до)
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        point: float = 1e-05,
        spec=None,
        commission_per_lot: float | None = None,  # None → Bybit CFD по spec
        ta_filter: str = "off",   # off | sr | sr_rsi | sr_room | full
        sr_atr: float = 0.85,     # касание уровня: доля ATR
        setup_mode: str = "hss",  # hss | sr_doji — doji на S/R, без EMA/отката
    ):
        self.ema_period = ema_period
        # Правило 3: минимум 2 чистые свечи; меньше — не принимаем.
        self.pullback_bars = max(MIN_PULLBACK_BARS, int(pullback_bars or MIN_PULLBACK_BARS))
        self.rr = rr
        self.doji_body_pct = doji_body_pct
        self.clean_wick_pct = clean_wick_pct
        self.sl_buffer_points = sl_buffer_points
        self.min_sl_ratio = min_sl_ratio
        self.max_sl_ratio = max_sl_ratio
        self.sl_buffer = 0.0
        self.min_sl = 0.0
        self.max_sl = float("inf")
        self.require_matching_doji = require_matching_doji
        self.require_structure = require_structure
        self.min_bars_since_cross = min_bars_since_cross
        self.block_after_small_doji = block_after_small_doji
        self.entry_mode = entry_mode
        # Правило 4: стоп от экстремума doji живёт ровно 3 бара.
        self.expire_bars = (
            STOP_EXPIRE_BARS if entry_mode == "stop"
            else max(1, int(expire_bars or 1))
        )
        self.vol_mode = vol_mode
        self.vol_window = vol_window
        self.tp_from_extreme = tp_from_extreme
        self.session = session
        self.ta_filter = (ta_filter or "off").strip().lower()
        self.sr_atr = float(sr_atr)
        self.setup_mode = (setup_mode or "hss").strip().lower()
        self.skipped_session = 0
        self.skipped_structure = 0
        self.skipped_small_doji = 0
        self.skipped_fee = 0
        self.skipped_ema_touch = 0
        self.skipped_ta = 0

        self.risk_pct = risk_pct
        self.risk_manager = risk_manager
        self.base_volume = base_volume
        self.spec = spec
        self.point = spec.point if spec else point
        self.equity = 0.0
        self._commission_override = commission_per_lot

        self.df: pd.DataFrame | None = None
        self.skipped_sl_range = 0
        self.setups_seen = 0

    def _commission_per_lot(self) -> float:
        if self._commission_override is not None:
            return float(self._commission_override)
        name = getattr(self.spec, "name", "") or ""
        if not name or _looks_crypto(name):
            return 0.0
        from neft.core.bybit_cfd_fees import commission_per_lot
        return float(commission_per_lot(name))

    def _edge_after_costs(self, sl_distance: float) -> float:
        """Ожидаемый $/лот при TP минус комиссия Bybit и спред на входе."""
        sp = self.spec
        cs = float(sp.contract_size) if sp else 100_000.0
        gross_tp = float(sl_distance) * float(self.rr) * cs
        fee = self._commission_per_lot()
        spread_cost = 0.0
        if sp is not None:
            pts = float(getattr(sp, "default_spread", 0) or 0)
            spread_cost = pts * float(sp.point) * cs
        return gross_tp - fee - spread_cost

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        self.df = add_features(df, self.ema_period, self.doji_body_pct,
                               self.clean_wick_pct, self.vol_mode,
                               self.vol_window)
        d = self.df
        # Тренд — HA-свечи относительно EMA с реального close (как на панели).
        up = (d.ha_close > d.ema).to_numpy()
        ha_bull = d.ha_bull.to_numpy()
        n = len(d)
        since = [0] * n
        ready = [False] * n
        done = [0] * n
        seen_pb = False
        run_bear = run_bull = 0
        for i in range(1, n):
            if up[i] != up[i - 1]:
                since[i] = 0
                ready[i] = False
                done[i] = 0
                seen_pb = False
                run_bear = run_bull = 0
                continue
            since[i] = since[i - 1] + 1
            against = (not ha_bull[i]) if up[i] else bool(ha_bull[i])
            if against:
                seen_pb = True
            # Структура готова после первого отката и возврата в сторону EMA.
            ready[i] = bool(ready[i - 1] or (seen_pb and not against))

            cb = bool(d.clean_bear.iat[i])
            cu = bool(d.clean_bull.iat[i])
            prev_bear, prev_bull = run_bear, run_bull
            run_bear = run_bear + 1 if cb else 0
            run_bull = run_bull + 1 if cu else 0
            done[i] = done[i - 1]
            if up[i] and prev_bear >= self.pullback_bars and run_bear == 0:
                done[i] += 1
            if (not up[i]) and prev_bull >= self.pullback_bars and run_bull == 0:
                done[i] += 1
        d["bars_since_cross"] = since
        d["structure_ready"] = ready
        d["pullbacks_done"] = done
        self.df = enrich_ta(d)

        med = float((df.high - df.low).median())
        self.min_sl = med * self.min_sl_ratio
        self.max_sl = med * self.max_sl_ratio
        self.sl_buffer = self.sl_buffer_points * self.point
        self.median_range = med
        return self.df

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

    def _spread_price(self, row) -> float:
        """Спред в цене — как в Backtester (spread_points × point)."""
        pts = int(getattr(row, "spread", 0) or 0)
        return pts * self.point

    def _market_fill(self, row, side: Side) -> float:
        """Цена market-входа: close ± spread (buy дороже, sell дешевле)."""
        sp = self._spread_price(row)
        c = float(row.close)
        return c + sp if side is Side.BUY else c - sp

    @staticmethod
    def _num(row, name: str) -> float | None:
        v = getattr(row, name, None)
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return x if x == x else None

    def _sr_tol(self, row) -> float:
        atr_v = self._num(row, "atr") or self.median_range or 0.0
        floor = (self.min_sl * 0.25) if self.min_sl else atr_v * 0.2
        return max(atr_v * self.sr_atr, floor)

    def _level_overlap(self, row, lvl: float | None) -> bool:
        """Свеча заходит в зону уровня (high/low пересекают lvl ± ATR)."""
        if lvl is None:
            return False
        tol = self._sr_tol(row)
        return float(row.low) <= lvl + tol and float(row.high) >= lvl - tol

    def _level_hit(self, row) -> tuple[Side | None, str | None]:
        """Какой уровень цена зацепила: поддержка → buy, сопротивление → sell."""
        res = self._num(row, "last_high")
        sup = self._num(row, "last_low")
        hit_r = self._level_overlap(row, res)
        hit_s = self._level_overlap(row, sup)
        close = float(row.close)
        if hit_r and hit_s:
            dr = abs(close - res) if res is not None else float("inf")
            ds = abs(close - sup) if sup is not None else float("inf")
            if dr < ds:
                return Side.SELL, "сопротивление"
            if ds < dr:
                return Side.BUY, "поддержка"
            return None, None
        if hit_r:
            return Side.SELL, "сопротивление"
        if hit_s:
            return Side.BUY, "поддержка"
        return None, None

    def _sr_tag(self, row, side: Side) -> str | None:
        """Doji касается свингового уровня: sell→сопротивление, buy→поддержка.

        CFD / классический HSS: расстояние close/extreme до свинга.
        Крипто-режим sr_doji смотрит перекрытие бара с уровнем отдельно.
        """
        tol = self._sr_tol(row)
        close, high, low = float(row.close), float(row.high), float(row.low)
        if side is Side.SELL:
            lvl = self._num(row, "last_high")
            if lvl is None:
                return None
            if min(abs(high - lvl), abs(close - lvl)) <= tol:
                return "сопротивление"
            return None
        lvl = self._num(row, "last_low")
        if lvl is None:
            return None
        if min(abs(low - lvl), abs(close - lvl)) <= tol:
            return "поддержка"
        return None

    def _sr_room(self, row, side: Side, sl_distance: float) -> bool:
        """До встречного свинга хватает на TP — иначе уровень съест тейк."""
        need = sl_distance * float(self.rr) * 0.95
        close = float(row.close)
        if side is Side.BUY:
            lvl = self._num(row, "last_high")
            return True if lvl is None else (lvl - close) >= need
        lvl = self._num(row, "last_low")
        return True if lvl is None else (close - lvl) >= need

    def _rsi_ok(self, row, side: Side) -> bool:
        rsi = self._num(row, "rsi14")
        if rsi is None:
            return True
        return rsi < 68 if side is Side.BUY else rsi > 32

    def _ta_block(self, row, side: Side, sl_distance: float | None = None,
                  skip_sr: bool = False) -> str | None:
        """Причина отказа TA, или None если вход ок. tag пишем в reason."""
        mode = self.ta_filter
        if mode in ("", "off", "none"):
            return None
        want_sr = (not skip_sr) and mode in ("sr", "sr_rsi", "sr_room", "full")
        want_rsi = mode in ("sr_rsi", "full", "rsi")
        want_room = mode in ("sr_room", "full")
        if want_sr and not self._sr_tag(row, side):
            return "не у уровня S/R"
        if want_rsi and not self._rsi_ok(row, side):
            return "RSI против входа"
        if want_room and sl_distance is not None and not self._sr_room(row, side, sl_distance):
            return "нет места до встречного уровня"
        return None

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if in_position or self.df is None:
            return None
        i = bar.index
        d = self.df
        row = d.iloc[i]

        # Крипта: только doji на S/R. CFD (setup_mode=hss) идёт по старому чеклисту.
        if self.setup_mode == "sr_doji":
            if self.session is not None:
                lo, hi = self.session
                if not (lo <= bar.time.hour < hi):
                    self.skipped_session += 1
                    return None
            return self._on_bar_sr_doji(i, d, row)

        if i < self.ema_period + self.pullback_bars + 3:
            return None

        # Kill Zone автора: 9:00–12:00 ET (= 16–19 UTC+3). None = круглосуточно.
        if self.session is not None:
            lo, hi = self.session
            if not (lo <= bar.time.hour < hi):
                self.skipped_session += 1
                return None

        # 4. Вход только на объёмной doji — по размеру свечи, по закрытию бара.
        if not (row.is_doji and row.big_doji):
            return None

        # 2. Покупки только выше EMA, продажи только ниже (HA-свеча vs линия).
        above = bool(row.ha_close > row.ema)
        side = Side.BUY if above else Side.SELL

        # 2c. Doji не касается EMA — вся свеча строго с одной стороны линии.
        ema_v = float(row.ema)
        if side is Side.BUY:
            if float(row.ha_low) <= ema_v:
                self.skipped_ema_touch += 1
                return None
        else:
            if float(row.ha_high) >= ema_v:
                self.skipped_ema_touch += 1
                return None

        # 3. Clean pullback: N баров СРАЗУ до doji — все чистые против тренда.
        #    Buy: clean_bear (красные HA без верхнего фитиля).
        #    Sell: clean_bull (зелёные HA без нижнего фитиля).
        pull = d.iloc[i - self.pullback_bars:i]
        if len(pull) < self.pullback_bars:
            return None
        if side is Side.BUY:
            if not bool(pull.clean_bear.all()):
                return None
        else:
            if not bool(pull.clean_bull.all()):
                return None

        # Цвет doji совпадает с направлением — предпочтительно, не обязательно.
        if self.require_matching_doji:
            if side is Side.BUY and not row.ha_bull:
                return None
            if side is Side.SELL and row.ha_bull:
                return None

        # 2b. Первый откат после пересечения EMA — создание структуры, не вход.
        if self.require_structure:
            if (not bool(row.structure_ready)
                    or (self.min_bars_since_cross
                        and row.bars_since_cross < self.min_bars_since_cross)):
                self.skipped_structure += 1
                return None

        # Мелкая doji внутри этого отката обесценивает сетап.
        # Не смотрим 5 баров назад: прошлый неудачный откат не блокирует новый.
        if self.block_after_small_doji:
            if bool(pull.is_doji.any()) or bool(pull.small_doji.any()):
                self.skipped_small_doji += 1
                return None

        # ТА: HSS-откат только у свингового уровня (sell = сопротивление).
        early = self._ta_block(row, side, sl_distance=None)
        if early:
            self.skipped_ta += 1
            return None

        self.setups_seen += 1

        # 4b/5. Разметка: вход от экстремума doji, стоп за противоположный фитиль.
        # Реальные high/low бара, не Heikin Ashi.
        if side is Side.BUY:
            entry = row.high
            sl = row.low - self.sl_buffer
        else:
            entry = row.low
            sl = row.high + self.sl_buffer
        sl_distance = abs(entry - sl)

        if not (self.min_sl <= sl_distance <= self.max_sl):
            self.skipped_sl_range += 1
            return None

        late = self._ta_block(row, side, sl_distance)
        if late:
            self.skipped_ta += 1
            return None

        sr_tag = self._sr_tag(row, side) or ""

        # 6. Тейк минимум 1:1 от размеченного входа.
        tp = (entry + sl_distance * self.rr if side is Side.BUY
              else entry - sl_distance * self.rr)

        # 7. Мелкая doji: TP после комиссии Bybit (+ спред) ≤ 0 → не входим.
        edge = self._edge_after_costs(sl_distance)
        if edge <= 0:
            self.skipped_fee += 1
            return None

        side_ru = "BUY" if side is Side.BUY else "SELL"
        trend = "выше EMA100" if side is Side.BUY else "ниже EMA100"
        pb_n = self.pullback_bars
        pb = (f"{pb_n} чистых медвежьих сразу до doji" if side is Side.BUY
              else f"{pb_n} чистых бычьих сразу до doji")
        fee = self._commission_per_lot()
        if self.entry_mode == "market":
            entry_desc = "вход по рынку на close doji"
        else:
            entry_desc = (
                f"стоп-вход от {'high' if side is Side.BUY else 'low'} doji "
                f"(ждём ≤{self.expire_bars} бар)"
            )
        why = (
            f"HSS {side_ru}: {trend}; структура ок (не 1-й откат); "
            f"{pb}; объёмная doji"
            + (f"; у {sr_tag}" if sr_tag else "")
            + f"; {entry_desc}, SL за фитилём, "
            f"TP RR 1:{self.rr}; edge≈${edge:.2f}/лот после комиссии ${fee:.2f}"
        )

        if self.entry_mode == "market":
            fill = self._market_fill(row, side)
            real_sl_distance = abs(fill - sl)
            if real_sl_distance <= 0:
                return None
            if self._edge_after_costs(real_sl_distance) <= 0:
                self.skipped_fee += 1
                return None
            tp = (fill + real_sl_distance * self.rr if side is Side.BUY
                  else fill - real_sl_distance * self.rr)
            volume = self._volume(real_sl_distance)
            entry_price = None
        else:
            volume = self._volume(sl_distance)
            entry_price = float(entry)

        return Signal(
            side=side,
            volume=volume,
            sl=float(sl), tp=float(tp),
            entry=entry_price,
            entry_type=self.entry_mode,
            expire_bars=self.expire_bars,
            reason=why,
        )

    def _on_bar_sr_doji(self, i: int, d, row) -> Signal | None:
        """Крипто-HSS: цена зашла в поддержку/сопротивление и закрылась doji.

        Направление берётся с уровня (buy от поддержки, sell от сопротивления).
        EMA / clean pullback / структура CFD-чеклиста не используются.
        ta_filter=full добавляет RSI и запас до встречного свинга.
        """
        if i < 40:
            return None
        if not bool(row.is_doji):
            return None
        side, sr_tag = self._level_hit(row)
        if side is None:
            self.skipped_ta += 1
            return None
        early = self._ta_block(row, side, sl_distance=None, skip_sr=True)
        if early:
            self.skipped_ta += 1
            return None

        self.setups_seen += 1
        if side is Side.BUY:
            entry = row.high
            sl = row.low - self.sl_buffer
        else:
            entry = row.low
            sl = row.high + self.sl_buffer
        sl_distance = abs(entry - sl)
        if not (self.min_sl <= sl_distance <= self.max_sl):
            self.skipped_sl_range += 1
            return None
        late = self._ta_block(row, side, sl_distance, skip_sr=True)
        if late:
            self.skipped_ta += 1
            return None

        edge = self._edge_after_costs(sl_distance)
        if edge <= 0:
            self.skipped_fee += 1
            return None

        side_ru = "BUY" if side is Side.BUY else "SELL"
        fee = self._commission_per_lot()
        why = (
            f"crypto HSS {side_ru}: doji у {sr_tag}"
            f"; вход по рынку на close doji, SL за фитилём, "
            f"TP RR 1:{self.rr}; edge≈${edge:.2f}/лот после комиссии ${fee:.2f}"
        )
        if self.entry_mode == "market":
            fill = self._market_fill(row, side)
            real_sl_distance = abs(fill - sl)
            if real_sl_distance <= 0:
                return None
            if self._edge_after_costs(real_sl_distance) <= 0:
                self.skipped_fee += 1
                return None
            tp = (fill + real_sl_distance * self.rr if side is Side.BUY
                  else fill - real_sl_distance * self.rr)
            volume = self._volume(real_sl_distance)
            entry_price = None
        else:
            tp = (entry + sl_distance * self.rr if side is Side.BUY
                  else entry - sl_distance * self.rr)
            volume = self._volume(sl_distance)
            entry_price = float(entry)
        return Signal(
            side=side, volume=volume, sl=float(sl), tp=float(tp),
            entry=entry_price, entry_type=self.entry_mode,
            expire_bars=self.expire_bars, reason=why,
        )

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        pass
