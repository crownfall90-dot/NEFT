"""Проверка пути исполнения: один минимальный ордер от отправки до закрытия.

Проверяет то, что бэктест проверить не может: примет ли брокер ордер, встанут
ли SL и TP, каким будет проскальзывание, закроется ли позиция по команде.

РАБОТАЕТ ТОЛЬКО НА ДЕМО-СЧЁТЕ. На боевом отказывается запускаться.

    python scripts/order_test.py --symbol EURUSD --volume 0.01
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import MetaTrader5 as mt5
from rich.console import Console

from neft.core.config import settings

con = Console()
MAGIC = 20260821


def step(n, text):
    con.print(f"\n[bold cyan]{n}.[/] {text}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="EURUSD")
    p.add_argument("--volume", type=float, default=0.0)
    p.add_argument("--sl-points", type=int, default=300)
    p.add_argument("--tp-points", type=int, default=300)
    p.add_argument("--hold", type=int, default=5, help="секунд держать позицию")
    p.add_argument("--demo", action="store_true",
                   help="использовать учётные данные MT5_DEMO_* (нужен --force-login)")
    p.add_argument("--force-login", action="store_true",
                   help="войти в счёт из .env, а не использовать открытый в терминале")
    a = p.parse_args()

    kwargs = settings.mt5_kwargs(demo=a.demo, force_login=a.force_login)
    if not mt5.initialize(**kwargs):
        con.print(f"[red]MT5 не инициализирован: {mt5.last_error()}[/]")
        raise SystemExit(1)

    acc = mt5.account_info()
    con.print(f"[dim]счёт {acc.login} @ {acc.server} · "
              f"{'DEMO' if acc.trade_mode != 2 else 'REAL'} · "
              f"{acc.balance:.2f} {acc.currency}[/]")

    # Единственная защита, которую нельзя обойти флагом.
    if acc.trade_mode == 2:
        con.print("\n[bold red]ОТКАЗ: счёт боевой.[/]")
        con.print("Тест исполнения выполняется только на демо-счёте.")
        con.print("Откройте демо: Файл → Открыть счёт → MetaQuotes Ltd → Демо")
        mt5.shutdown()
        raise SystemExit(1)

    if not mt5.terminal_info().trade_allowed:
        con.print("[red]Алготрейдинг выключен в терминале.[/]")
        mt5.shutdown()
        raise SystemExit(1)

    sym = a.symbol
    if not mt5.symbol_select(sym, True):
        con.print(f"[red]Символ {sym} недоступен[/]")
        mt5.shutdown()
        raise SystemExit(1)
    info = mt5.symbol_info(sym)
    for _ in range(10):
        tick = mt5.symbol_info_tick(sym)
        if tick and tick.bid:
            break
        time.sleep(0.3)
    if not tick or not tick.bid:
        con.print(f"[red]Нет котировок по {sym} — рынок закрыт?[/]")
        mt5.shutdown()
        raise SystemExit(1)

    volume = a.volume or info.volume_min
    point = info.point
    price = tick.ask
    sl = price - a.sl_points * point
    tp = price + a.tp_points * point

    filling = (mt5.ORDER_FILLING_FOK if info.filling_mode & 1
               else mt5.ORDER_FILLING_IOC if info.filling_mode & 2
               else mt5.ORDER_FILLING_RETURN)

    step(1, f"Отправка BUY {sym} {volume} по {price} "
            f"(SL {sl:.{info.digits}f} / TP {tp:.{info.digits}f})")
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": volume,
        "type": mt5.ORDER_TYPE_BUY, "price": price,
        "sl": sl, "tp": tp, "deviation": 20, "magic": MAGIC,
        "comment": "neft order test", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    res = mt5.order_send(req)
    if res is None:
        con.print(f"  [red]✗ order_send вернул None: {mt5.last_error()}[/]")
        mt5.shutdown()
        raise SystemExit(1)
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        # Отказ по расписанию — не дефект бота: запрос дошёл и получил
        # осмысленный ответ. Отличаем его от настоящих проблем.
        SCHEDULE = {mt5.TRADE_RETCODE_MARKET_CLOSED: "рынок закрыт",
                    mt5.TRADE_RETCODE_TRADE_DISABLED: "торговля отключена"}
        if res.retcode in SCHEDULE:
            con.print(f"  [yellow]! {SCHEDULE[res.retcode]}[/] "
                      f"(retcode={res.retcode})")
            con.print("
  [green]✓ Проверено:[/] соединение, формирование "
                      "запроса, режим заполнения, обмен с брокером.")
            con.print("  [yellow]Не проверено:[/] исполнение, приём SL/TP, "
                      "проскальзывание, закрытие.")
            con.print("
  [dim]Повторите после открытия рынка.[/]
")
            mt5.shutdown()
            raise SystemExit(2)
        con.print(f"  [red]✗ отклонён: retcode={res.retcode} «{res.comment}»[/]")
        mt5.shutdown()
        raise SystemExit(1)

    slip = (res.price - price) / point
    con.print(f"  [green]✓ исполнен[/] сделка #{res.deal}, цена {res.price}, "
              f"проскальзывание {slip:+.0f} пунктов")

    step(2, "Проверка, что позиция открыта и SL/TP приняты брокером")
    time.sleep(1)
    positions = [p for p in (mt5.positions_get(symbol=sym) or ())
                 if p.magic == MAGIC]
    if not positions:
        con.print("  [red]✗ позиция не найдена[/]")
        mt5.shutdown()
        raise SystemExit(1)
    pos = positions[0]
    con.print(f"  [green]✓ позиция #{pos.ticket}[/] объём {pos.volume}, "
              f"вход {pos.price_open}")
    ok_sl = abs(pos.sl - sl) < point * 10
    ok_tp = abs(pos.tp - tp) < point * 10
    con.print(f"  {'[green]✓[/]' if ok_sl else '[red]✗[/]'} SL у брокера: {pos.sl}")
    con.print(f"  {'[green]✓[/]' if ok_tp else '[red]✗[/]'} TP у брокера: {pos.tp}")
    if not (ok_sl and ok_tp):
        con.print("  [yellow]![/] брокер изменил уровни — учтите в стратегии")

    step(3, f"Удержание {a.hold} с, затем закрытие по рынку")
    time.sleep(a.hold)
    tick = mt5.symbol_info_tick(sym)
    close = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": pos.volume,
        "type": mt5.ORDER_TYPE_SELL, "position": pos.ticket,
        "price": tick.bid, "deviation": 20, "magic": MAGIC,
        "comment": "neft order test close", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    res2 = mt5.order_send(close)
    if res2 is None or res2.retcode != mt5.TRADE_RETCODE_DONE:
        con.print(f"  [red]✗ закрытие не прошло: "
                  f"{res2.retcode if res2 else mt5.last_error()}[/]")
        con.print("  [yellow]ВНИМАНИЕ: позиция осталась открытой, закройте вручную[/]")
        mt5.shutdown()
        raise SystemExit(1)
    con.print(f"  [green]✓ закрыта[/] по {res2.price}")

    left = [p for p in (mt5.positions_get(symbol=sym) or ()) if p.magic == MAGIC]
    con.print(f"  {'[green]✓[/]' if not left else '[red]✗[/]'} "
              f"открытых позиций бота: {len(left)}")

    con.print("\n[bold green]Путь исполнения работает полностью.[/]")
    con.print("[dim]Отправка → исполнение → SL/TP у брокера → закрытие → сверка.[/]\n")
    mt5.shutdown()


if __name__ == "__main__":
    main()
