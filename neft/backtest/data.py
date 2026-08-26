"""История из MT5 с кэшем на диск — терминал отдаёт максимум ~50к баров M1."""
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

from neft.core.config import ROOT

log = logging.getLogger(__name__)
CACHE = ROOT / "data"

TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1,
}


def _ensure_mt5() -> bool:
    """True = мы сами открыли соединение и обязаны закрыть."""
    if mt5.terminal_info() is not None:
        return False
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    return True


def _rates_to_df(rates) -> pd.DataFrame:
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df[["time", "open", "high", "low", "close", "tick_volume", "spread"]]


def load(symbol: str, timeframe: str = "M1", bars: int = 50_000,
         refresh: bool = False) -> pd.DataFrame:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{symbol.replace('+', 'plus')}_{timeframe}_{bars}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, parse_dates=["time"])

    owned = _ensure_mt5()
    try:
        tf = TIMEFRAMES[timeframe]
        mt5.symbol_select(symbol, True)

        # Терминал держит историю лениво: пока её не запросили, глубины нет.
        # Прогреваем мелким запросом, затем добираем нужный объём с повторами.
        mt5.copy_rates_from_pos(symbol, tf, 0, 10)

        # Терминал отклоняет запрос на maxbars и больше: нужен запас.
        maxbars = mt5.terminal_info().maxbars
        want = min(bars, maxbars - 1000)

        rates = None
        while want >= 1000:
            for _ in range(4):
                rates = mt5.copy_rates_from_pos(symbol, tf, 0, want)
                if rates is not None and len(rates):
                    break
                # Запрос по диапазону заставляет терминал тянуть историю с сервера.
                mt5.copy_rates_range(symbol, tf,
                                     datetime.now() - timedelta(days=730), datetime.now())
                time.sleep(1.5)
            if rates is not None and len(rates):
                break
            want //= 2      # глубины столько нет — просим меньше
    finally:
        if owned:
            mt5.shutdown()
    if rates is None or not len(rates):
        raise RuntimeError(f"Нет истории по {symbol} {timeframe}")
    log.info("%s %s: терминал отдал %d баров", symbol, timeframe, len(rates))

    df = _rates_to_df(rates)
    df.to_csv(path, index=False)
    log.info("%s %s: %d баров, %s — %s", symbol, timeframe, len(df),
             df.time.iloc[0], df.time.iloc[-1])
    return df


def load_days(symbol: str, timeframe: str = "M1", days: int = 90,
              refresh: bool = False, *, chunk_days: int = 25) -> pd.DataFrame:
    """История за N календарных дней через copy_rates_range (чанки).

    Нужна для окон длиннее maxbars терминала (на M1 ~3 месяца часто
    больше одного запроса). Кэш: data/{symbol}_{tf}_{days}d.csv
    """
    CACHE.mkdir(exist_ok=True)
    safe = symbol.replace("+", "plus")
    path = CACHE / f"{safe}_{timeframe}_{int(days)}d.csv"
    if path.exists() and not refresh:
        df = pd.read_csv(path, parse_dates=["time"])
        if len(df):
            return df

    owned = _ensure_mt5()
    try:
        tf = TIMEFRAMES[timeframe]
        mt5.symbol_select(symbol, True)
        mt5.copy_rates_from_pos(symbol, tf, 0, 10)

        end = datetime.now()
        start = end - timedelta(days=int(days))
        # Прогрев: сервер подтягивает историю в терминал.
        mt5.copy_rates_range(symbol, tf, start, end)
        time.sleep(1.0)

        parts: list[pd.DataFrame] = []
        cur = start
        while cur < end:
            nxt = min(cur + timedelta(days=chunk_days), end)
            rates = None
            for _ in range(4):
                rates = mt5.copy_rates_range(symbol, tf, cur, nxt)
                if rates is not None and len(rates):
                    break
                mt5.copy_rates_range(symbol, tf, start, end)
                time.sleep(1.2)
            if rates is not None and len(rates):
                parts.append(_rates_to_df(rates))
            cur = nxt
    finally:
        if owned:
            mt5.shutdown()

    if not parts:
        raise RuntimeError(f"Нет истории по {symbol} {timeframe} за {days}д")
    df = (pd.concat(parts, ignore_index=True)
            .drop_duplicates(subset=["time"])
            .sort_values("time")
            .reset_index(drop=True))
    df.to_csv(path, index=False)
    log.info("%s %s: %d баров за ~%dд, %s — %s", symbol, timeframe, len(df),
             days, df.time.iloc[0], df.time.iloc[-1])
    return df


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Агрегация M1 → M5/M15 для графика отчёта (легче файла)."""
    if df.empty:
        return df.copy()
    g = (df.set_index("time")
           .resample(rule, label="left", closed="left")
           .agg(open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"),
                tick_volume=("tick_volume", "sum"),
                spread=("spread", "last"))
           .dropna(subset=["open", "close"])
           .reset_index())
    return g
