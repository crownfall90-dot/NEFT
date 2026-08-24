"""Бэктест London S/R."""
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
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.london_sr import LondonSR

con = Console()


def run(df, *, symbol="NAS100", balance=1000.0, risk=1.0, spread=1.0,
        min_rr=1.0, london=(11, 16), ny=(16, 23)):
    spec = symbols.load(symbol)
    limits = RiskLimits(risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
                        max_volume=100.0, max_daily_loss_pct=100.0,
                        max_drawdown_pct=100.0, min_free_margin_pct=0.0)
    rm = RiskManager(start_balance=balance, limits=limits)
    strat = LondonSR(london=london, ny=ny, min_rr=min_rr,
                     risk_pct=risk, risk_manager=rm, spec=spec)
    costs = Costs(spread_points=spread, contract_size=spec.contract_size,
                  point=spec.point)
    res = Backtester(strat, rm, costs, start_balance=balance).run(df)
    return strat, res, metrics.compute(res.equity, res.trades, balance, res.ruined)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="NAS100,DJ30,GER40,EURUSD+,XAUUSD+,USOUSD")
    p.add_argument("--bars", type=int, default=99_000)
    p.add_argument("--min-rr", type=float, default=1.0)
    a = p.parse_args()

    t = Table(title="London S/R · M1 · зоны лондонской сессии 11-16 сервера",
              box=None, pad_edge=False, title_style="bold cyan")
    for c in ("инструмент", "касаний", "сделок", "винрейт", "PF", "средний RR",
              "матож.$", "доход", "просадка"):
        t.add_column(c, justify="right" if c != "инструмент" else "left")

    symbols_list = [s.strip() for s in a.symbols.split(",")]
    for n, sym in enumerate(symbols_list, 1):
        # Подготовка свингов/зон — O(n) в чистом Python на 99к барах,
        # десятки секунд на инструмент. Без прогресса выглядит как зависание.
        con.print(f"[dim]{sym}  ({n}/{len(symbols_list)}) считаю...[/]", end="\r")
        try:
            df = data.load(sym, "M1", a.bars)
            st, res, m = run(df, symbol=sym, min_rr=a.min_rr)
        except Exception as e:
            con.print(f"[red]{sym}: {type(e).__name__}: {e}[/]" + " " * 20)
            continue
        if not m.trades:
            t.add_row(sym, str(st.taps_seen), "0", "—", "—", "—", "—", "—", "—")
            continue
        rr = (m.avg_win / abs(m.avg_loss)) if m.avg_loss else 0
        c = "green" if m.net_profit > 0 else "red"
        t.add_row(sym, str(st.taps_seen), str(m.trades),
                  f"[bold]{m.win_rate:.1f}%[/]", f"[{c}]{m.profit_factor:.2f}[/]",
                  f"1:{rr:.2f}", f"[{c}]{m.expectancy:+.2f}[/]",
                  f"[{c}]{m.return_pct:+.2f}%[/]", f"{m.max_drawdown_pct:.1f}%")
    con.print(" " * 40, end="\r")
    con.print(t)
