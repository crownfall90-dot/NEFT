"""Диагностика подключений. Ничего не торгует — только читает.

    python scripts/check_connection.py
"""
import logging
import sys
from pathlib import Path

# Консоль Windows по умолчанию cp1251 — рамки и галочки в неё не влезают.
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console
from rich.table import Table

from neft.adapters.crypto_broker import CryptoBroker
from neft.adapters.mt5_broker import MT5Broker
from neft.core.config import settings
from neft.core.models import BrokerError

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
con = Console()

MT5_SYMBOLS = ["XAUUSD+", "EURUSD+", "GBPUSD+", "EURUSD"]
CRYPTO_SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT"]


def section(title: str) -> None:
    con.print(f"\n[bold cyan]{title}[/]")


def check_mt5() -> None:
    section("MT5")
    # Предохранитель снимаем только для диагностики: смотрим, но не торгуем.
    broker = MT5Broker(demo_only=False)
    try:
        acc = broker.connect()
    except BrokerError as e:
        con.print(f"  [red]✗[/] {e}")
        return

    kind = "[green]DEMO[/]" if acc.is_demo else "[red]REAL[/]"
    con.print(f"  [green]✓[/] {acc.login} @ {acc.server}  {kind}  "
              f"{acc.balance:.2f} {acc.currency}")
    if not acc.is_demo:
        con.print("  [yellow]![/] Боевой счёт. Для бота нужен демо (MetaQuotes-Demo).")

    import MetaTrader5 as mt5
    if not mt5.terminal_info().trade_allowed:
        con.print("  [yellow]![/] Алготрейдинг ВЫКЛЮЧЕН — ордера будут отклонены.")

    t = Table("символ", "bid", "ask", "спред", "торгуемый", box=None, pad_edge=False)
    for s in MT5_SYMBOLS:
        try:
            q = broker.quote(s)
            ok = "[green]да[/]" if broker.is_tradable(s) else "[red]нет[/]"
            t.add_row(s, f"{q.bid:g}", f"{q.ask:g}", f"{q.spread:g}", ok)
        except BrokerError as e:
            t.add_row(s, f"[dim]{e}[/]", "", "", "")
    con.print(t)
    con.print(f"  позиций открыто: {len(broker.positions())}")
    broker.disconnect()


def check_crypto(exchange_id: str) -> None:
    section(f"{exchange_id} ({'TESTNET' if settings.crypto_testnet else 'LIVE'})")
    broker = CryptoBroker(exchange_id)
    try:
        acc = broker.connect()
    except Exception as e:
        con.print(f"  [red]✗[/] {type(e).__name__}: {e}")
        return

    if acc.server == "public":
        con.print("  [yellow]![/] Ключей нет — доступны только котировки")
    else:
        con.print(f"  [green]✓[/] баланс {acc.balance:.2f} {acc.currency}")

    t = Table("символ", "bid", "ask", "спред", box=None, pad_edge=False)
    for s in CRYPTO_SYMBOLS:
        try:
            q = broker.quote(s)
            t.add_row(s, f"{q.bid:g}", f"{q.ask:g}", f"{q.spread:g}")
        except Exception as e:
            t.add_row(s, f"[dim]{type(e).__name__}[/]", "", "")
    con.print(t)
    broker.disconnect()


if __name__ == "__main__":
    con.print(f"[dim]DEMO_ONLY={settings.demo_only}  "
              f"CRYPTO_TESTNET={settings.crypto_testnet}[/]")
    check_mt5()
    for ex in ("binance", "bybit"):
        check_crypto(ex)
    con.print()
