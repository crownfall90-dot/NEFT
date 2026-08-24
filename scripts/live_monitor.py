"""Монитор стратегии в реальном времени. НИЧЕГО НЕ ТОРГУЕТ — только смотрит.

Показывает по каждому инструменту, на каком шаге чек-листа находится рынок
прямо сейчас: тренд по EMA, длина чистого отката, свеча doji, объём.

    python scripts/live_monitor.py --seconds 120
    python scripts/live_monitor.py --any-hour     # игнорировать Kill Zone
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import MetaTrader5 as mt5
import pandas as pd
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from neft.core.indicators import add_features

con = Console()
TF = mt5.TIMEFRAME_M1
SESSION = (16, 19)   # время сервера = 9-12 ET


def fetch(symbol: str, bars: int = 300) -> pd.DataFrame | None:
    r = mt5.copy_rates_from_pos(symbol, TF, 0, bars)
    if r is None or len(r) < 150:
        return None
    df = pd.DataFrame(r)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df[["time", "open", "high", "low", "close", "tick_volume", "spread"]]


def state(symbol: str, pullback_bars: int, any_hour: bool) -> dict:
    df = fetch(symbol)
    if df is None:
        return {"symbol": symbol, "err": "нет данных"}
    d = add_features(df, 100, 0.10, 0.05, "min")
    last = d.iloc[-1]          # текущий, ещё не закрытый бар
    closed = d.iloc[-2]        # последний закрытый — по нему судим

    tick = mt5.symbol_info_tick(symbol)
    hour = closed.time.hour
    in_session = any_hour or (SESSION[0] <= hour < SESSION[1])

    above = closed.close > closed.ema
    side = "BUY" if above else "SELL"

    # Сколько подряд чистых свечей отката прямо сейчас
    col = "clean_bear" if above else "clean_bull"
    run = 0
    for i in range(len(d) - 2, 0, -1):
        if bool(d.iloc[i][col]):
            run += 1
        else:
            break

    return {
        "symbol": symbol,
        "bid": tick.bid if tick else 0,
        "spread": (tick.ask - tick.bid) if tick else 0,
        "time": closed.time,
        "ema": closed.ema,
        "side": side,
        "dist_ema": (closed.close - closed.ema) / closed.ema * 100,
        "pullback": run,
        "pullback_ok": run >= pullback_bars,
        "doji": bool(closed.is_doji),
        "body": closed.body_ratio,
        # "Высокий объём" в стратегии — это РАЗМЕР свечи, не тиковый объём
        # (см. README): сравниваем size с size_ref, как это делает ScalpHA.
        "vol_ok": bool(closed.high_volume),
        "vol": round(float(closed["size"]), 5),
        "vol_ref": round(float(closed.size_ref), 5) if pd.notna(closed.size_ref) else 0.0,
        "in_session": in_session,
        "hour": hour,
    }


def render(rows: list[dict], tick_n: int, any_hour: bool) -> Group:
    t = Table(box=None, pad_edge=False, expand=False)
    for c in ("инструмент", "цена", "спред", "тренд", "до EMA",
              "откат", "doji", "размер свечи", "сессия", "статус"):
        t.add_column(c, justify="left" if c == "инструмент" else "right")

    for r in rows:
        if r.get("err"):
            t.add_row(r["symbol"], f"[red]{r['err']}[/]", *[""] * 8)
            continue

        checks = [r["in_session"], r["pullback_ok"], r["doji"], r["vol_ok"]]
        ready = all(checks)
        done = sum(checks)

        if ready:
            status = "[bold green]СЕТАП ГОТОВ[/]"
        elif r["in_session"]:
            status = f"[yellow]{done}/4[/]"
        else:
            status = "[dim]вне сессии[/]"

        side_c = "green" if r["side"] == "BUY" else "red"
        t.add_row(
            r["symbol"],
            f"{r['bid']:.3f}".rstrip("0").rstrip("."),
            f"{r['spread']:.3f}".rstrip("0").rstrip("."),
            f"[{side_c}]{r['side']}[/]",
            f"{r['dist_ema']:+.2f}%",
            (f"[green]{r['pullback']}[/]" if r["pullback_ok"]
             else f"[dim]{r['pullback']}[/]"),
            "[green]да[/]" if r["doji"] else f"[dim]{r['body']:.2f}[/]",
            (f"[green]{r['vol']:g}[/]" if r["vol_ok"]
             else f"[dim]{r['vol']:g}/{r['vol_ref']:g}[/]"),
            "[green]✓[/]" if r["in_session"] else f"[dim]{r['hour']:02d}ч[/]",
            status,
        )

    bar_time = rows[0].get("time") if rows and not rows[0].get("err") else None
    head = Text.assemble(
        ("НАБЛЮДЕНИЕ В РЕАЛЬНОМ ВРЕМЕНИ", "bold cyan"),
        ("  ·  ", "dim"),
        (f"обновление {tick_n}", "dim"),
        ("  ·  ", "dim"),
        (f"бар {bar_time:%H:%M}" if bar_time is not None else "", "dim"),
        ("  ·  ", "dim"),
        ("сессия игнорируется" if any_hour else f"Kill Zone {SESSION[0]}-{SESSION[1]} сервера",
         "yellow" if any_hour else "dim"),
    )
    legend = Text(
        "Чек-лист: сессия → чистый откат ≥2 свечей → doji → объём выше минимума из 3.\n"
        "Ордера не отправляются: это наблюдение.",
        style="dim",
    )
    return Group(head, Text(""), t, Text(""), legend)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="NAS100,DJ30,GER40,USOUSD,EURUSD+,XAUUSD+")
    p.add_argument("--seconds", type=int, default=0, help="0 = бесконечно")
    p.add_argument("--interval", type=float, default=3.0)
    p.add_argument("--pullback", type=int, default=2)
    p.add_argument("--any-hour", action="store_true")
    a = p.parse_args()

    if not mt5.initialize():
        con.print(f"[red]MT5 initialize failed: {mt5.last_error()}[/]")
        return
    symbols = [s.strip() for s in a.symbols.split(",")]
    for s in symbols:
        mt5.symbol_select(s, True)

    started = time.time()
    n = 0
    try:
        with Live(console=con, refresh_per_second=4, screen=False) as live:
            while True:
                n += 1
                rows = [state(s, a.pullback, a.any_hour) for s in symbols]
                live.update(render(rows, n, a.any_hour))
                if a.seconds and time.time() - started >= a.seconds:
                    break
                time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    finally:
        mt5.shutdown()
    con.print(f"\n[dim]Остановлено. Обновлений: {n}[/]")


if __name__ == "__main__":
    main()
