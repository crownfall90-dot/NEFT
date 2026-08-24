"""Сканер вселенной: не привязываемся к одной монете.

На каждом закрытом баре смотрим, где сейчас живой сетап наших стратегий
(HSS / London S/R / Flow). Торгуем только топ-K. BTC и ENA в бан — не сканируем.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from neft.core.strategy import Bar
from neft.core.ta_scan import enrich_ta, ta_chart_series, ta_confluence, ta_summary


@dataclass
class Hit:
    symbol: str
    tf: str
    strategy: str
    score: float
    why: str


def _row(df: pd.DataFrame, back: int = 2):
    if df is None or len(df) < back + 5:
        return None
    return df.iloc[-back]


def score_hss(df: pd.DataFrame) -> tuple[float, str]:
    row = _row(df)
    if row is None or "ema" not in df.columns:
        return 0.0, ""
    bits = []
    s = 0.0
    above = bool(row.close > row.ema)
    bits.append("выше EMA" if above else "ниже EMA")
    s += 0.5
    if getattr(row, "bars_since_cross", 0) >= 20:
        s += 0.5
        bits.append("структура")
    if bool(getattr(row, "is_doji", False)) and bool(getattr(row, "big_doji", False)):
        s += 3.0
        bits.append("объёмная doji")
    elif bool(getattr(row, "clean_bear", False)) and above:
        s += 1.2
        bits.append("откат вниз")
    elif bool(getattr(row, "clean_bull", False)) and not above:
        s += 1.2
        bits.append("откат вверх")
    return s, ", ".join(bits)


def score_flow(df: pd.DataFrame) -> tuple[float, str]:
    row = _row(df)
    if row is None or "atr" not in df.columns:
        return 0.0, ""
    bits = []
    s = 0.0
    regime = str(getattr(row, "regime", "mixed"))
    bits.append(regime)
    if regime == "trend":
        s += 0.6
    atr = float(row.atr) if pd.notna(row.atr) else 0.0
    vwap = getattr(row, "ny_vwap", float("nan"))
    if atr > 0 and pd.notna(vwap):
        dist = abs(float(row.close) - float(vwap)) / atr
        if dist < 0.6:
            s += 1.5
            bits.append("у NY VWAP")
        elif dist < 1.2:
            s += 0.6
            bits.append("рядом VWAP")
    if bool(getattr(row, "orb_ready", False)):
        s += 0.4
        bits.append("ORB готов")
        oh, ol = getattr(row, "orb_high", None), getattr(row, "orb_low", None)
        if oh is not None and ol is not None and pd.notna(oh):
            if row.close > oh or row.close < ol:
                s += 1.5
                bits.append("за диапазоном ORB")
    if bool(getattr(row, "asia_ready", False)):
        ah = getattr(row, "asia_high", None)
        if ah is not None and pd.notna(ah) and row.high >= ah:
            s += 1.2
            bits.append("снятие Азии")
    return s, ", ".join(bits)


def score_squeeze(df: pd.DataFrame) -> tuple[float, str]:
    row = _row(df)
    if row is None or "last_high" not in getattr(df, "columns", []):
        return 0.0, ""
    if not pd.notna(row.last_high) or not pd.notna(row.last_low):
        return 0.3, "свинги ещё не собрались"
    rng = abs(float(row.last_high) - float(row.last_low)) or 1e-9
    near = min(abs(row.close - row.last_high), abs(row.close - row.last_low)) / rng
    bits = ["треугольник"]
    s = 0.5
    if near < 0.12:
        s += 1.6
        bits.append("у границы")
    elif near < 0.28:
        s += 0.7
        bits.append("сжимается")
    return s, ", ".join(bits)


def score_breakout(df: pd.DataFrame) -> tuple[float, str]:
    row = _row(df)
    if row is None:
        return 0.0, ""
    hour = int(pd.Timestamp(row.time).hour)
    bits = [f"{hour}h"]
    s = 0.2
    if 16 <= hour < 18:
        s += 0.8
        bits.append("окно пробоя")
    hi = getattr(row, "last_high", None)
    lo = getattr(row, "last_low", None)
    if hi is not None and lo is not None and pd.notna(hi) and pd.notna(lo):
        if row.close > hi or row.close < lo:
            s += 1.6
            bits.append("за диапазоном")
        else:
            s += 0.4
            bits.append("в боксе")
    return s, ", ".join(bits)


def score_lsr(df: pd.DataFrame, ny=(16, 23)) -> tuple[float, str]:
    row = _row(df)
    if row is None:
        return 0.0, ""
    hour = int(pd.Timestamp(row.time).hour)
    if not (ny[0] <= hour < ny[1]):
        return 0.2, f"час {hour} вне NY"
    bits = [f"NY {hour}h"]
    s = 0.8
    if "last_high" in df.columns and pd.notna(row.last_high):
        rng = abs(float(row.last_high) - float(row.last_low)) or 1e-9
        near = min(abs(row.close - row.last_high), abs(row.close - row.last_low)) / rng
        if near < 0.15:
            s += 1.8
            bits.append("у свинга")
        elif near < 0.35:
            s += 0.7
            bits.append("к уровню")
    return s, ", ".join(bits)


def probe_signal(strat, df: pd.DataFrame) -> bool:
    """Есть ли вход на последнем закрытом баре. Состояние стратегии портится —
    вызывать на копии/после prepare того же кадра, что и сканер."""
    if df is None or len(df) < 50:
        return False
    i = len(df) - 2
    row = df.iloc[i]
    bar = Bar(row.time, row.open, row.high, row.low, row.close,
              int(getattr(row, "spread", 1) or 1), index=i,
              volume=float(getattr(row, "tick_volume", 0) or 0))
    try:
        return strat.on_bar(bar, in_position=False) is not None
    except Exception:  # noqa: BLE001
        return False


def score_watcher(watcher) -> list[Hit]:
    hits: list[Hit] = []
    strat = watcher.strat
    slots = getattr(strat, "slots", None)
    items = slots if slots else [type("S", (), {"name": strat.name, "strategy": strat})()]
    for slot in items:
        if not getattr(slot, "enabled", True):
            continue
        inner = slot.strategy
        name = getattr(slot, "name", inner.name)
        df = getattr(inner, "df", None)
        if df is None:
            df = getattr(watcher, "df", None)
        df = enrich_ta(df)
        if "HSS" in name or inner.name == "scalp_ha":
            sc, why = score_hss(df)
            kind = "HSS"
        elif "Flow" in name or inner.name == "session_flow":
            sc, why = score_flow(df)
            kind = "Flow"
        elif "London" in name or inner.name == "london_sr":
            sc, why = score_lsr(df)
            kind = "London S/R"
        elif "Squeeze" in name or inner.name == "squeeze":
            sc, why = score_squeeze(df)
            kind = "Squeeze"
        elif "Breakout" in name or inner.name == "london_breakout":
            sc, why = score_breakout(df)
            kind = "Breakout"
        else:
            continue
        if sc <= 0:
            continue
        bonus, ta_bits = ta_confluence(df)
        if bonus > 0:
            sc += bonus
            if ta_bits:
                why = (why + ", " if why else "") + ", ".join(ta_bits[:2])
        hits.append(Hit(watcher.symbol, watcher.tf, kind, sc, why))
    return hits


# HSS и London S/R на истории чаще закрывают в плюс, чем 5m-скальп.
# Доходность не цель: сначала качество сетапа и винрейт.
_KIND_BIAS = {"HSS": 0.45, "London S/R": 0.35, "Flow": 0.0,
             "Squeeze": 0.15, "Breakout": 0.1}


def rank_hits(hits: list[Hit], top_k: int = 2, min_score: float = 1.2) -> list[Hit]:
    def key(h: Hit) -> float:
        return h.score + _KIND_BIAS.get(h.strategy, 0.0)
    ranked = sorted((h for h in hits if h.score >= min_score),
                    key=key, reverse=True)
    seen: set[str] = set()
    out: list[Hit] = []
    for h in ranked:
        if h.symbol in seen:
            continue
        seen.add(h.symbol)
        out.append(h)
        if len(out) >= top_k:
            break
    return out


def _ts(t) -> int:
    ts = pd.Timestamp(t)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.timestamp())


def _num(row, name: str) -> float | None:
    v = getattr(row, name, None)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _hline(price: float | None, title: str, color: str, style: int = 2,
           role: str = "scan") -> dict | None:
    if price is None:
        return None
    return {"price": float(price), "title": title, "color": color,
            "style": style, "role": role}


def _path(df: pd.DataFrame | None, col: str, n: int = 90) -> list[dict]:
    """Линия индикатора для графика.

    Горячий путь: зовётся на каждого наблюдателя каждый круг. iterrows()
    здесь создавал объект Series на строку — при 69 наблюдателях это тысячи
    объектов на пустом месте. Тянем колонки массивами.
    """
    if df is None or col not in getattr(df, "columns", []):
        return []
    tail = df.tail(n)
    vals = tail[col].to_numpy(dtype=float)
    # Единицу указываем явно: разрешение datetime64 в pandas менялось,
    # и неявное деление молча даёт времена не той величины.
    times = (pd.to_datetime(tail.time, utc=True)
             .to_numpy(dtype="datetime64[s]").astype("int64"))
    return [{"time": int(t), "value": float(v)}
            for t, v in zip(times, vals) if v == v]  # NaN != NaN


def _wait_hss(why: str) -> str:
    if "объёмная doji" in why:
        return "HSS: doji есть — ждёт вход по правилу (стоп за фитилём)"
    if "откат вниз" in why:
        return "HSS: откат к EMA сверху — ждёт объёмную doji вниз"
    if "откат вверх" in why:
        return "HSS: откат к EMA снизу — ждёт объёмную doji вверх"
    if "выше EMA" in why:
        return "HSS: выше EMA — ждёт откат и объёмную doji"
    if "ниже EMA" in why:
        return "HSS: ниже EMA — ждёт откат вверх и объёмную doji"
    return "HSS: ждёт структуру у EMA и объёмную doji"


def _wait_lsr(why: str) -> str:
    if "вне NY" in why:
        return "London: ждёт окно NY 16–23 UTC и цену у свинга / зоны"
    if "у свинга" in why:
        return "London: цена у свинга — ждёт слом структуры на касании зоны"
    if "к уровню" in why:
        return "London: тянется к уровню — ждёт касание хая/лоу Лондона"
    return "London: ждёт возврат к зоне Лондона в NY"


def _wait_flow(why: str) -> str:
    bits = []
    if "ORB готов" in why and "за диапазоном" not in why:
        bits.append("ждёт выход за ORB")
    elif "за диапазоном ORB" in why:
        bits.append("за ORB — ждёт сетап Flow (follow / failed)")
    elif "ORB" not in why:
        bits.append("ORB ещё строится")
    if "у NY VWAP" in why or "рядом VWAP" in why:
        bits.append("у VWAP — место входа Flow")
    else:
        bits.append("ждёт цену у NY VWAP")
    if "снятие Азии" in why:
        bits.append("Азия снята — ждёт подтверждение")
    return "Flow: " + "; ".join(bits)


def _wait_squeeze(why: str) -> str:
    if "у границы" in why:
        return "Squeeze: у границы треугольника — ждёт пробой линии и свинга"
    if "сжимается" in why:
        return "Squeeze: сжимается — ждёт касание границы, вход не по касанию"
    if "не собрались" in why:
        return "Squeeze: свинги ещё копятся — треугольника нет"
    return "Squeeze: ждёт сжатие и пробой"


def _wait_breakout(why: str) -> str:
    if "окно пробоя" in why and "за диапазоном" in why:
        return "Пробой: цена за боксом в окне 16–18 UTC — ждёт вход"
    if "окно пробоя" in why:
        return "Пробой: окно 16–18 UTC — ждёт выход свечи из бокса Лондона"
    if "в боксе" in why:
        return "Пробой: в боксе — ждёт 16:30–18 UTC и пробой"
    return "Пробой: ждёт окно 16:30–18 UTC и выход из бокса"


def _squeeze_edges(inner, i: int) -> list[dict]:
    out = []
    try:
        highs = inner._taps("swing_high", i)
        lows = inner._taps("swing_low", i)
    except Exception:  # noqa: BLE001
        return out
    if len(highs) >= 2:
        upper = inner._line(highs, i)
        out.append(_hline(upper, "сжат. верх · пробой вверх", "#9b7ed9", 0,
                           role="entry_candidate"))
    if len(lows) >= 2:
        lower = inner._line(lows, i)
        out.append(_hline(lower, "сжат. низ · пробой вниз", "#9b7ed9", 0,
                           role="entry_candidate"))
    return [x for x in out if x]


def scan_overlay(watcher) -> dict:
    """Уровни и «чего ждёт» для графика. Не ставит ордера.

    На паре обычно висит 4-5 стратегий разом (HSS + London S/R + Flow +
    Squeeze + Breakout) — если рисовать линии всех сразу, график превращается
    в нечитаемую кашу из полутора десятков подписей друг на друге. Каждая
    линия и путь помечается тем, чья это стратегия (_kind); в конце
    оставляем только линии стратегии с максимальным score — она реально
    ближе всех к сигналу, остальные просто занимают экран.
    """
    lines: list[dict] = []
    paths: list[dict] = []
    waits: list[str] = []
    hits_out: list[dict] = []
    strat = getattr(watcher, "strat", None)
    slots = getattr(strat, "slots", None) if strat is not None else None
    items = slots if slots else []
    seen_title: set[str] = set()
    cur_kind = ""

    def add(line: dict | None) -> None:
        if not line or line["title"] in seen_title:
            return
        seen_title.add(line["title"])
        line["_kind"] = cur_kind
        lines.append(line)

    for slot in items:
        if not getattr(slot, "enabled", True):
            continue
        inner = slot.strategy
        name = getattr(slot, "name", inner.name)
        df = getattr(inner, "df", None)
        if df is None:
            df = getattr(watcher, "df", None)
        row = _row(df)
        if row is None:
            continue
        i = len(df) - 2
        if "HSS" in name or inner.name == "scalp_ha":
            cur_kind = "HSS"
            sc, why = score_hss(df)
            waits.append((cur_kind, _wait_hss(why)))
            hits_out.append({"strategy": "HSS", "score": round(sc, 2), "why": why})
            ema = _num(row, "ema")
            add(_hline(ema, "EMA 100 · HSS", "#c4a574", 0,
                       role="entry_candidate"))
            pts = _path(df, "ema")
            if pts:
                paths.append({"id": "ema", "title": "EMA 100", "color": "#c4a574",
                              "style": 0, "points": pts, "_kind": cur_kind})
        elif "Flow" in name or inner.name == "session_flow":
            cur_kind = "Flow"
            sc, why = score_flow(df)
            waits.append((cur_kind, _wait_flow(why)))
            hits_out.append({"strategy": "Flow", "score": round(sc, 2), "why": why})
            # Не больше трёх ориентиров: VWAP + ближайший ORB. Азия — только
            # если сканер сам про неё пишет, иначе правая шкала забита.
            add(_hline(_num(row, "ny_vwap"), "NY VWAP", "#d4a24a", 0,
                       role="entry_candidate"))
            add(_hline(_num(row, "orb_high"), "ORB верх", "#d36b6b", 2,
                       role="entry_candidate"))
            add(_hline(_num(row, "orb_low"), "ORB низ", "#6fbf8a", 2,
                       role="entry_candidate"))
            if "Азия" in why or "азия" in why:
                add(_hline(_num(row, "asia_high"), "Азия high", "#6b8ad3", 2))
                add(_hline(_num(row, "asia_low"), "Азия low", "#6b8ad3", 2))
            pts = _path(df, "ny_vwap")
            if pts:
                paths.append({"id": "vwap", "title": "NY VWAP", "color": "#d4a24a",
                              "style": 2, "points": pts, "_kind": cur_kind})
        elif "London" in name or inner.name == "london_sr":
            cur_kind = "London S/R"
            sc, why = score_lsr(df)
            waits.append((cur_kind, _wait_lsr(why)))
            hits_out.append({"strategy": "London S/R", "score": round(sc, 2), "why": why})
            day = getattr(row, "day", None) or pd.Timestamp(row.time).date()
            z = (getattr(inner, "zones", None) or {}).get(day)
            if z is None:
                add(_hline(_num(row, "last_high"), "свинг high", "#c33636", 2,
                           role="entry_candidate"))
                add(_hline(_num(row, "last_low"), "свинг low", "#1baf7a", 2,
                           role="entry_candidate"))
            if z is not None:
                add(_hline(float(getattr(z, "high_zone_bot", z.high)),
                           "Лондон high", "#c33636", 3, role="entry_candidate"))
                add(_hline(float(getattr(z, "low_zone_top", z.low)),
                           "Лондон low", "#1baf7a", 3, role="entry_candidate"))
        elif "Squeeze" in name or inner.name == "squeeze":
            cur_kind = "Squeeze"
            sc, why = score_squeeze(df)
            waits.append((cur_kind, _wait_squeeze(why)))
            hits_out.append({"strategy": "Squeeze", "score": round(sc, 2), "why": why})
            edges = _squeeze_edges(inner, i)
            if edges:
                for edge in edges:
                    add(edge)
            else:
                add(_hline(_num(row, "last_high"), "свинг high", "#9b7ed9", 2,
                           role="entry_candidate"))
                add(_hline(_num(row, "last_low"), "свинг low", "#9b7ed9", 2,
                           role="entry_candidate"))
        elif "Breakout" in name or inner.name == "london_breakout":
            cur_kind = "Breakout"
            sc, why = score_breakout(df)
            waits.append((cur_kind, _wait_breakout(why)))
            hits_out.append({"strategy": "Breakout", "score": round(sc, 2), "why": why})
            day = getattr(row, "day", None) or pd.Timestamp(row.time).date()
            z = (getattr(inner, "zones", None) or {}).get(day)
            if z is not None:
                add(_hline(float(z.high), "бокс high", "#5a8aaa", 0,
                           role="entry_candidate"))
                add(_hline(float(z.low), "бокс low", "#5a8aaa", 0,
                           role="entry_candidate"))
            else:
                add(_hline(_num(row, "last_high"), "бокс high", "#5a8aaa", 2,
                           role="entry_candidate"))
                add(_hline(_num(row, "last_low"), "бокс low", "#5a8aaa", 2,
                           role="entry_candidate"))

    hits_out.sort(key=lambda x: -x["score"])
    top_kind = hits_out[0]["strategy"] if hits_out else ""
    lines = [l for l in lines if l.get("_kind") == top_kind]
    paths = [p for p in paths if p.get("_kind") == top_kind]
    base_df = enrich_ta(getattr(watcher, "df", None))
    close = None
    if base_df is not None and len(base_df):
        try:
            close = float(base_df.close.iloc[-1])
        except (AttributeError, IndexError, ValueError, TypeError):
            close = None

    def _near(a: float, b: float) -> bool:
        return abs(a - b) <= max(abs(b) * 0.0008, 1e-8)

    # Одна «ждём здесь» + максимум один соседний контекст. Иначе на шкале
    # слипаются ORB / VWAP / Азия / EMA с одной и той же пары.
    entry = None
    candidates = [l for l in lines if l.get("role") == "entry_candidate"]
    if candidates and close is not None:
        entry = min(candidates, key=lambda l: abs(l["price"] - close))
        entry["role"] = "entry"
        entry["title"] = "→ ждём: " + entry["title"]
    ctx: list[dict] = []
    if entry is not None and close is not None:
        rest = [l for l in lines if l is not entry]
        # Второй уровень — следующий ближайший, но не дубль цены.
        rest.sort(key=lambda l: abs(l["price"] - close))
        for l in rest:
            if _near(l["price"], entry["price"]):
                continue
            l["role"] = "scan"
            ctx.append(l)
            break
        lines = [entry] + ctx
    else:
        for l in lines:
            if l.get("role") == "entry_candidate":
                l["role"] = "scan"
        if close is not None and lines:
            lines.sort(key=lambda l: abs(l["price"] - close))
            lines = lines[:2]
        else:
            lines = lines[:2]
    for l in lines:
        l.pop("_kind", None)
    for p in paths:
        p.pop("_kind", None)
    wait_top = [text for kind, text in waits if kind == top_kind]
    return {
        "lines": lines[:3],
        "paths": paths[:1],
        "wait": wait_top,
        "hits": hits_out[:4],
        "lead": top_kind,
        "ta": ta_summary(base_df),
        "ta_chart": ta_chart_series(base_df),
    }
