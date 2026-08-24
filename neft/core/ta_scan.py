"""Контекст теханализа для сканера и панели.

Собирает проверенные на OHLCV признаки, которые часто используют в скальпинге
и intraday: RSI, MACD, Bollinger, структура свингов, импульс объёма, тренд EMA.
Не заменяет стратегии — только усиливает score и текст «почему рядом».
"""
from __future__ import annotations

import pandas as pd

from neft.core.indicators import atr, ema
from neft.core.structure import structure_state, swings


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def macd_hist(series: pd.Series, fast: int = 12, slow: int = 26,
              signal: int = 9) -> pd.Series:
    line = ema(series, fast) - ema(series, slow)
    sig = ema(line, signal)
    return line - sig


def enrich_ta(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """Добавить колонки TA, если их ещё нет (кэш на том же df)."""
    if df is None or len(df) < 30:
        return df
    out = df
    if "rsi14" not in out.columns:
        out = out.copy()
        out["rsi14"] = rsi(out.close, 14)
    if "macd_hist" not in out.columns:
        if out is df:
            out = out.copy()
        out["macd_hist"] = macd_hist(out.close)
    if "atr" not in out.columns:
        if out is df:
            out = out.copy()
        out["atr"] = atr(out, 14)
    if "bb_mid" not in out.columns:
        if out is df:
            out = out.copy()
        mid = out.close.rolling(20, min_periods=10).mean()
        std = out.close.rolling(20, min_periods=10).std()
        out["bb_mid"] = mid
        out["bb_upper"] = mid + 2 * std
        out["bb_lower"] = mid - 2 * std
        out["bb_width"] = (out["bb_upper"] - out["bb_lower"]) / mid.replace(0, pd.NA)
    if "last_high" not in out.columns:
        if out is df:
            out = out.copy()
        sw = swings(out, left=2, right=2)
        out["last_high"] = sw.last_high
        out["last_low"] = sw.last_low
        out["mkt_structure"] = structure_state(out, sw)
    if "vol_ratio" not in out.columns:
        if out is df:
            out = out.copy()
        vol = out.tick_volume if "tick_volume" in out.columns else out.get("volume")
        if vol is not None:
            ref = vol.rolling(20, min_periods=5).mean().shift(1)
            out["vol_ratio"] = (vol / ref.replace(0, pd.NA)).fillna(1.0)
        else:
            out["vol_ratio"] = 1.0
    if "ema_slope" not in out.columns and "ema" in out.columns:
        if out is df:
            out = out.copy()
        out["ema_slope"] = out.ema.pct_change(5)
    return out


def _num(row, name: str) -> float | None:
    v = getattr(row, name, None)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def ta_confluence(df: pd.DataFrame | None, back: int = 2) -> tuple[float, list[str]]:
    """Бонус к score сканера + короткие подсказки для панели."""
    df = enrich_ta(df)
    if df is None or len(df) < back + 5:
        return 0.0, []
    row = df.iloc[-back]
    prev = df.iloc[-back - 1] if len(df) > back + 1 else row
    bits: list[str] = []
    score = 0.0

    rsi_v = _num(row, "rsi14")
    if rsi_v is not None:
        if rsi_v <= 32:
            score += 0.55
            bits.append(f"RSI {rsi_v:.0f} перепродан")
        elif rsi_v >= 68:
            score += 0.55
            bits.append(f"RSI {rsi_v:.0f} перекуплен")
        elif 45 <= rsi_v <= 55:
            score += 0.25
            bits.append(f"RSI {rsi_v:.0f} баланс")

    macd_now = _num(row, "macd_hist")
    macd_prev = _num(prev, "macd_hist")
    if macd_now is not None and macd_prev is not None:
        if macd_prev <= 0 < macd_now:
            score += 0.65
            bits.append("MACD бычий импульс")
        elif macd_prev >= 0 > macd_now:
            score += 0.65
            bits.append("MACD медвежий импульс")
        elif macd_now > 0 and macd_now > macd_prev:
            score += 0.25
            bits.append("MACD растёт")
        elif macd_now < 0 and macd_now < macd_prev:
            score += 0.25
            bits.append("MACD падает")

    lh, ll = _num(row, "last_high"), _num(row, "last_low")
    close = float(row.close)
    if lh is not None and ll is not None and lh > ll:
        rng = lh - ll
        if close > lh:
            score += 0.55
            bits.append("пробой свинга вверх")
        elif close < ll:
            score += 0.55
            bits.append("пробой свинга вниз")
        elif (lh - close) / rng < 0.1:
            score += 0.45
            bits.append("у сопротивления")
        elif (close - ll) / rng < 0.1:
            score += 0.45
            bits.append("у поддержки")

    struct = int(getattr(row, "mkt_structure", 0) or 0)
    if struct == 1:
        score += 0.35
        bits.append("структура бычья HH/HL")
    elif struct == -1:
        score += 0.35
        bits.append("структура медвежья LH/LL")

    bb_w = _num(row, "bb_width")
    if bb_w is not None and bb_w < 0.025:
        score += 0.4
        bits.append("сжатие Bollinger")

    vr = _num(row, "vol_ratio")
    if vr is not None and vr >= 1.6:
        score += 0.45
        bits.append(f"объём ×{vr:.1f}")

    slope = _num(row, "ema_slope")
    if slope is not None and abs(slope) > 0.0015:
        score += 0.3
        bits.append("тренд EMA " + ("↑" if slope > 0 else "↓"))

    atr_v = _num(row, "atr")
    if atr_v and close:
        body = abs(float(row.close) - float(row.open))
        if body > atr_v * 1.2:
            score += 0.35
            bits.append("импульсная свеча")

    return min(score, 2.2), bits[:4]


def ta_summary(df: pd.DataFrame | None) -> dict:
    """Сводка для вкладки «анализ монеты»."""
    bonus, hints = ta_confluence(df)
    df = enrich_ta(df)
    if df is None or len(df) < 5:
        return {"score": 0.0, "hints": [], "rsi": None, "structure": 0}
    row = df.iloc[-2]
    rsi_v = _num(row, "rsi14")
    struct = int(getattr(row, "mkt_structure", 0) or 0)
    struct_txt = {1: "бычья", -1: "медвежья"}.get(struct, "боковик")
    trend = "нейтраль"
    if "ema" in df.columns and pd.notna(row.ema):
        trend = "выше EMA" if float(row.close) > float(row.ema) else "ниже EMA"
    return {
        "score": round(bonus, 2),
        "hints": hints,
        "rsi": round(rsi_v, 1) if rsi_v is not None else None,
        "structure": struct,
        "structure_txt": struct_txt,
        "trend": trend,
    }


_FIB_LEVELS = (
    (0.0, "0%"),
    (0.236, "23.6%"),
    (0.382, "38.2%"),
    (0.5, "50%"),
    (0.618, "61.8%"),
    (0.786, "78.6%"),
    (1.0, "100%"),
)


def fib_levels(df: pd.DataFrame | None) -> list[dict]:
    """Уровни Fibonacci по последнему подтверждённому свингу high/low."""
    df = enrich_ta(df)
    if df is None or len(df) < 10:
        return []
    row = df.iloc[-2]
    hi, lo = _num(row, "last_high"), _num(row, "last_low")
    if hi is None or lo is None or hi <= lo:
        return []
    rng = hi - lo
    colors = {
        0.0: "#8a7aaa", 1.0: "#8a7aaa",
        0.236: "#6b6080", 0.382: "#756a8f", 0.5: "#807599",
        0.618: "#8a80a3", 0.786: "#958bad",
    }
    out: list[dict] = []
    for ratio, label in _FIB_LEVELS:
        px = lo + ratio * rng
        out.append({
            "price": round(px, 10),
            "title": f"Fib {label}",
            "color": colors.get(ratio, "#8a7aaa"),
            "style": 2,
            "role": "fib",
        })
    return out


def ta_chart_series(df: pd.DataFrame | None, n: int = 120) -> dict:
    """Серии RSI/MACD и Fib для графика панели."""
    df = enrich_ta(df)
    if df is None or len(df) < 30:
        return {"fib": [], "rsi": [], "macd": []}
    tail = df.tail(n)
    times = (pd.to_datetime(tail.time, utc=True)
             .to_numpy(dtype="datetime64[s]").astype("int64"))
    rsi_pts, macd_pts = [], []
    if "rsi14" in tail.columns:
        for t, v in zip(times, tail.rsi14.to_numpy(dtype=float)):
            if v == v:
                rsi_pts.append({"time": int(t), "value": round(float(v), 2)})
    if "macd_hist" in tail.columns:
        for t, v in zip(times, tail.macd_hist.to_numpy(dtype=float)):
            if v == v:
                macd_pts.append({"time": int(t), "value": round(float(v), 8)})
    return {"fib": fib_levels(df), "rsi": rsi_pts, "macd": macd_pts}
