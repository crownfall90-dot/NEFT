"""Прогон NewsImpulse / NewsFade / NewsStraddle по всем CFD_SYMBOLS.

Синтетические M1-бары + High-event по первой валюте инструмента.
Без MT5 — чистая проверка логики сигналов.

    python scripts/test_news_pulse.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from neft.core.mt5_symbols import CFD_SYMBOLS
from neft.core.news import Event, currencies_for, events_for_instrument
from neft.core.portfolio import Portfolio
from neft.core.strategy import Bar
from neft.strategies.news_pulse import (
    EventTracker, NewsFade, NewsImpulse, NewsStraddle,
)

# Базовая цена и амплитуда импульса (~2% хода) — достаточно для ATR-триггера.
BASE_PX: dict[str, float] = {
    "NAS100": 20_000, "DJ30": 40_000, "GER40": 18_000, "FRA40": 7_500,
    "ES35": 11_000, "UK100": 8_000, "CHINA50": 12_000,
    "XAUUSD+": 2_400, "UKOUSD": 80, "USOUSD": 75,
    "EURUSD+": 1.10, "GBPUSD+": 1.30, "USDJPY+": 150.0,
    "AUDUSD+": 0.65, "NZDUSD+": 0.60, "USDCAD+": 1.35,
    "USDCHF+": 0.88, "EURJPY+": 165.0, "EURGBP+": 0.85, "GBPJPY+": 190.0,
}

GROUPS = {
    "index": ["NAS100", "DJ30", "GER40", "FRA40", "ES35", "UK100", "CHINA50"],
    "oil_metal": ["XAUUSD+", "UKOUSD", "USOUSD"],
    "fx": [
        "EURUSD+", "GBPUSD+", "USDJPY+", "AUDUSD+", "NZDUSD+",
        "USDCAD+", "USDCHF+", "EURJPY+", "EURGBP+", "GBPJPY+",
    ],
}


def synth_df(symbol: str, n: int = 80, impulse_i: int = 50) -> pd.DataFrame:
    px = BASE_PX.get(symbol, 100.0)
    # ~1.5% impulse body — гарантированно > 0.5·ATR после прогрева
    impulse = px * 0.015
    rows = []
    price = px
    t0 = pd.Timestamp("2026-08-26 12:00:00")
    for i in range(n):
        o = price
        if i == impulse_i:
            c = o + impulse
            h, l = c + impulse * 0.05, o - impulse * 0.02
        elif i == impulse_i + 1:
            c = o - impulse * 0.4
            h, l = o + impulse * 0.05, c - impulse * 0.02
        else:
            # мелкий шум для ATR
            step = px * 0.0003
            c = o + step
            h, l = max(o, c) + step, min(o, c) - step
        rows.append({
            "time": t0 + pd.Timedelta(minutes=i),
            "open": o, "high": h, "low": l, "close": c,
            "tick_volume": 100, "spread": 2,
        })
        price = c
    return pd.DataFrame(rows)


def make_event(df: pd.DataFrame, symbol: str, impulse_i: int = 50) -> Event:
    curs = currencies_for(symbol)
    assert curs, f"{symbol}: нет валют в INSTRUMENT_CURRENCIES"
    # бар-время сервера UTC+3 → event UTC = bar - 3h
    bar_t = df.iloc[impulse_i]["time"]
    ev_t = pd.Timestamp(bar_t) - pd.Timedelta(hours=3)
    return Event(
        time=ev_t, currency=curs[0], impact="High", title="Test Release",
        forecast="100", previous="90", actual="120",
    )


def bar_at(df: pd.DataFrame, i: int) -> Bar:
    r = df.iloc[i]
    return Bar(r.time, r.open, r.high, r.low, r.close, int(r.spread),
               index=i, volume=float(r.tick_volume))


def test_impulse(symbol: str) -> str:
    df = synth_df(symbol)
    ev = make_event(df, symbol)
    tr = EventTracker()
    s = NewsImpulse(symbol=symbol, tracker=tr, utc_offset_hours=3.0,
                    impulse_atr=0.5, post_window_min=15)
    s.set_events([ev])
    s.set_equity(1000)
    s.prepare(df)
    sig = s.on_bar(bar_at(df, 50), False)
    if sig is None:
        return "FAIL: no signal"
    if sig.side.value != "buy":
        return f"FAIL: side={sig.side}"
    if sig.entry_type != "market":
        return f"FAIL: entry_type={sig.entry_type}"
    if sig.sl is None or sig.tp is None or sig.volume <= 0:
        return "FAIL: bad sl/tp/vol"
    if ev.key not in tr.traded:
        return "FAIL: not marked traded"
    return "OK"


def test_fade(symbol: str) -> str:
    df = synth_df(symbol)
    ev = make_event(df, symbol)
    tr = EventTracker()
    s = NewsFade(symbol=symbol, tracker=tr, utc_offset_hours=3.0,
                 impulse_atr=0.5, post_window_min=15)
    s.set_events([ev])
    s.set_equity(1000)
    s.prepare(df)
    arm = s.on_bar(bar_at(df, 50), False)
    if arm is not None:
        return f"FAIL: arm should be None, got {arm.reason}"
    if ev.key not in s._spikes:
        return "FAIL: spike not armed"
    sig = s.on_bar(bar_at(df, 51), False)
    if sig is None:
        return "FAIL: no fade signal"
    if sig.side.value != "sell":
        return f"FAIL: side={sig.side} (want sell)"
    if sig.entry_type != "market":
        return f"FAIL: entry_type={sig.entry_type}"
    return "OK"


def test_straddle(symbol: str) -> str:
    df = synth_df(symbol)
    ev = make_event(df, symbol)
    tr = EventTracker()
    s = NewsStraddle(symbol=symbol, tracker=tr, utc_offset_hours=3.0,
                     pre_minutes=5, impulse_atr=0.5)
    s.set_events([ev])
    s.set_equity(1000)
    s.prepare(df)
    # 2 минуты до релиза по серверному времени (bar 48)
    sig = s.on_bar(bar_at(df, 48), False)
    if sig is None:
        return "FAIL: no straddle signal"
    if sig.entry_type != "stop" or sig.entry is None:
        return "FAIL: not a stop entry"
    oco = s.consume_oco()
    if oco is None or len(oco) != 2:
        return "FAIL: no OCO pair"
    buy, sell = oco
    if buy.side.value != "buy" or sell.side.value != "sell":
        return "FAIL: OCO sides"
    if buy.entry <= sell.entry:
        return f"FAIL: buy_entry {buy.entry} <= sell_entry {sell.entry}"
    if ev.key not in tr.armed:
        return "FAIL: not armed"
    return "OK"


def test_portfolio_priority(symbol: str) -> str:
    """На импульсном баре Impulse побеждает Fade; Straddle молчит после релиза."""
    df = synth_df(symbol)
    ev = make_event(df, symbol)
    tr = EventTracker()
    kw = dict(symbol=symbol, tracker=tr, utc_offset_hours=3.0, impulse_atr=0.5,
              post_window_min=15, pre_minutes=5)
    port = (Portfolio()
            .add(NewsImpulse(**kw), "Impulse")
            .add(NewsFade(**kw), "Fade")
            .add(NewsStraddle(**kw), "Straddle"))
    for slot in port.slots:
        slot.strategy.set_events([ev])
        slot.strategy.set_equity(1000)
    port.prepare(df)
    sig = port.on_bar(bar_at(df, 50), False)
    if sig is None:
        return "FAIL: no signal"
    if port._owner is None or port._owner.name != "Impulse":
        return f"FAIL: owner={port._owner.name if port._owner else None}"
    if "impulse" not in sig.reason:
        return f"FAIL: reason={sig.reason}"
    return "OK"


def test_currency_map(symbol: str) -> str:
    curs = currencies_for(symbol)
    if not curs:
        return "FAIL: empty currencies"
    # событие по первой валюте должно матчиться
    ev = Event(time=pd.Timestamp("2026-01-01"), currency=curs[0],
               impact="High", title="x")
    matched = events_for_instrument([ev], symbol)
    if not matched:
        return "FAIL: events_for_instrument empty"
    return f"OK ({','.join(curs)})"


def main() -> int:
    assert set(CFD_SYMBOLS) == set().union(*GROUPS.values()), \
        "GROUPS не покрывают CFD_SYMBOLS"

    results: list[tuple[str, str, str, str]] = []
    failed = 0

    print(f"CFD_SYMBOLS: {len(CFD_SYMBOLS)}")
    print("-" * 72)

    for group, symbols in GROUPS.items():
        print(f"\n=== {group.upper()} ({len(symbols)}) ===")
        for sym in symbols:
            row = {"symbol": sym}
            for name, fn in (
                ("currency", test_currency_map),
                ("impulse", test_impulse),
                ("fade", test_fade),
                ("straddle", test_straddle),
                ("priority", test_portfolio_priority),
            ):
                try:
                    status = fn(sym)
                except Exception as e:  # noqa: BLE001
                    status = f"EXC: {e}"
                ok = status.startswith("OK")
                if not ok:
                    failed += 1
                mark = "PASS" if ok else "FAIL"
                row[name] = status
                results.append((group, sym, name, status))
                print(f"  {sym:12} {name:10} {mark:4}  {status}")

    print("\n" + "=" * 72)
    total = len(results)
    print(f"Итого: {total - failed}/{total} ок, failed={failed}")
    if failed:
        print("\nПровалы:")
        for g, s, n, st in results:
            if not st.startswith("OK"):
                print(f"  [{g}] {s} / {n}: {st}")
        return 1
    print("Все режимы на всех FX / индексах / нефти — OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
