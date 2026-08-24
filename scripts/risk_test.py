"""Мартингейл под правилом риска 0.5-3% против мартингейла без него."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.table import Table

from neft.backtest import data, metrics
from neft.backtest.engine import Backtester, Costs
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.martingale import Martingale

con = Console()
BAL = 1000.0
df = data.load("EURUSD+", "M1", 50_000)


def run(label, risk_pct, max_risk, steps=10, base_lot=0.01):
    limits = RiskLimits(
        risk_per_trade_pct=risk_pct or 1.0,
        max_risk_per_trade_pct=max_risk,
        max_volume=10.0, max_daily_loss_pct=100.0,
        max_drawdown_pct=100.0, min_free_margin_pct=0.0,
    )
    risk = RiskManager(start_balance=BAL, limits=limits)
    strat = Martingale(base_volume=base_lot, risk_pct=risk_pct, risk_manager=risk,
                       tp_pips=10, sl_pips=10, multiplier=2.0, max_steps=steps)
    res = Backtester(strat, risk, Costs(spread_points=1.0), start_balance=BAL).run(df)
    m = metrics.compute(res.equity, res.trades, BAL, res.ruined)
    return label, m, len(res.rejected), strat


rows = [
    run("Фикс. лот 0.01, риска нет", None, 100.0, steps=1),
    run("Мартингейл без потолка риска", None, 100.0),
    run("Риск 1%, потолок 3% (наше правило)", 1.0, 3.0),
    run("Риск 0.5%, потолок 3%", 0.5, 3.0),
    run("Риск 2%, потолок 3%", 2.0, 3.0),
]

t = Table(box=None, pad_edge=False)
t.add_column("конфигурация"); t.add_column("итог", justify="right")
t.add_column("доход", justify="right"); t.add_column("просадка", justify="right")
t.add_column("сделок", justify="right"); t.add_column("отказов риска", justify="right")
for label, m, rej, strat in rows:
    c = "green" if m.net_profit > 0 else "red"
    t.add_row(label, f"${m.end_balance:,.0f}",
              f"[{c}]{m.return_pct:+.2f}%[/]",
              f"{m.max_drawdown_pct:.1f}%", f"{m.trades}", f"{rej}")
con.print(t)
