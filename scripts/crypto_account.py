"""Единая машина для крипты: одна из наших стратегий на каждой паре,
собранные в один виртуальный счёт по маршрутизации из routing.py.

Работает так же, как scripts/account.py для MT5-инструментов: каждая пара
торгуется своими стратегиями на своих таймфреймах (см. CRYPTO_ROUTES),
сделки сводятся в один хронологический поток по РЕАЛЬНОМУ времени закрытия
из движка, риск на сделку считается от текущего капитала счёта.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.table import Table

from neft.backtest import crypto_data
from neft.backtest.engine import Backtester, Costs
from neft.core.risk import RiskLimits, RiskManager
from neft.core.routing import CRYPTO_ROUTES, crypto_routes_for
from neft.strategies.factory import crypto_strategy, label_for
from scripts.account import clip, report, simulate
from scripts.compare_crypto import spec_for

con = Console()


def route_trades(symbol, route, bars, risk, balance, days=None):
    """Сделки одной (пара, стратегия, ТФ) конфигурации, с реальным временем.

    days, если задан, приоритетнее bars — передаётся дальше в crypto_data.load
    тем же способом, каким там пересчитывается число баров под таймфрейм.
    """
    df = crypto_data.load(symbol, route["tf"], bars, days=days)
    spec = spec_for(symbol, float(df.close.median()))
    limits = RiskLimits(risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
                        max_volume=1e6, max_daily_loss_pct=100.0,
                        max_drawdown_pct=100.0, min_free_margin_pct=0.0)
    rm = RiskManager(start_balance=balance, limits=limits)

    strat = crypto_strategy(route, rr=1.0, risk=risk, risk_manager=rm, spec=spec)
    label = label_for(route)

    px = float(df.close.median())
    costs = Costs(spread_points=spec.default_spread,
                  contract_size=spec.contract_size, point=spec.point,
                  commission_per_lot=crypto_data.TAKER_FEE * px * spec.contract_size,
                  commission_maker_per_lot=crypto_data.MAKER_FEE * px * spec.contract_size)
    res = Backtester(strat, rm, costs, start_balance=balance).run(df)

    risk_frac = risk / 100
    return [{
        "symbol": symbol, "strategy": label, "pnl": t.pnl, "reason": t.reason,
        "bars": t.bars_held, "risk_frac": risk_frac,
        "r_multiple": t.pnl / (balance * risk_frac) if risk_frac else 0,
        "time": t.closed_at,
    } for t in res.trades]


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default=",".join(CRYPTO_ROUTES))
    p.add_argument("--bars", type=int, default=40_000)
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--risk", type=float, default=0.75)
    p.add_argument("--sweep", action="store_true",
                   help="прогнать 0.5%%/0.75%%/1.0%% и показать все три")
    p.add_argument("--dd-stop", type=float, default=None,
                   help="остановить торговлю при просадке, %%")
    a = p.parse_args()

    syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    pool = {}
    for sym in syms:
        routes = crypto_routes_for(sym)
        if not routes:
            con.print(f"[dim]{sym}: маршрутизация не задана, пропуск[/]")
            continue
        merged = []
        for r in routes:
            try:
                trades = route_trades(sym, r, a.bars, a.risk, a.balance)
                merged += trades
                con.print(f"[dim]{sym:20s} {r['strategy']:11s} {r['tf']:>3s} "
                          f"{('сессия '+str(r['session'])) if r['session'] else 'круглосуточно':16s}"
                          f" -> {len(trades)} сделок[/]")
            except Exception as e:
                con.print(f"[red]{sym} {r['strategy']} {r['tf']}: "
                          f"{type(e).__name__}: {e}[/]")
        if merged:
            pool[sym] = merged

    if not pool:
        con.print("[red]Ни одной пары не обработано.[/]")
        raise SystemExit(1)

    combos = {
        "ETH": ["ETH/USDT:USDT"],
        "ETH + BCH + HYPE": ["ETH/USDT:USDT", "BCH/USDT:USDT", "HYPE/USDT:USDT"],
        "вселенная без BTC": list(CRYPTO_ROUTES),
    }
    risks = (0.5, 0.75, 1.0) if a.sweep else (a.risk,)

    t = Table(title=f"КРИПТО-СЧЁТ ${a.balance:.0f} "
                    f"(депозит — единица сравнения тестов)",
              box=None, pad_edge=False, title_style="bold cyan")
    for c in ("состав", "окно", "риск", "сделок", "винрейт", "просадка",
              "итог $", "доход"):
        t.add_column(c, justify="right" if c != "состав" else "left")

    for label, syms_in in combos.items():
        merged_raw = [x for s in syms_in for x in pool.get(s, [])]
        merged_raw, w0, w1 = clip(merged_raw)
        if not merged_raw:
            continue
        window = f"{w0:%d.%m.%y}—{w1:%d.%m.%y}" if w0 is not None else ""
        first = True
        for risk in risks:
            curve, taken, halted = simulate(merged_raw, a.balance, risk, a.dd_stop)
            r = report(label, curve, taken, a.balance, halted)
            if r is None:
                continue
            c = "green" if r["ret"] > 0 else "red"
            t.add_row(label if first else "", window if first else "",
                      f"{risk}%", str(r["trades"]), f"{r['win_rate']:.1f}%",
                      f"{r['dd']:.1f}%", f"{r['end']:,.0f}",
                      f"[{c}]{r['ret']:+.2f}%[/]")
            first = False
    con.print(t)
