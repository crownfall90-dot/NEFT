"""Полная проверка на демо-счёте: предполёт + реальный ордер.

Запускать, когда терминал переключён на демо-счёт.
Ордер отправляется минимальным объёмом и закрывается через несколько секунд.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import MetaTrader5 as mt5
from rich.console import Console

con = Console()


def main() -> None:
    if not mt5.initialize():
        con.print(f"[red]MT5 недоступен: {mt5.last_error()}[/]")
        raise SystemExit(1)
    acc = mt5.account_info()
    term = mt5.terminal_info()
    login, server, real = acc.login, acc.server, acc.trade_mode == 2
    allowed = term.trade_allowed
    mt5.shutdown()

    con.print(f"\n[bold]Терминал сейчас на счёте[/] {login} @ {server} "
              f"[{'red' if real else 'green'}]{'REAL' if real else 'DEMO'}[/]\n")

    if real:
        con.print("[bold red]Это боевой счёт — тест исполнения не запускается.[/]")
        con.print("Переключитесь на демо: в окне «Навигатор» дважды щёлкните")
        con.print("по счёту в разделе [bold]MetaQuotes-Demo[/], дождитесь подключения")
        con.print("и запустите этот пункт снова.\n")
        raise SystemExit(1)

    if not allowed:
        con.print("[bold yellow]Алготрейдинг выключен.[/]")
        con.print("Нажмите кнопку [bold]«Алготрейдинг»[/] на панели терминала "
                  "и запустите снова.\n")
        raise SystemExit(1)

    con.print("[cyan]— Шаг 1: предполётная проверка[/]")
    r = subprocess.run([str(PY), str(ROOT / "scripts" / "preflight.py")])
    if r.returncode != 0:
        con.print("\n[yellow]Есть блокирующие проблемы — ордер не отправляю.[/]\n")
        raise SystemExit(1)

    con.print("\n[cyan]— Шаг 2: тест исполнения (один минимальный ордер)[/]")
    subprocess.run([str(PY), str(ROOT / "scripts" / "order_test.py"),
                    "--symbol", "EURUSD", "--hold", "5"])


if __name__ == "__main__":
    main()
