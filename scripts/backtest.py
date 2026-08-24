"""Бэктест мартингейла на EURUSD+ M1.

    python scripts/backtest.py [--balance 1000] [--steps 6] [--json out.json]
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.table import Table

from neft.backtest import data, metrics
from neft.backtest.engine import Backtester, Costs
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.martingale import Martingale

logging.basicConfig(level=logging.INFO, format="%(message)s")
con = Console()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="EURUSD+")
    p.add_argument("--bars", type=int, default=50_000)
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--lot", type=float, default=0.01)
    p.add_argument("--tp", type=float, default=10)
    p.add_argument("--sl", type=float, default=10)
    p.add_argument("--steps", type=int, default=6)
    p.add_argument("--mult", type=float, default=2.0)
    p.add_argument("--spread", type=float, default=1.0)
    p.add_argument("--json", default=None)
    args = p.parse_args()

    df = data.load(args.symbol, "M1", args.bars)
    con.print(f"[dim]{args.symbol} M1: {len(df)} баров, "
              f"{df.time.iloc[0]} — {df.time.iloc[-1]}[/]")

    strat = Martingale(base_volume=args.lot, tp_pips=args.tp, sl_pips=args.sl,
                       multiplier=args.mult, max_steps=args.steps)
    risk = RiskManager(
        start_balance=args.balance,
        limits=RiskLimits(max_volume=10.0, max_daily_loss_pct=100.0,
                          max_drawdown_pct=100.0, min_free_margin_pct=0.0),
    )
    bt = Backtester(strat, risk, Costs(spread_points=args.spread),
                    start_balance=args.balance)
    res = bt.run(df)
    m = metrics.compute(res.equity, res.trades, args.balance, res.ruined)

    t = Table(box=None, pad_edge=False)
    t.add_column("метрика", style="dim"); t.add_column("значение", justify="right")
    color = "green" if m.net_profit > 0 else "red"
    t.add_row("Стартовый баланс", f"${m.start_balance:,.2f}")
    t.add_row("Итоговый баланс", f"[{color}]${m.end_balance:,.2f}[/]")
    t.add_row("Прибыль", f"[{color}]${m.net_profit:,.2f} ({m.return_pct:+.2f}%)[/]")
    t.add_row("Сделок", f"{m.trades}")
    t.add_row("Винрейт", f"{m.win_rate:.1f}%  ({m.wins}/{m.losses})")
    t.add_row("Profit factor", f"{m.profit_factor:.3f}")
    t.add_row("Макс. просадка", f"[red]{m.max_drawdown_pct:.1f}%  "
                                f"(${m.max_drawdown_abs:,.2f})[/]")
    t.add_row("Макс. серия убытков", f"[red]{m.max_loss_streak}[/]")
    t.add_row("Крупнейший убыток", f"${m.largest_loss:,.2f}")
    t.add_row("Матожидание сделки", f"${m.expectancy:+.4f}")
    t.add_row("Достигнут потолок серии", f"{strat.resets} раз")
    t.add_row("Максимальный шаг", f"{strat.max_step_seen}")
    con.print(t)

    if res.ruined:
        con.print(f"\n[bold red]СЧЁТ СЛИТ[/] — {res.ruin_time}")
    if res.halt_reason:
        con.print(f"[yellow]Kill-switch:[/] {res.halt_reason}")

    if args.json:
        eq = res.equity.iloc[:: max(1, len(res.equity) // 1500)]
        Path(args.json).write_text(json.dumps({
            "symbol": args.symbol,
            "period": [str(df.time.iloc[0]), str(df.time.iloc[-1])],
            "params": {"lot": args.lot, "tp": args.tp, "sl": args.sl,
                       "steps": args.steps, "mult": args.mult, "spread": args.spread},
            "metrics": m.as_dict(),
            "resets": strat.resets, "max_step": strat.max_step_seen,
            "ruined": res.ruined,
            "equity": [{"t": str(i), "v": round(float(v), 2)}
                       for i, v in eq.items()],
            "trades": [{"side": t.side.value, "vol": t.volume,
                        "entry": t.entry, "exit": t.exit,
                        "pnl": round(t.pnl, 2), "reason": t.reason}
                       for t in res.trades],
        }, ensure_ascii=False), encoding="utf-8")
        con.print(f"[dim]JSON -> {args.json}[/]")


if __name__ == "__main__":
    main()
