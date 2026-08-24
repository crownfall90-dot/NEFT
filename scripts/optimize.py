"""Перебор конфигураций: инструмент, набор стратегий, риск, RR.

Депозит $1000 — единица сравнения между прогонами, не целевой капитал.
Риск в коридоре 0.5–3% по правилу сайзинга. Порога «прошёл / не прошёл»
по просадке и винрейту нет — смотрим PF, доход, просадку как факты.
"""
import argparse
import json
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
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.london_sr import LondonSR
from neft.strategies.scalp_ha import ScalpHA

con = Console()

BALANCE = 1000.0


def build(symbol, risk, use_hss, use_lsr, rr, min_rr, kill_dd):
    spec = symbols.load(symbol)
    limits = RiskLimits(
        risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
        max_volume=100.0, max_daily_loss_pct=100.0,
        max_drawdown_pct=kill_dd,          # kill-switch риск-слоя
        min_free_margin_pct=0.0,
    )
    rm = RiskManager(start_balance=BALANCE, limits=limits)
    pf = Portfolio()
    if use_hss:
        pf.add(ScalpHA(rr=rr, pullback_bars=2, session=(16, 19), vol_mode="min",
                       vol_window=2, entry_mode="stop",
                       risk_pct=risk, risk_manager=rm, spec=spec), "HSS")
    if use_lsr:
        pf.add(LondonSR(london=(11, 16), ny=(16, 23), min_rr=min_rr,
                        risk_pct=risk, risk_manager=rm, spec=spec), "London S/R")
    return pf, rm, spec


def evaluate(df, symbol, risk, use_hss, use_lsr, rr, min_rr, kill_dd, spread=1.0):
    pf, rm, spec = build(symbol, risk, use_hss, use_lsr, rr, min_rr, kill_dd)
    costs = Costs(spread_points=spread, contract_size=spec.contract_size,
                  point=spec.point)
    res = Backtester(pf, rm, costs, start_balance=BALANCE).run(df)
    m = metrics.compute(res.equity, res.trades, BALANCE, res.ruined)
    return m, pf, res


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="NAS100,DJ30,GER40,EURUSD+,XAUUSD+,USOUSD")
    p.add_argument("--bars", type=int, default=99_000)
    p.add_argument("--min-trades", type=int, default=10)
    p.add_argument("--json", default="logs/optimize.json")
    a = p.parse_args()

    combos = []
    for use_hss, use_lsr, label in ((True, False, "HSS"),
                                    (False, True, "London"),
                                    (True, True, "обе")):
        for risk in (0.5, 0.75, 1.0):
            for rr in (1.0, 1.5):
                for min_rr in (1.0, 1.5):
                    if not use_lsr and min_rr != 1.0:
                        continue
                    if not use_hss and rr != 1.0:
                        continue
                    combos.append((use_hss, use_lsr, label, risk, rr, min_rr))

    results = []
    for sym in [s.strip() for s in a.symbols.split(",")]:
        try:
            df = data.load(sym, "M1", a.bars)
        except Exception as e:
            con.print(f"[red]{sym}: {e}[/]")
            continue
        con.print(f"[dim]{sym}: {len(combos)} конфигураций…[/]")
        for use_hss, use_lsr, label, risk, rr, min_rr in combos:
            try:
                m, pf, res = evaluate(df, sym, risk, use_hss, use_lsr, rr,
                                      min_rr, kill_dd=100.0)
            except Exception:
                continue
            if m.trades < a.min_trades:
                continue
            results.append({
                "symbol": sym, "strategies": label, "risk": risk,
                "rr": rr, "min_rr": min_rr,
                "trades": m.trades, "win_rate": m.win_rate,
                "pf": m.profit_factor, "dd": m.max_drawdown_pct,
                "ret": m.return_pct, "expectancy": m.expectancy,
                "streak": m.max_loss_streak, "end": m.end_balance,
            })

    Path(a.json).write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")

    ranked = sorted(results, key=lambda r: (-r["pf"] if r["pf"] != float("inf") else 0,
                                            -r["ret"]))
    t = Table(title=f"конфигурации · депозит ${BALANCE:.0f} (единица сравнения) · "
                    f"{len(results)} вариантов",
              box=None, pad_edge=False, title_style="bold cyan")
    for c in ("инструмент", "стратегии", "риск", "RR", "сделок", "винрейт",
              "просадка", "PF", "итог $", "доход"):
        t.add_column(c, justify="right" if c not in ("инструмент", "стратегии") else "left")
    for r in ranked[:25]:
        c = "green" if r["ret"] > 0 else "red"
        t.add_row(r["symbol"], r["strategies"], f"{r['risk']}%", f"1:{r['rr']}",
                  str(r["trades"]), f"{r['win_rate']:.1f}%",
                  f"{r['dd']:.1f}%", f"{r['pf']:.2f}",
                  f"{r['end']:,.0f}", f"[{c}]{r['ret']:+.2f}%[/]")
    con.print(t)


if __name__ == "__main__":
    main()
