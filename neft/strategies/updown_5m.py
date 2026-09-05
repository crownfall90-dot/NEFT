"""СТАРАЯ ВЕРСИЯ — трендовая, НЕ ИСПОЛЬЗОВАТЬ. См. updown_fade.py.

Эта стратегия ставит ПО тренду. Сканирование 62 688 баров показало, что на
5-минутном горизонте BTC работает обратное: после сильного движения цена
возвращается. Верхний квинтиль momentum даёт 47.1% Up, нижний — 51.4%.
То есть логика ниже систематически ставит на слабую сторону — отсюда 42.7%
на последней неделе вне выборки. Оставлено как отрицательный пример.

Рабочая версия — neft/strategies/updown_fade.py (mean-reversion, 52.5%).

---

Адаптация HSS под бинарный контракт «BTC вверх/вниз за 5 минут».

Контракт (predict.fun / Binance prediction): в момент T покупается Up или Down,
в T+5min сравнивается close(T+5) с close(T). Угадал — контракт стоит 1.00,
не угадал — 0.00. При цене 0.46 выигрыш даёт +117% на ставку, проигрыш −100%.
Порог безубытка = 46% винрейта.

Почему это НЕ обычная ScalpHA: там сделка закрывается по SL/TP, то есть важно
«куда цена сходит», а здесь — «где цена окажется ровно через 5 минут». Сетап,
который сходил в плюс и вернулся, для SL/TP победа, для бинарки — проигрыш.
Поэтому здесь нет стопов: только направление и фиксированная экспирация.

Фильтры подобраны под задачу «мало сделок, высокий винрейт»:
  * тренд по EMA (как в HSS) — направление сделки;
  * momentum за N баров — цена уже идёт в сторону сделки;
  * ATR-коридор — исключает и мёртвый флэт (шум решает исход), и всплески;
  * запрет входа против сильного импульса последней свечи (mean-reversion риск).

РЕЗУЛЬТАТ ВАЛИДАЦИИ (см. scripts/updown_oos.py) — читать перед торговлей:
дефолтные параметры дают 60% винрейта на 3 днях, на которых подбирались,
и 48.9% на 802 сделках вне выборки (95% ДИ 45.4–52.3%, порог б/у 46%).
Понедельная разбивка вне выборки: 47.9 / 57.4 / 48.2 / 46.5 / 42.7 — это
блуждание вокруг порога, а не устойчивое преимущество. Признаки подгонки:
сдвиг momentum_bars с 15 на 14 роняет винрейт до 45.9%, а задержка входа
на одну минуту — с 60% до 53%. Торговый перевес НЕ подтверждён.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from neft.core.indicators import atr, ema


class UpDown5m:
    """Сигналы направления для бинарного контракта с экспирацией 5 минут."""

    name = "updown_5m"

    def __init__(
        self,
        horizon: int = 5,            # баров до экспирации (1m свечи → 5 минут)
        ema_fast: int = 20,
        ema_slow: int = 60,
        momentum_bars: int = 15,     # окно оценки импульса
        min_momentum_atr: float = 1.5,    # импульс в долях ATR
        max_momentum_atr: float = 2.0,    # выше — риск разворота/перегрева
        atr_period: int = 14,
        min_atr_pct: float = 0.02,   # % от цены: ниже — флэт, решает шум
        max_atr_pct: float = 0.30,   # выше — слишком дико
        require_ema_stack: bool = True,   # fast/slow должны быть согласованы
        max_wick_ratio: float = 0.65,     # длинный фитиль против входа = отказ
        cooldown_bars: int = 5,      # пауза после сделки (не пересекать экспирации)
        session: tuple[int, int] | None = None,
    ):
        self.horizon = horizon
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.momentum_bars = momentum_bars
        self.min_momentum_atr = min_momentum_atr
        self.max_momentum_atr = max_momentum_atr
        self.atr_period = atr_period
        self.min_atr_pct = min_atr_pct
        self.max_atr_pct = max_atr_pct
        self.require_ema_stack = require_ema_stack
        self.max_wick_ratio = max_wick_ratio
        self.cooldown_bars = cooldown_bars
        self.session = session

        self.skipped_atr = 0
        self.skipped_momentum = 0
        self.skipped_stack = 0
        self.skipped_wick = 0
        self.skipped_session = 0

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy().reset_index(drop=True)
        d["ema_f"] = ema(d.close, self.ema_fast)
        d["ema_s"] = ema(d.close, self.ema_slow)
        d["atr"] = atr(d, self.atr_period)
        d["atr_pct"] = d.atr / d.close * 100.0

        # Импульс: смещение цены за окно, нормированное на ATR.
        d["mom"] = (d.close - d.close.shift(self.momentum_bars)) / d.atr.replace(0, np.nan)

        rng = (d.high - d.low).replace(0, np.nan)
        upper = d.high - d[["open", "close"]].max(axis=1)
        lower = d[["open", "close"]].min(axis=1) - d.low
        d["upper_wick"] = (upper / rng).fillna(0.0)
        d["lower_wick"] = (lower / rng).fillna(0.0)
        self.df = d
        return d

    def signals(self) -> pd.DataFrame:
        """Все сигналы разом: строка = сделка (индекс бара входа + сторона)."""
        d = self.df
        n = len(d)
        warmup = max(self.ema_slow, self.atr_period, self.momentum_bars) + 2
        rows: list[dict] = []
        last_entry = -10**9

        for i in range(warmup, n - self.horizon):
            r = d.iloc[i]
            if i - last_entry < self.cooldown_bars:
                continue
            if self.session is not None:
                lo, hi = self.session
                if not (lo <= r.time.hour < hi):
                    self.skipped_session += 1
                    continue

            ap = float(r.atr_pct)
            if not (self.min_atr_pct <= ap <= self.max_atr_pct):
                self.skipped_atr += 1
                continue

            mom = float(r.mom) if r.mom == r.mom else 0.0
            side = "Up" if mom > 0 else "Down"
            amom = abs(mom)
            if not (self.min_momentum_atr <= amom <= self.max_momentum_atr):
                self.skipped_momentum += 1
                continue

            if self.require_ema_stack:
                up_stack = r.close > r.ema_f > r.ema_s
                dn_stack = r.close < r.ema_f < r.ema_s
                if side == "Up" and not up_stack:
                    self.skipped_stack += 1
                    continue
                if side == "Down" and not dn_stack:
                    self.skipped_stack += 1
                    continue

            # Длинный фитиль против входа — рынок уже отбили, вход опасен.
            if side == "Up" and float(r.upper_wick) > self.max_wick_ratio:
                self.skipped_wick += 1
                continue
            if side == "Down" and float(r.lower_wick) > self.max_wick_ratio:
                self.skipped_wick += 1
                continue

            entry_px = float(r.close)
            exit_row = d.iloc[i + self.horizon]
            exit_px = float(exit_row.close)
            if exit_px == entry_px:
                outcome = "tie"
            elif (exit_px > entry_px) == (side == "Up"):
                outcome = "win"
            else:
                outcome = "loss"

            rows.append({
                "i_entry": i,
                "i_exit": i + self.horizon,
                "time": r.time,
                "exit_time": exit_row.time,
                "side": side,
                "entry": entry_px,
                "exit": exit_px,
                "delta": exit_px - entry_px,
                "outcome": outcome,
                "atr_pct": ap,
                "mom": mom,
            })
            last_entry = i

        return pd.DataFrame(rows)
