"""Probe: HSS/ScalpHA and SonikPulse v3 on Tag XAUUSD.f."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from rich.console import Console

from neft.backtest import metrics
from neft.backtest.engine import Backtester, Costs
from neft.core import symbols
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.scalp_ha import ScalpHA
from neft.strategies.sonik_open import SonikPulse

con = Console()
BALANCE = 1000.0
SYMBOL = "XAUUSD.f"


def make_rm(risk: float = 0.25) -> RiskManager:
    return RiskManager(
        start_balance=BALANCE,
        limits=RiskLimits(
            risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
            min_risk_per_trade_pct=0.0, max_volume=100.0,
            max_daily_loss_pct=100.0, max_drawdown_pct=100.0,
            min_free_margin_pct=0.0,
        ),
    )


def summarize(name: str, m, trades) -> None:
    holds = [t.bars_held for t in trades]
    med = sorted(holds)[len(holds) // 2] if holds else 0
    color = "green" if m.return_pct >= 0 else "red"
    con.print(
        f"[{color}]{name}[/]: ret={m.return_pct:+.1f}% WR={m.win_rate:.0f}% "
        f"DD={m.max_drawdown_pct:.1f}% N={m.trades} hold≈{med}m PF={m.profit_factor:.2f}"
    )


def main() -> None:
    df = pd.read_csv("data/XAUUSD.f_M1_221d.csv", parse_dates=["time"])
    spec = symbols.load(SYMBOL, refresh=True)
    spr = float(df[df.time.dt.hour.between(7, 18)].spread.median())
    costs = Costs(
        spread_points=spr, contract_size=spec.contract_size, point=spec.point,
        commission_per_lot=0.0, commission_on_close=False, leverage=30,
    )
    con.print(f"M1 {df.time.iloc[0]}→{df.time.iloc[-1]} · {len(df)} bars · spread≈{spr:.0f}")

    con.print("\n[bold]HSS / ScalpHA[/]")
    for sess, rr, mode, extra in [
        ((7, 11), 1.5, "stop", {}),
        ((7, 11), 1.6, "market", {}),
        ((6, 20), 1.5, "stop", {}),
        ((7, 10), 1.6, "market", {"doji_body_pct": 0.08}),
        ((7, 12), 1.5, "stop", {"sl_buffer_points": 0.4}),
        ((7, 11), 1.6, "market", {"pullback_bars": 3}),
    ]:
        rm = make_rm()
        s = ScalpHA(
            risk_pct=0.25, risk_manager=rm, spec=spec, point=spec.point,
            commission_per_lot=0.0, base_volume=0.01,
            session=sess, rr=rr, entry_mode=mode, **extra,
        )
        res = Backtester(s, rm, costs, BALANCE, SYMBOL).run(df)
        m = metrics.compute(res.equity, res.trades, BALANCE, res.ruined)
        summarize(f"HSS sess={sess} rr={rr} {mode} {extra}", m, res.trades)

    con.print("\n[bold]SonikPulse pullback[/]")
    for kw in [
        dict(sl_points=2.5, tp_points=4.0, min_body_atr=0.55, require_break=True,
             require_pullback=True, max_prior_along=0.1, session_from=(7, 0),
             session_until=(10, 0), session2_from=None, session2_until=None),
        dict(sl_points=2.5, tp_points=4.0, min_body_atr=0.7, require_break=True,
             require_pullback=True, max_prior_along=0.0, session_from=(7, 0),
             session_until=(11, 0), session2_from=None, session2_until=None),
        dict(sl_points=2.2, tp_points=3.8, min_body_atr=0.55, require_break=True,
             require_pullback=True, max_prior_along=0.2, session_from=(6, 30),
             session_until=(20, 0), session2_from=None, max_trades_day=2),
        dict(sl_points=2.5, tp_points=4.0, min_body_atr=0.55, require_break=True,
             require_pullback=True, max_prior_along=0.1, min_compress=0.5,
             session_from=(7, 0), session_until=(10, 0),
             session2_from=(14, 0), session2_until=(17, 0)),
    ]:
        rm = make_rm()
        s = SonikPulse(risk_pct=0.25, risk_manager=rm, spec=spec, **kw)
        res = Backtester(s, rm, costs, BALANCE, SYMBOL).run(df)
        m = metrics.compute(res.equity, res.trades, BALANCE, res.ruined)
        summarize(f"Pulse {kw}", m, res.trades)


if __name__ == "__main__":
    main()
