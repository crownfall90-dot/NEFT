"""Когда откроется рынок по инструменту — чтобы не гадать."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import datetime as dt

import MetaTrader5 as mt5
from rich.console import Console

con = Console()
DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница",
        "суббота", "воскресенье"]

if not mt5.initialize():
    con.print(f"[red]MT5 недоступен: {mt5.last_error()}[/]")
    raise SystemExit(1)

acc = mt5.account_info()
now = dt.datetime.now()
con.print(f"\n[bold]{acc.login} @ {acc.server}[/]")
con.print(f"[dim]сейчас {now:%d.%m.%Y %H:%M} ({DAYS[now.weekday()]}), "
          f"время сервера UTC+3[/]\n")

for sym in [s.strip() for s in (sys.argv[1:] or ["EURUSD", "EURUSD+", "NAS100"])]:
    mt5.symbol_select(sym, True)
    i = mt5.symbol_info(sym)
    if i is None:
        con.print(f"  {sym:10s} [dim]нет на этом сервере[/]")
        continue
    t = mt5.symbol_info_tick(sym)
    if t and t.bid:
        age = (now - dt.datetime.fromtimestamp(t.time)).total_seconds()
        # Котировки могут транслироваться и в выходные, но ордера при этом
        # отклоняются с retcode=10018. Наличие тика — не признак открытого рынка.
        state = ("[green]котировки идут[/]" if age < 120
                 else f"[yellow]последний тик {age/60:.0f} мин назад[/]")
    else:
        state = "[red]нет котировок[/]"
    con.print(f"  {sym:10s} {state}")

# Форекс: открытие в понедельник 00:00 по серверу (UTC+3).
if now.weekday() >= 5 or (now.weekday() == 4 and now.hour >= 23):
    days_ahead = (7 - now.weekday()) % 7 or 7
    nxt = (now + dt.timedelta(days=days_ahead)).replace(
        hour=0, minute=5, second=0, microsecond=0)
    left = nxt - now
    con.print(f"\n[yellow]Выходные.[/] Форекс откроется "
              f"[bold]{nxt:%d.%m %H:%M}[/] — через "
              f"{left.days} д {left.seconds // 3600} ч\n")
else:
    con.print("\n[green]Рабочий день — рынок должен быть открыт.[/]\n")
mt5.shutdown()
