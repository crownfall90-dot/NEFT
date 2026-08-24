"""Ближайшие важные новости по нашим инструментам.

Ничего не торгует. Показывает, что реально стоит в календаре на неделю
и как это соотносится с нашими символами.

    python scripts/news_check.py
    python scripts/news_check.py --impacts High,Medium
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from rich.console import Console
from rich.table import Table

from neft.core.news import INSTRUMENT_CURRENCIES, NewsGate, currencies_for, load_calendar
from neft.core.routing import CRYPTO_ROUTES

con = Console()

MT5_SYMBOLS = list(INSTRUMENT_CURRENCIES)
CRYPTO_SYMBOLS = list(CRYPTO_ROUTES)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--impacts", default="High")
    p.add_argument("--refresh", action="store_true")
    a = p.parse_args()
    impacts = tuple(x.strip() for x in a.impacts.split(","))

    events = load_calendar(refresh=a.refresh)
    con.print(f"[dim]Источник: ForexFactory, {len(events)} событий на текущую "
              f"неделю (только вперёд от старта недели — не архив)[/]\n")

    gate = NewsGate(impacts=impacts, events=events)
    now = pd.Timestamp.now("UTC").tz_localize(None)
    con.print(f"[bold]сейчас (UTC): {now:%a %d.%m %H:%M}[/]\n")

    t = Table(title=f"события уровня {', '.join(impacts)} на неделю",
              box=None, pad_edge=False, title_style="bold cyan")
    for c in ("время UTC", "валюта", "событие"):
        t.add_column(c, justify="left")
    shown = [e for e in events if e.impact in impacts]
    for e in shown:
        mark = "[dim](прошло)[/] " if e.time < now else ""
        t.add_row(f"{mark}{e.time:%a %d.%m %H:%M}", e.currency, e.title)
    con.print(t)
    if not shown:
        con.print("  [dim]на этой неделе таких событий нет[/]")

    con.print(f"\n[bold]по инструментам (ближайшие {impacts[0]}-события):[/]")
    t2 = Table(box=None, pad_edge=False)
    for c in ("инструмент", "валюты", "ближайшее событие"):
        t2.add_column(c, justify="left")
    for sym in MT5_SYMBOLS + CRYPTO_SYMBOLS:
        curs = currencies_for(sym)
        up = gate.upcoming(sym, now, horizon_hours=168)
        label = sym.replace("/USDT:USDT", "")
        if up:
            e = up[0]
            t2.add_row(label, ",".join(curs), f"{e.time:%a %d.%m %H:%M} {e.currency} {e.title}")
        else:
            t2.add_row(label, ",".join(curs), "[dim]нет на этой неделе[/]")
    con.print(t2)
