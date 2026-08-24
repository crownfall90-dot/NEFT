"""История с криптобирж через ccxt, с постраничной докачкой и кэшем.

Биржа отдаёт максимум 1000-1500 свечей за запрос, поэтому длинную историю
собираем кусками. Результат приводится к тем же колонкам, что и данные MT5,
чтобы стратегии не знали, откуда пришли бары.
"""
import logging
import time
from pathlib import Path

import ccxt
import pandas as pd

from neft.core.config import ROOT

log = logging.getLogger(__name__)
CACHE = ROOT / "data" / "crypto"
_TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
         "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

# Спреды измерены по стакану, а не выбраны на глаз: у BTC это 0.013 б.п.,
# у мелких альтов — единицы. Разница на два порядка, для скальпинга решающая.
SPREADS_FILE = ROOT / "data" / "crypto_spreads.json"
FALLBACK_SPREAD_BPS = 3.0
_measured: dict | None = None

# Комиссия USDT-перпетуалов, стандартный тариф без VIP/скидок за объём.
# Bybit взят как более высокая (консервативная) из двух интегрированных
# бирж — Binance берёт чуть меньше (0.05%/0.02%). Проверено на
# bybit.com/en/announcement-info/fee-rate (авг. 2026):
#   тейкер 0.055% — вход всегда (стоп/маркет) и выход по SL (гарантия
#   исполнения важнее экономии);
#   мейкер 0.020% — выход по TP лимитным ордером, цена сама доходит до
#   уровня, гнаться не нужно.
TAKER_FEE = 0.00055
MAKER_FEE = 0.00020


def _bps(symbol: str) -> float:
    global _measured
    if _measured is None:
        import json
        _measured = (json.loads(SPREADS_FILE.read_text(encoding="utf-8"))
                     if SPREADS_FILE.exists() else {})
    return float(_measured.get(symbol, FALLBACK_SPREAD_BPS))


def load(symbol: str, timeframe: str = "1m", bars: int = 50_000,
         exchange_id: str = "binance", refresh: bool = False,
         days: float | None = None) -> pd.DataFrame:
    """days — сколько календарных дней истории взять (пересчитывается в bars
    под нужный timeframe). Приоритетнее bars, если задан."""
    CACHE.mkdir(parents=True, exist_ok=True)
    safe = symbol.replace("/", "-").replace(":", "_")

    # bars должен быть пересчитан из days ДО построения пути кэша — иначе
    # запросы на разные периоды коллизируют на одном файле, названном по
    # параметру bars по умолчанию, и отдают друг другу чужие данные.
    if days is not None:
        tf_ms_tmp = _TF_MS.get(timeframe)
        if tf_ms_tmp is None:
            tf_ms_tmp = ccxt.Exchange.parse_timeframe(timeframe) * 1000
        bars = int(days * 86400 * 1000 / tf_ms_tmp)

    path = CACHE / f"{exchange_id}_{safe}_{timeframe}_{bars}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, parse_dates=["time"])

    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True,
                                     "options": {"defaultType": "swap"}})
    ex.load_markets()
    limit = 1000
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    since = ex.milliseconds() - bars * tf_ms

    rows: list[list] = []
    while len(rows) < bars:
        chunk = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        if not chunk:
            break
        rows += chunk
        since = chunk[-1][0] + tf_ms
        if len(chunk) < limit:
            break
        time.sleep(ex.rateLimit / 1000)

    if not rows:
        raise RuntimeError(f"Нет истории по {symbol} {timeframe} на {exchange_id}")

    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts").tail(bars).reset_index(drop=True)
    df["time"] = pd.to_datetime(df.ts, unit="ms")

    # Приводим к формату баров MT5: тиковый объём и спред в пунктах.
    df["tick_volume"] = df.volume
    point = _point_for(df.close.median())
    df["spread"] = (df.close * _bps(symbol) / 10_000 / point).round().astype(int)
    df = df[["time", "open", "high", "low", "close", "tick_volume", "spread"]]

    df.to_csv(path, index=False)
    log.info("%s %s %s: %d баров, %s — %s", exchange_id, symbol, timeframe,
             len(df), df.time.iloc[0], df.time.iloc[-1])
    return df


def _point_for(price: float) -> float:
    """Шаг цены под масштаб инструмента: у BTC это 0.1, у мелких альтов 1e-6."""
    if price >= 10_000:
        return 0.1
    if price >= 100:
        return 0.01
    if price >= 1:
        return 0.0001
    if price >= 0.01:
        return 1e-6
    return 1e-8
