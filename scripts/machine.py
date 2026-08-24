"""Единая машина: HSS + London S/R под общим риск-слоем.

    python scripts/machine.py --symbols NAS100,DJ30,XAUUSD+
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.table import Table

from neft.backtest import data, metrics
from neft.backtest.engine import Backtester, Costs
from neft.core import symbols
from neft.core.portfolio import Portfolio
from neft.core.routing import enabled_for
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.london_sr import LondonSR
from neft.strategies.scalp_ha import ScalpHA

con = Console()


def build(symbol: str, balance: float, risk: float, route: bool = True):
    spec = symbols.load(symbol)
    limits = RiskLimits(risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
                        max_volume=100.0, max_daily_loss_pct=100.0,
                        max_drawdown_pct=100.0, min_free_margin_pct=0.0)
    rm = RiskManager(start_balance=balance, limits=limits)

    # vol_window=2 и вход стоп-ордером: обе настройки подняли винрейт на всех
    # инструментах при меньшем числе сделок.
    hss = ScalpHA(rr=1.0, pullback_bars=2, session=(16, 19), vol_mode="min",
                  vol_window=2, entry_mode="stop",
                  risk_pct=risk, risk_manager=rm, spec=spec)
    lsr = LondonSR(london=(11, 16), ny=(16, 23), min_rr=1.0,
                   risk_pct=risk, risk_manager=rm, spec=spec)

    # HSS первым: его сетап короче и точнее, London S/R ждёт часами.
    pf = Portfolio().add(hss, "HSS").add(lsr, "London S/R")
    if route:
        flags = enabled_for(symbol)
        for slot in pf.slots:
            slot.enabled = flags.get(slot.name, True)
    return pf, rm, spec


def run(symbol: str, bars: int, balance: float, risk: float, spread: float,
        route: bool = True):
    df = data.load(symbol, "M1", bars)
    pf, rm, spec = build(symbol, balance, risk, route)
    costs = Costs(spread_points=spread, contract_size=spec.contract_size,
                  point=spec.point)
    res = Backtester(pf, rm, costs, start_balance=balance).run(df)
    m = metrics.compute(res.equity, res.trades, balance, res.ruined)
    return pf, res, m, df


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="NAS100,DJ30,GER40,EURUSD+,XAUUSD+")
    p.add_argument("--bars", type=int, default=99_000)
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--risk", type=float, default=1.0)
    p.add_argument("--spread", type=float, default=1.0)
    p.add_argument("--no-route", action="store_true",
                   help="включить обе стратегии на всех инструментах")
    a = p.parse_args()

    total = Table(title="ЕДИНАЯ МАШИНА с маршрутизацией · M1 · риск 1% на сделку",
                  box=None, pad_edge=False, title_style="bold cyan")
    for c in ("инструмент", "сделок", "винрейт", "PF", "матож.$", "доход",
              "просадка", "серия-"):
        total.add_column(c, justify="right" if c != "инструмент" else "left")

    detail = Table(title="Вклад каждой стратегии", box=None, pad_edge=False,
                   title_style="bold cyan")
    for c in ("инструмент", "стратегия", "сигналов", "сделок", "винрейт", "P&L"):
        detail.add_column(c, justify="right" if c not in ("инструмент", "стратегия") else "left")

    symbols_list = [s.strip() for s in a.symbols.split(",")]
    for n, sym in enumerate(symbols_list, 1):
        # На 99к барах подготовка признаков — O(n) в чистом Python, это
        # десятки секунд на инструмент. Без строки прогресса выглядит как
        # зависание.
        con.print(f"[dim]{sym}  ({n}/{len(symbols_list)}) считаю...[/]", end="\r")
        try:
            pf, res, m, df = run(sym, a.bars, a.balance, a.risk, a.spread,
                                 route=not a.no_route)
        except Exception as e:
            con.print(f"[red]{sym}: {type(e).__name__}: {e}[/]" + " " * 20)
            continue
        c = "green" if m.net_profit > 0 else "red"
        total.add_row(sym, str(m.trades), f"[bold]{m.win_rate:.1f}%[/]",
                      f"[{c}]{m.profit_factor:.2f}[/]", f"[{c}]{m.expectancy:+.2f}[/]",
                      f"[{c}]{m.return_pct:+.2f}%[/]",
                      f"{m.max_drawdown_pct:.1f}%", str(m.max_loss_streak))
        first = True
        for slot, r in zip(pf.slots, pf.report()):
            if not slot.enabled:
                detail.add_row(sym if first else "", f"[dim]{r['name']}[/]",
                               "[dim]выкл[/]", "", "", "")
                first = False
                continue
            cc = "green" if r["pnl"] > 0 else "red"
            detail.add_row(sym if first else "", r["name"], str(r["signals"]),
                           str(r["trades"]),
                           f"{r['win_rate']:.1f}%" if r["trades"] else "—",
                           f"[{cc}]{r['pnl']:+.2f}[/]")
            first = False

    con.print(" " * 40, end="\r")   # стереть последнюю строку прогресса
    con.print(total)
    con.print()
    con.print(detail)
