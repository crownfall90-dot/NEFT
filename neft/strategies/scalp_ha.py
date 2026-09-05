"""Скальпинг на Heikin Ashi: EMA-фильтр, чистый откат, вход на объёмном doji.

Правила (из разбора видео «The Best Scalping Strategy»):
  1. Heikin Ashi + EMA 100 на графике.
  2. Покупки только выше EMA, продажи только ниже.
  3. Откат: минимум N подряд «чистых» свечей против тренда.
     Чистая = без фитиля со стороны движения.
  4. Вход на doji с объёмом не ниже максимума последних 3 баров.
  5. Стоп за фитиль doji.
  6. Тейк по соотношению риск/прибыль.

Ключевой момент реализации: сигналы считаются по Heikin Ashi, но стоп, тейк
и точка входа — по РЕАЛЬНЫМ ценам. HA сглажен, и торговля по его уровням даёт
прибыль, которой на счёте не существует.
"""
import pandas as pd

from neft.core.indicators import add_features
from neft.core.models import Side
from neft.core.strategy import Bar, ClosedTrade, Signal, Strategy


class ScalpHA(Strategy):
    name = "scalp_ha"

    def __init__(
        self,
        ema_period: int = 100,
        pullback_bars: int = 2,
        rr: float = 1.0,
        doji_body_pct: float = 0.10,
        clean_wick_pct: float = 0.05,
        sl_buffer_points: float = 2.0,
        min_sl_ratio: float = 0.25,   # доля медианного размера бара
        max_sl_ratio: float = 4.0,
        require_matching_doji: bool = False,
        require_structure: bool = True,     # не входить на первом откате после EMA
        min_bars_since_cross: int = 20,
        block_after_small_doji: bool = True,
        entry_mode: str = "stop",           # "stop" = по экстремуму doji;
                                            # "market" = по закрытию, как у автора
        expire_bars: int = 3,
        vol_mode: str = "min",
        vol_window: int = 2,
        tp_from_extreme: bool = True,
        session: tuple[int, int] | None = None,   # часы сервера, [от, до)
        risk_pct: float | None = None,
        risk_manager=None,
        base_volume: float = 0.01,
        point: float = 1e-05,
        spec=None,
    ):
        self.ema_period = ema_period
        self.pullback_bars = pullback_bars
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
        self.expire_bars = expire_bars
        self.vol_mode = vol_mode
        self.vol_window = vol_window
        self.tp_from_extreme = tp_from_extreme
        self.session = session
        self.skipped_session = 0
        self.skipped_structure = 0
        self.skipped_small_doji = 0

        self.risk_pct = risk_pct
        self.risk_manager = risk_manager
        self.base_volume = base_volume
        self.spec = spec
        self.point = spec.point if spec else point
        self.equity = 0.0

        self.df: pd.DataFrame | None = None
        self.skipped_sl_range = 0   # сигналы, отсеянные по длине стопа
        self.setups_seen = 0

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        self.df = add_features(df, self.ema_period, self.doji_body_pct,
                               self.clean_wick_pct, self.vol_mode,
                               self.vol_window)
        # Пороги стопа задаются относительно типичного размера бара — иначе
        # значения, подобранные на EURUSD, бессмысленны на индексе.
        d = self.df
        # Структура рынка: сколько баров прошло с пересечения EMA и сколько
        # откатов уже завершилось. Первый откат после пересечения не торгуем —
        # рынок ещё не подтвердил структуру над/под EMA.
        up = (d.close > d.ema).to_numpy()
        n = len(d)
        since = [0] * n
        done = [0] * n
        run_bear = run_bull = 0
        for i in range(1, n):
            since[i] = 0 if up[i] != up[i - 1] else since[i - 1] + 1
            done[i] = 0 if up[i] != up[i - 1] else done[i - 1]
            cb = bool(d.clean_bear.iat[i]); cu = bool(d.clean_bull.iat[i])
            prev_bear, prev_bull = run_bear, run_bull
            run_bear = run_bear + 1 if cb else 0
            run_bull = run_bull + 1 if cu else 0
            # откат считается завершённым, когда серия из >=2 чистых свечей прервалась
            if up[i] and prev_bear >= 2 and run_bear == 0:
                done[i] += 1
            if (not up[i]) and prev_bull >= 2 and run_bull == 0:
                done[i] += 1
        d["bars_since_cross"] = since
        d["pullbacks_done"] = pd.Series(done, index=d.index).groupby(
            (pd.Series(up, index=d.index) != pd.Series(up, index=d.index).shift()
             ).cumsum()).cumsum()

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

    def on_bar(self, bar: Bar, in_position: bool) -> Signal | None:
        if in_position or self.df is None:
            return None
        i = bar.index
        if i < self.ema_period + self.pullback_bars + 3:
            return None

        d = self.df
        row = d.iloc[i]

        # Kill Zone: автор торгует 9:00-12:00 по восточному времени.
        if self.session is not None:
            lo, hi = self.session
            if not (lo <= bar.time.hour < hi):
                self.skipped_session += 1
                return None

        # 4. Вход только на "высокообъёмной" doji — по РАЗМЕРУ свечи.
        if not (row.is_doji and row.big_doji):
            return None

        # 2. Фильтр тренда по реальной цене относительно EMA.
        above = row.close > row.ema
        side = Side.BUY if above else Side.SELL

        # 3. Откат: N подряд чистых свечей ПРОТИВ направления сделки,
        # считаем на барах до doji.
        pull = d.iloc[i - self.pullback_bars:i]
        if side is Side.BUY:
            if not bool(pull.clean_bear.all()):
                return None
        else:
            if not bool(pull.clean_bull.all()):
                return None

        # Необязательное правило: цвет doji совпадает с направлением сделки.
        if self.require_matching_doji:
            if side is Side.BUY and not row.ha_bull:
                return None
            if side is Side.SELL and row.ha_bull:
                return None

        # 2b. Структура рынка: первый откат после пересечения EMA пропускаем.
        if self.require_structure:
            if (row.bars_since_cross < self.min_bars_since_cross
                    or row.pullbacks_done < 1):
                self.skipped_structure += 1
                return None

        # Маленькая doji перед сигнальной обесценивает сетап.
        if self.block_after_small_doji:
            back = d.iloc[max(0, i - 5):i]
            if bool(back.small_doji.any()):
                self.skipped_small_doji += 1
                return None

        self.setups_seen += 1

        # 4b/5. Вход по экстремуму doji, стоп за её противоположный фитиль.
        # Реальные экстремумы бара, не Heikin Ashi.
        if side is Side.BUY:
            entry = row.high
            sl = row.low - self.sl_buffer
        else:
            entry = row.low
            sl = row.high + self.sl_buffer
        sl_distance = abs(entry - sl)

        # Слишком узкий стоп съедается спредом, слишком широкий ломает RR.
        if not (self.min_sl <= sl_distance <= self.max_sl):
            self.skipped_sl_range += 1
            return None

        # 6. Тейк. Автор размечает сделку от фитиля doji, а входит по рынку на
        # закрытии свечи. Из-за этого фактический RR выходит выше планового —
        # у него в среднем 1.42 вместо 1.0.
        tp = (entry + sl_distance * self.rr if side is Side.BUY
              else entry - sl_distance * self.rr)

        if self.entry_mode == "market":
            # Реальный риск считается от цены входа, а не от разметки.
            real_entry = row.close
            real_sl_distance = abs(real_entry - sl)
            if real_sl_distance <= 0:
                return None
            if not self.tp_from_extreme:
                tp = (real_entry + real_sl_distance * self.rr if side is Side.BUY
                      else real_entry - real_sl_distance * self.rr)
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
            reason=f"doji RR1:{self.rr}",
        )

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        pass
