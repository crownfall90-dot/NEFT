"""Бэктест 5-Minute London Breakout на M5."""
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
from neft.strategies.london_breakout import LondonBreakout

con = Console()


def run(df, *, symbol="NAS100", balance=1000.0, risk=1.0, rr=2.0, spread=1.0):
    spec = symbols.load(symbol)
    limits = RiskLimits(risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
                        max_volume=100.0, max_daily_loss_pct=100.0,
                        max_drawdown_pct=100.0, min_free_margin_pct=0.0)
    rm = RiskManager(start_balance=balance, limits=limits)
    strat = LondonBreakout(rr=rr, risk_pct=risk, risk_manager=rm, spec=spec)
    costs = Costs(spread_points=spread, contract_size=spec.contract_size,
                  point=spec.point)
    res = Backtester(strat, rm, costs, start_balance=balance).run(df)
    return strat, res, metrics.compute(res.equity, res.trades, balance, res.ruined)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="NAS100,DJ30,GER40,EURUSD+,XAUUSD+")
    p.add_argument("--tf", default="M5")
    p.add_argument("--bars", type=int, default=99_000)
    p.add_argument("--balance", type=float, default=1000.0)
    a = p.parse_args()

    t = Table(title=f"5-Minute London Breakout · {a.tf} · бокс 10-16 сервера "
                    f"(03:00-09:00 ET) · вход после 16:30 · депозит ${a.balance:.0f}",
              box=None, pad_edge=False, title_style="bold cyan")
    for c in ("инструмент", "RR", "риск", "дней", "сделок", "винрейт", "PF",
              "просадка", "доход", "рано", "поздно"):
        t.add_column(c, justify="right" if c != "инструмент" else "left")

    for sym in [s.strip() for s in a.symbols.split(",")]:
        try:
            df = data.load(sym, a.tf, a.bars)
        except Exception as e:
            con.print(f"[red]{sym}: {e}[/]")
            continue
        first = True
        for rr in (2.0, 1.5, 1.0):
            for risk in (0.75, 0.5):
                st, res, m = run(df, symbol=sym, balance=a.balance,
                                 risk=risk, rr=rr)
                if m.trades < 3:
                    continue
                c = "green" if m.net_profit > 0 else "red"
                wc = "green" if m.win_rate > 50 else "yellow"
                dc = "green" if m.max_drawdown_pct <= 5 else "red"
                t.add_row(sym if first else "", f"1:{rr}", f"{risk}%",
                          str(st.boxes_seen), str(m.trades),
                          f"[{wc}]{m.win_rate:.1f}%[/]", f"{m.profit_factor:.2f}",
                          f"[{dc}]{m.max_drawdown_pct:.1f}%[/]",
                          f"[{c}]{m.return_pct:+.2f}%[/]",
                          str(st.skipped_early), str(st.skipped_late))
                first = False
    con.print(t)
