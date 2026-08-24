"""Широкое сравнение: какие стратегии на каких парах работают.

M1 — HSS и London S/R. M5 — London Breakout.
Результат нормируется на день, неделю и месяц, чтобы разные периоды
истории можно было сравнивать между собой.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from rich.console import Console
from rich.table import Table

from neft.backtest import data, metrics
from neft.backtest.engine import Backtester, Costs
from neft.core import symbols
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.london_breakout import LondonBreakout
from neft.strategies.london_sr import LondonSR
from neft.strategies.scalp_ha import ScalpHA

con = Console()
BAL = 1000.0


def make_rm(risk):
    return RiskManager(start_balance=BAL, limits=RiskLimits(
        risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0, max_volume=100.0,
        max_daily_loss_pct=100.0, max_drawdown_pct=100.0, min_free_margin_pct=0.0))


def evaluate(symbol, kind, risk=0.75, bars=99_000):
    spec = symbols.load(symbol)
    tf = "M5" if kind == "Breakout" else "M1"
    df = data.load(symbol, tf, bars)
    rm = make_rm(risk)
    if kind == "HSS":
        strat = ScalpHA(rr=1.0, pullback_bars=2, session=(16, 19), vol_mode="min",
                        vol_window=2, entry_mode="stop",
                        risk_pct=risk, risk_manager=rm, spec=spec)
    elif kind == "London S/R":
        strat = LondonSR(london=(11, 16), ny=(16, 23), min_rr=1.0,
                         risk_pct=risk, risk_manager=rm, spec=spec)
    else:
        strat = LondonBreakout(rr=1.5, risk_pct=risk, risk_manager=rm, spec=spec)

    costs = Costs(spread_points=spec.default_spread,
                  contract_size=spec.contract_size, point=spec.point)
    res = Backtester(strat, rm, costs, start_balance=BAL).run(df)
    m = metrics.compute(res.equity, res.trades, BAL, res.ruined)

    days = (df.time.iloc[-1] - df.time.iloc[0]).days or 1
    tdays = df.time.dt.date.nunique() or 1
    months = days / 30.44
    return {
        "symbol": symbol, "kind": kind, "tf": tf,
        "from": df.time.iloc[0], "to": df.time.iloc[-1],
        "days": days, "tdays": tdays, "months": months,
        "trades": m.trades, "win_rate": m.win_rate, "pf": m.profit_factor,
        "dd": m.max_drawdown_pct, "ret": m.return_pct, "end": m.end_balance,
        "expectancy": m.expectancy, "streak": m.max_loss_streak,
        "per_month": m.return_pct / months,
        "per_week": m.return_pct / (days / 7),
        "trades_month": m.trades / months,
        "trades_week": m.trades / (days / 7),
        "trades_day": m.trades / tdays,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", required=True)
    p.add_argument("--kinds", default="HSS,London S/R,Breakout")
    p.add_argument("--risk", type=float, default=0.75)
    p.add_argument("--min-trades", type=int, default=8)
    p.add_argument("--json", default="logs/compare.json")
    a = p.parse_args()

    syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    kinds = [k.strip() for k in a.kinds.split(",")]
    rows = []
    for sym in syms:
        for kind in kinds:
            try:
                r = evaluate(sym, kind, a.risk)
            except Exception as e:
                con.print(f"[dim]{sym} {kind}: {type(e).__name__}[/]")
                continue
            rows.append(r)
            con.print(f"[dim]{sym:9s} {kind:11s} {r['trades']:>4} сделок  "
                      f"{r['win_rate']:5.1f}%  {r['ret']:+7.2f}%[/]")

    Path(a.json).write_text(json.dumps(rows, ensure_ascii=False, default=str),
                            encoding="utf-8")

    good = [r for r in rows if r["trades"] >= a.min_trades]
    good.sort(key=lambda r: -r["per_month"])

    t = Table(title=f"Сравнение · депозит ${BAL:.0f} · риск {a.risk}% · "
                    f"минимум {a.min_trades} сделок",
              box=None, pad_edge=False, title_style="bold cyan")
    for c in ("инструмент", "стратегия", "ТФ", "период", "сделок",
              "в неделю", "винрейт", "PF", "просадка", "% в месяц", "% всего"):
        t.add_column(c, justify="right" if c not in ("инструмент", "стратегия", "ТФ", "период") else "left")
    for r in good[:40]:
        wc = "green" if r["win_rate"] > 50 else "yellow"
        dc = "green" if r["dd"] <= 5 else "red"
        c = "green" if r["ret"] > 0 else "red"
        t.add_row(r["symbol"], r["kind"], r["tf"],
                  f"{pd.Timestamp(r['from']):%m.%y}—{pd.Timestamp(r['to']):%m.%y}",
                  str(r["trades"]), f"{r['trades_week']:.1f}",
                  f"[{wc}]{r['win_rate']:.1f}%[/]", f"{r['pf']:.2f}",
                  f"[{dc}]{r['dd']:.1f}%[/]",
                  f"[{c}]{r['per_month']:+.2f}%[/]",
                  f"[{c}]{r['ret']:+.2f}%[/]")
    con.print(t)
