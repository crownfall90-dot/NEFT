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


def load(symbol: str, timeframe: str = "M1", bars: int = 50_000,
         refresh: bool = False) -> pd.DataFrame:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{symbol.replace('+', 'plus')}_{timeframe}_{bars}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, parse_dates=["time"])

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
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
    mt5.shutdown()
    if rates is None or not len(rates):
        raise RuntimeError(f"Нет истории по {symbol} {timeframe}")
    log.info("%s %s: терминал отдал %d баров", symbol, timeframe, len(rates))

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df[["time", "open", "high", "low", "close", "tick_volume", "spread"]]
    df.to_csv(path, index=False)
    log.info("%s %s: %d баров, %s — %s", symbol, timeframe, len(df),
             df.time.iloc[0], df.time.iloc[-1])
    return df
