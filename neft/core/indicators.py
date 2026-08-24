"""Индикаторы. Всё считается векторно один раз перед прогоном."""
import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = df.close.shift(1)
    tr = pd.concat([
        df.high - df.low,
        (df.high - prev).abs(),
        (df.low - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def grouped_vwap(df: pd.DataFrame, group: pd.Series,
                 mask: pd.Series | None = None) -> pd.Series:
    """Сессионный VWAP без заглядывания вперёд: cum(tp·vol) / cum(vol)."""
    tp = (df.high + df.low + df.close) / 3.0
    vol = df.tick_volume.replace(0, np.nan)
    vol = vol.ffill().fillna(1.0)
    if mask is not None:
        tp = tp.where(mask)
        vol = vol.where(mask, 0.0)
    pv = (tp * vol).fillna(0.0)
    g = group.astype(str)
    return pv.groupby(g).cumsum() / vol.groupby(g).cumsum().replace(0, np.nan)


def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Heikin Ashi.

    ВАЖНО: ha_close — это среднее, а не реальная цена. Сигналы по нему считать
    можно, исполнять сделки по нему нельзя — иначе бэктест покажет прибыль,
    которой в реальности нет.
    """
    ha_close = (df.open + df.high + df.low + df.close) / 4

    ha_open = np.empty(len(df))
    ha_open[0] = (df.open.iloc[0] + df.close.iloc[0]) / 2
    o, c = ha_open, ha_close.to_numpy()
    for i in range(1, len(df)):
        o[i] = (o[i - 1] + c[i - 1]) / 2

    ha_open = pd.Series(ha_open, index=df.index)
    ha_high = pd.concat([df.high, ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([df.low, ha_open, ha_close], axis=1).min(axis=1)

    return pd.DataFrame({
        "ha_open": ha_open, "ha_high": ha_high,
        "ha_low": ha_low, "ha_close": ha_close,
    }, index=df.index)


def add_features(df: pd.DataFrame, ema_period: int = 100,
                 doji_body_pct: float = 0.10,
                 clean_wick_pct: float = 0.05,
                 vol_mode: str = "min",
                 vol_window: int = 2) -> pd.DataFrame:
    """Готовит все колонки, которые нужны скальпинг-стратегии."""
    out = df.copy()
    out = pd.concat([out, heikin_ashi(df)], axis=1)
    out["ema"] = ema(out.close, ema_period)

    rng = (out.ha_high - out.ha_low).replace(0, np.nan)
    body = (out.ha_close - out.ha_open).abs()

    out["ha_bull"] = out.ha_close > out.ha_open
    out["body_ratio"] = (body / rng).fillna(1.0)
    out["is_doji"] = out.body_ratio <= doji_body_pct

    # «Чистая» свеча по определению Heikin Ashi: у сильной бычьей нет нижнего
    # фитиля, у сильной медвежьей — верхнего.
    top = out.ha_high - out[["ha_open", "ha_close"]].max(axis=1)
    bot = out[["ha_open", "ha_close"]].min(axis=1) - out.ha_low
    out["top_wick_ratio"] = (top / rng).fillna(0.0)
    out["bot_wick_ratio"] = (bot / rng).fillna(0.0)
    out["clean_bull"] = out.ha_bull & (out.bot_wick_ratio <= clean_wick_pct)
    out["clean_bear"] = (~out.ha_bull) & (out.top_wick_ratio <= clean_wick_pct)

    # "High volume" в оригинале означает РАЗМЕР свечи, а не тиковый объём:
    # «the size of the doji has to be bigger than the size of the last one to
    # three candles… bigger than at least ONE of the last three».
    size = out.ha_high - out.ha_low
    out["size"] = size
    win = size.rolling(vol_window)
    out["size_ref"] = (win.min() if vol_mode == "min" else win.max()).shift(1)
    out["big_doji"] = size > out.size_ref
    out["high_volume"] = out.big_doji          # совместимость с прежним именем

    # Тиковый объём оставляем справочно — на форексе он не равен реальному.
    out["tickvol_ref"] = out.tick_volume.rolling(3).min().shift(1)
    out["tickvol_ok"] = out.tick_volume >= out.tickvol_ref

    # Маленькая doji перед сигнальной обесценивает сетап (правило автора).
    out["small_doji"] = out.is_doji & (size <= out.size_ref)

    return out
