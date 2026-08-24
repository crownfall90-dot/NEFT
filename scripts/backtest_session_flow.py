"""Бэктест сетапов Session Flow по отдельности и единым playbook.

    python scripts/backtest_session_flow.py
    python scripts/backtest_session_flow.py --symbols ETH/USDT:USDT --tf 5m --bars 40000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.table import Table

from neft.backtest import crypto_data, metrics
from neft.backtest.engine import Backtester, Costs
from neft.core.routing import CRYPTO_UNIVERSE
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.factory import crypto_strategy
from neft.strategies.session_flow import ALL_SETUPS
from scripts.compare_crypto import spec_for

con = Console()
BAL = 1000.0


def evaluate(symbol: str, tf: str, bars: int, risk: float, route: dict,
             flow: dict | None = None):
    df = crypto_data.load(symbol, tf, bars)
    spec = spec_for(symbol, float(df.close.median()))
    rm = RiskManager(start_balance=BAL, limits=RiskLimits(
        risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0, max_volume=1e6,
        max_daily_loss_pct=100.0, max_drawdown_pct=100.0, min_free_margin_pct=0.0))
    strat = crypto_strategy(route, rr=1.5, risk=risk, risk_manager=rm, spec=spec,
                            flow=flow)
    costs = Costs(spread_points=spec.default_spread,
                  contract_size=spec.contract_size, point=spec.point)
    res = Backtester(strat, rm, costs, start_balance=BAL).run(df)
    m = metrics.compute(res.equity, res.trades, BAL, res.ruined)
    days = (df.time.iloc[-1] - df.time.iloc[0]).days or 1
    by = {}
    if hasattr(strat, "by_setup"):
        by = {k: v for k, v in strat.by_setup.items() if v}
    elif hasattr(strat, "report"):
        by = {r["name"]: r["trades"] for r in strat.report() if r["trades"]}
    return {
        "symbol": symbol, "kind": route["strategy"],
        "setups": route.get("setups", "blend"),
        "tf": tf, "from": str(df.time.iloc[0]), "to": str(df.time.iloc[-1]),
        "days": days, "trades": m.trades, "win_rate": m.win_rate,
        "pf": None if m.profit_factor == float("inf") else m.profit_factor,
        "dd": m.max_drawdown_pct, "ret": m.return_pct,
        "expectancy": m.expectancy, "streak": m.max_loss_streak,
        "by": by, "setups_seen": getattr(strat, "setups_seen", m.trades),
    }


def score(row: dict) -> float:
    """Цель — винрейт и короткая просадка, не доход за период."""
    if row["trades"] < 25:
        return -999
    pf = row["pf"] or 0.0
    if pf < 1.0 or row["dd"] > 15 or row["streak"] > 10:
        return -100 + row["win_rate"] / 100
    return row["win_rate"] - row["dd"] * 1.5 - row["streak"] + min(pf, 1.3) * 5


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default=",".join(CRYPTO_UNIVERSE))
    p.add_argument("--tf", default="5m")
    p.add_argument("--bars", type=int, default=40_000)
    p.add_argument("--risk", type=float, default=0.5)
    p.add_argument("--json", default="logs/session_flow.json")
    a = p.parse_args()
    symbols = [s.strip() for s in a.symbols.split(",") if s.strip()]

    plan = []
    for setup in ALL_SETUPS:
        plan.append({"strategy": "Flow", "tf": a.tf, "session": None, "setups": setup})
    plan.append({"strategy": "Flow", "tf": a.tf, "session": None, "setups": "blend"})
    plan.append({"strategy": "Playbook", "tf": a.tf, "session": None})

    rows = []
    for sym in symbols:
        for route in plan:
            flow = {"gate_regime": route.get("setups") == "blend"
                    or route["strategy"] == "Playbook"}
            if route["strategy"] == "Flow" and route.get("setups") not in (None, "blend"):
                flow["gate_regime"] = False
            try:
                r = evaluate(sym, a.tf, a.bars, a.risk, route, flow)
            except Exception as e:  # noqa: BLE001
                con.print(f"[dim]{sym} {route}: {type(e).__name__}: {e}[/]")
                continue
            r["score"] = score(r)
            rows.append(r)
            pf = f"{r['pf']:.2f}" if r["pf"] is not None else "—"
            con.print(
                f"[dim]{sym.split('/')[0]:4s} {str(r['setups'])[:14]:14s} "
                f"{r['trades']:>4} сд  {r['win_rate']:5.1f}%  PF {pf:>5}  "
                f"{r['ret']:+7.1f}%  DD {r['dd']:4.1f}%[/]"
            )

    t = Table(title="Сетапы 5m/15m — отдельно и единый playbook", box=None)
    for c in ("пара", "что", "сделок", "win%", "PF", "доход", "DD", "score"):
        t.add_column(c, justify="right" if c != "пара" and c != "что" else "left")
    for r in sorted(rows, key=lambda x: -x.get("score", -999)):
        pf = f"{r['pf']:.2f}" if r["pf"] is not None else "—"
        t.add_row(
            r["symbol"].split("/")[0],
            f"{r['kind']}:{r['setups']}",
            str(r["trades"]), f"{r['win_rate']:.1f}", pf,
            f"{r['ret']:+.1f}%", f"{r['dd']:.1f}%", f"{r['score']:.1f}",
        )
    con.print(t)

    Path(a.json).parent.mkdir(parents=True, exist_ok=True)
    Path(a.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2,
                                       default=str), encoding="utf-8")
    con.print(f"[dim]JSON -> {a.json}[/]")


if __name__ == "__main__":
    main()
