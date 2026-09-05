"""Загрузка 1m свечей BTCUSDT с публичного Binance API для рынка up/down 5m."""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

CACHE = Path(__file__).resolve().parents[1] / "data" / "updown"
BASE = "https://api.binance.com/api/v3/klines"


def _fetch(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    out: list = []
    cur = start_ms
    while cur < end_ms:
        r = requests.get(BASE, params={
            "symbol": symbol, "interval": interval,
            "startTime": cur, "endTime": end_ms, "limit": 1000,
        }, timeout=30)
        r.raise_for_status()
        chunk = r.json()
        if not chunk:
            break
        out.extend(chunk)
        nxt = chunk[-1][0] + 1
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.15)
    return out


def load(symbol: str = "BTCUSDT", days: int = 3, interval: str = "1m",
         end: pd.Timestamp | None = None) -> pd.DataFrame:
    """Свечи за `days` суток до `end` (UTC). Кэшируется на диск."""
    end_ts = pd.Timestamp.now("UTC").tz_localize(None) if end is None else pd.Timestamp(end)
    end_ts = end_ts.floor("min")
    start_ts = end_ts - pd.Timedelta(days=days)

    CACHE.mkdir(parents=True, exist_ok=True)
    tag = f"{symbol}_{interval}_{start_ts:%Y%m%d%H%M}_{end_ts:%Y%m%d%H%M}.csv"
    path = CACHE / tag
    if path.exists():
        return pd.read_csv(path, parse_dates=["time"])

    raw = _fetch(symbol, interval,
                 int(start_ts.timestamp() * 1000), int(end_ts.timestamp() * 1000))
    if not raw:
        raise RuntimeError(f"Binance вернул пусто для {symbol} {interval}")

    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore"])
    df["time"] = pd.to_datetime(df.open_time, unit="ms")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["tick_volume"] = df["trades"].astype(int)
    df["spread"] = 1
    df = df[["time", "open", "high", "low", "close", "volume", "tick_volume", "spread"]]
    df = df.sort_values("time").reset_index(drop=True)
    df.to_csv(path, index=False)
    return df


if __name__ == "__main__":
    d = load(days=3)
    print(f"{len(d)} баров: {d.time.iloc[0]} → {d.time.iloc[-1]}")
    print(d.tail(3).to_string(index=False))
