"""Бэктест скальпинг-стратегии Heikin Ashi + EMA100 + объёмный doji.

    python scripts/backtest_scalp.py --tf M1 --rr 1.5 --risk 1.0
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
from neft.backtest.engine import Backtester
from neft.core import symbols
from neft.core.bybit_cfd_fees import costs_for
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.scalp_ha import ScalpHA

con = Console()


def run(df, *, balance=1000.0, rr=1.0, risk=1.0, pullback=2, ema=100,
        doji=0.10, clean=0.05, match=False, spread=1.0, symbol="EURUSD+",
        vol_mode="min", session=None, vol_window=3,
        entry_mode="stop", tp_from_extreme=True):
    spec = symbols.load(symbol)
    limits = RiskLimits(risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
                        max_volume=10.0, max_daily_loss_pct=100.0,
                        max_drawdown_pct=100.0, min_free_margin_pct=0.0)
    rm = RiskManager(start_balance=balance, limits=limits)
    strat = ScalpHA(ema_period=ema, pullback_bars=pullback, rr=rr,
                    doji_body_pct=doji, clean_wick_pct=clean,
                    require_matching_doji=match, risk_pct=risk,
                    vol_mode=vol_mode, session=session, vol_window=vol_window,
                    entry_mode=entry_mode, tp_from_extreme=tp_from_extreme,
                    risk_manager=rm, spec=spec)
    costs = costs_for(spec, spread_points=spread)
    res = Backtester(strat, rm, costs, start_balance=balance).run(df)
    m = metrics.compute(res.equity, res.trades, balance, res.ruined)
    return strat, res, m


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="EURUSD+")
    p.add_argument("--tf", default="M1")
    p.add_argument("--bars", type=int, default=99_000)
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--rr", type=float, default=1.0)
    p.add_argument("--risk", type=float, default=1.0)
    p.add_argument("--pullback", type=int, default=2)
    p.add_argument("--ema", type=int, default=100)
    p.add_argument("--doji", type=float, default=0.10)
    p.add_argument("--match", action="store_true")
    p.add_argument("--spread", type=float, default=1.0)
    p.add_argument("--vol-mode", default="min", choices=["min", "max"])
    p.add_argument("--session", default=None,
                   help="часы сервера, напр. 16-19 (= 9-12 ET)")
    p.add_argument("--json", default=None)
    a = p.parse_args()

    df = data.load(a.symbol, a.tf, a.bars)
    con.print(f"[dim]{a.symbol} {a.tf}: {len(df)} баров, "
              f"{df.time.iloc[0]} — {df.time.iloc[-1]}[/]")

    strat, res, m = run(df, balance=a.balance, rr=a.rr, risk=a.risk,
                        pullback=a.pullback, ema=a.ema, doji=a.doji,
                        match=a.match, spread=a.spread, symbol=a.symbol,
                        vol_mode=a.vol_mode,
                        session=tuple(int(x) for x in a.session.split("-"))
                        if a.session else None)

    t = Table(box=None, pad_edge=False)
    t.add_column("метрика", style="dim"); t.add_column("значение", justify="right")
    c = "green" if m.net_profit > 0 else "red"
    t.add_row("Итоговый баланс", f"[{c}]${m.end_balance:,.2f}[/]")
    t.add_row("Доход", f"[{c}]{m.return_pct:+.2f}%[/]")
    t.add_row("Сделок", f"{m.trades}")
    t.add_row("Винрейт", f"{m.win_rate:.1f}%  ({m.wins}/{m.losses})")
    t.add_row("Profit factor", f"{m.profit_factor:.3f}")
    t.add_row("Матожидание", f"${m.expectancy:+.4f}")
    t.add_row("Макс. просадка", f"{m.max_drawdown_pct:.1f}%")
    t.add_row("Макс. серия убытков", f"{m.max_loss_streak}")
    t.add_row("Средний выигрыш", f"${m.avg_win:,.2f}")
    t.add_row("Средний убыток", f"${m.avg_loss:,.2f}")
    t.add_row("Сетапов найдено", f"{strat.setups_seen}")
    t.add_row("Отсеяно: нет структуры", f"{strat.skipped_structure}")
    t.add_row("Отсеяно: малая doji до", f"{strat.skipped_small_doji}")
    t.add_row("Ордеров не сработало", f"{res.expired}")
    t.add_row("Отсеяно по длине стопа", f"{strat.skipped_sl_range}")
    t.add_row("Отсеяно вне сессии", f"{strat.skipped_session}")
    t.add_row("Отказов риск-слоя", f"{len(res.rejected)}")
    con.print(t)

    if a.json:
        eq = res.equity.iloc[:: max(1, len(res.equity) // 900)]
        Path(a.json).write_text(json.dumps({
            "strategy": "scalp_ha", "symbol": a.symbol, "timeframe": a.tf,
            "bars": len(df), "period": [str(df.time.iloc[0]), str(df.time.iloc[-1])],
            "params": {"rr": a.rr, "risk_pct": a.risk, "pullback": a.pullback,
                       "ema": a.ema, "doji": a.doji, "spread": a.spread},
            "metrics": m.as_dict(),
            "equity": [{"t": str(i), "v": round(float(v), 2)} for i, v in eq.items()],
            "trades": [{"side": t_.side.value, "vol": t_.volume, "pnl": round(t_.pnl, 2),
                        "reason": t_.reason, "bars": t_.bars_held} for t_ in res.trades],
        }, ensure_ascii=False), encoding="utf-8")
        con.print(f"[dim]JSON -> {a.json}[/]")


if __name__ == "__main__":
    main()
