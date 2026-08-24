"""Предполётная проверка перед отправкой первого ордера.

Ничего не торгует. Проверяет всё, что способно сломать исполнение:
подключение, тип счёта, разрешение на алготрейдинг, доступность и торгуемость
символов, режимы заполнения, шаг лота, свободную маржу и настройки риска.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import MetaTrader5 as mt5
from rich.console import Console
from rich.table import Table

from neft.core.config import settings

con = Console()

# Маска symbol_info.filling_mode использует SYMBOL_FILLING_* (1, 2),
# а не ORDER_FILLING_* (0, 1, 2) — их легко перепутать.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2
TRADE_MODE = {0: "DEMO", 1: "CONTEST", 2: "REAL"}

# Основной набор — инструменты Bybit. На демо MetaQuotes их нет, поэтому
# при отсутствии подбираем эквиваленты, реально доступные на сервере.
WANT = ["NAS100", "DJ30", "XAUUSD+", "EURUSD+", "USDJPY+", "USDCAD+"]
FALLBACK = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "US30", "USTECH100M",
            "NASA", "USDCAD"]

problems, warnings = [], []


def check(ok, name, detail="", warn=False):
    mark = "[green]✓[/]" if ok else ("[yellow]![/]" if warn else "[red]✗[/]")
    # Пояснение имеет смысл только когда проверка не прошла.
    con.print(f"  {mark} {name}" + (f"  [dim]{detail}[/]" if detail and not ok else ""))
    if not ok:
        (warnings if warn else problems).append(f"{name}: {detail}")
    return ok


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--demo", action="store_true",
                   help="использовать учётные данные MT5_DEMO_* (нужен --force-login)")
    p.add_argument("--force-login", action="store_true",
                   help="войти в счёт из .env, а не использовать открытый в терминале")
    a = p.parse_args()

    con.print("\n[bold cyan]ПРЕДПОЛЁТНАЯ ПРОВЕРКА[/]\n")

    kwargs = settings.mt5_kwargs(demo=a.demo, force_login=a.force_login)
    if not mt5.initialize(**kwargs):
        con.print(f"[red]MT5 не инициализирован: {mt5.last_error()}[/]")
        raise SystemExit(1)

    term, acc = mt5.terminal_info(), mt5.account_info()

    con.print("[bold]Терминал и счёт[/]")
    check(term.connected, "связь с сервером брокера")
    check(term.trade_allowed, "алготрейдинг разрешён",
          "включите кнопку «Алготрейдинг»" if not term.trade_allowed else "")
    if acc is None:
        con.print("[red]Нет данных счёта[/]")
        raise SystemExit(1)

    kind = TRADE_MODE.get(acc.trade_mode, "?")
    is_demo = acc.trade_mode != 2
    con.print(f"  [dim]счёт {acc.login} @ {acc.server} · {kind} · "
              f"{acc.balance:.2f} {acc.currency} · плечо 1:{acc.leverage}[/]")
    check(is_demo, f"счёт демонстрационный ({kind})",
          "боевой счёт — первые прогоны только на демо", warn=True)
    check(acc.balance > 0, "на счёте есть средства",
          f"баланс {acc.balance:.2f} — ордера будут отклонены")
    check(acc.margin_free > 0 or acc.balance == 0, "свободная маржа",
          f"{acc.margin_free:.2f}")

    con.print(f"\n[bold]Предохранители[/]")
    check(settings.demo_only or not is_demo,
          f"DEMO_ONLY={settings.demo_only}",
          "на боевом счёте ордера будут заблокированы" if settings.demo_only
          and not is_demo else "")
    if not settings.demo_only:
        check(False, "DEMO_ONLY снят", "боевые ордера разрешены", warn=True)

    con.print(f"\n[bold]Инструменты[/]")

    def probe(names):
        return [n for n in names if mt5.symbol_info(n) is not None]

    want = probe(WANT)
    if not want:
        want = probe(FALLBACK)
        if want:
            con.print("  [dim]Инструментов Bybit на этом сервере нет — "
                      "проверяю доступные здесь.[/]")
    if not want:
        con.print("  [red]Ни один из известных инструментов не найден.[/]")
        problems.append("нет доступных инструментов")

    t = Table(box=None, pad_edge=False)
    for c in ("символ", "есть", "торгуется", "спред", "мин.лот", "шаг", "режимы"):
        t.add_column(c, justify="right" if c != "символ" else "left")
    available = []
    for s in want:
        i = mt5.symbol_info(s)
        if i is None:
            t.add_row(s, "[red]нет[/]", "", "", "", "", "")
            continue
        if not i.visible:
            mt5.symbol_select(s, True)
            i = mt5.symbol_info(s)
        tradable = i.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL
        modes = []
        if i.filling_mode & SYMBOL_FILLING_FOK:
            modes.append("FOK")
        if i.filling_mode & SYMBOL_FILLING_IOC:
            modes.append("IOC")
        if not modes:
            modes.append("[red]нет FOK/IOC[/]")
        tick = mt5.symbol_info_tick(s)
        t.add_row(s, "[green]да[/]",
                  "[green]да[/]" if tradable else "[red]нет[/]",
                  f"{i.spread}" if tick and tick.bid else "[red]нет котировок[/]",
                  f"{i.volume_min}", f"{i.volume_step}",
                  ", ".join(modes) or "?")
        if tradable and tick and tick.bid:
            available.append(s)
        if tradable and not (i.filling_mode & (SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC)):
            warnings.append(f"{s}: брокер не заявляет FOK/IOC — "
                            f"рыночный ордер может быть отклонён")
    con.print(t)
    check(bool(available), f"доступных для торговли символов: {len(available)}",
          "ни один инструмент не торгуется")
    if available:
        con.print(f"  [dim]{', '.join(available)}[/]")

    con.print(f"\n[bold]Итог[/]")
    if problems:
        con.print(f"  [red]блокирующих проблем: {len(problems)}[/]")
        for p in problems:
            con.print(f"    [red]•[/] {p}")
    else:
        con.print("  [green]блокирующих проблем нет[/]")
    if warnings:
        con.print(f"  [yellow]предупреждений: {len(warnings)}[/]")
        for w in warnings:
            con.print(f"    [yellow]•[/] {w}")
    con.print()
    mt5.shutdown()
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
