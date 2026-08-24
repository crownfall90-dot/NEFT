"""Walk-forward: честная проверка стратегии на данных, которых она не видела.

Метод: скользящее окно. На отрезке «обучения» перебираем параметры и берём
лучший, затем применяем его к следующему отрезку, которого при подборе не было.
Считается только результат вне выборки.

Если параметры подобраны под шум, результат вне выборки будет около нуля или
хуже — независимо от того, как красиво выглядит результат на обучении.
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
from neft.strategies.scalp_ha import ScalpHA

con = Console()

WARMUP = 200          # баров на прогрев EMA100 и Heikin Ashi
GRID = [(rr, pb) for rr in (1.0, 1.5, 2.0, 3.0) for pb in (2, 3)]


def run_slice(df, spec, rr, pullback, *, balance=1000.0, risk=1.0,
              session=(16, 19), spread=1.0):
    limits = RiskLimits(risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
                        max_volume=100.0, max_daily_loss_pct=100.0,
                        max_drawdown_pct=100.0, min_free_margin_pct=0.0)
    rm = RiskManager(start_balance=balance, limits=limits)
    strat = ScalpHA(pullback_bars=pullback, rr=rr, risk_pct=risk,
                    vol_mode="min", session=session, risk_manager=rm, spec=spec)
    costs = Costs(spread_points=spread, contract_size=spec.contract_size,
                  point=spec.point)
    res = Backtester(strat, rm, costs, start_balance=balance).run(df)
    return metrics.compute(res.equity, res.trades, balance, res.ruined)


def score(m) -> float:
    """Чем ранжируем параметры на обучении.

    Матожидание на сделку, но только при достаточном числе сделок — иначе
    выигрывает конфигурация с тремя случайными удачными входами.
    """
    if m.trades < 10:
        return -999.0
    return m.expectancy


def walk(symbol: str, tf: str = "M1", bars: int = 99_000,
         is_len: int = 25_000, oos_len: int = 8_000,
         session=(16, 19), balance: float = 1000.0):
    spec = symbols.load(symbol)
    df = data.load(symbol, tf, bars).reset_index(drop=True)

    folds = []
    start = 0
    while start + is_len + oos_len <= len(df):
        is_df = df.iloc[start:start + is_len]
        oos_df = df.iloc[start + is_len - WARMUP:start + is_len + oos_len]

        best, best_score = None, -1e9
        for rr, pb in GRID:
            m = run_slice(is_df, spec, rr, pb, balance=balance, session=session)
            sc = score(m)
            if sc > best_score:
                best, best_score = (rr, pb, m), sc

        rr, pb, is_m = best
        oos_m = run_slice(oos_df, spec, rr, pb, balance=balance, session=session)
        folds.append({
            "from": str(oos_df.time.iloc[0]), "to": str(oos_df.time.iloc[-1]),
            "rr": rr, "pullback": pb,
            "is_trades": is_m.trades, "is_return": is_m.return_pct,
            "is_expectancy": is_m.expectancy,
            "oos_trades": oos_m.trades, "oos_return": oos_m.return_pct,
            "oos_wr": oos_m.win_rate, "oos_pf": oos_m.profit_factor,
            "oos_expectancy": oos_m.expectancy,
        })
        start += oos_len

    return folds


def report(symbol: str, folds: list[dict]) -> dict:
    t = Table(title=f"{symbol} · walk-forward", box=None, pad_edge=False,
              title_style="bold cyan")
    for c in ("окно OOS", "подобрано", "IS сделок", "IS доход",
              "OOS сделок", "OOS винрейт", "OOS PF", "OOS доход"):
        t.add_column(c, justify="right" if c != "окно OOS" else "left")

    compound = 1.0
    for f in folds:
        c = "green" if f["oos_return"] > 0 else "red"
        per = f"{pd.Timestamp(f['from']):%d.%m} — {pd.Timestamp(f['to']):%d.%m}"
        t.add_row(per, f"1:{f['rr']}/{f['pullback']}",
                  str(f["is_trades"]), f"{f['is_return']:+.1f}%",
                  str(f["oos_trades"]),
                  f"{f['oos_wr']:.0f}%" if f["oos_trades"] else "—",
                  f"{f['oos_pf']:.2f}" if f["oos_trades"] else "—",
                  f"[{c}]{f['oos_return']:+.2f}%[/]")
        compound *= 1 + f["oos_return"] / 100
    con.print(t)

    oos = [f["oos_return"] for f in folds]
    wins = sum(1 for r in oos if r > 0)
    trades = sum(f["oos_trades"] for f in folds)
    is_ret = sum(f["is_return"] for f in folds) / len(folds) if folds else 0
    summary = {
        "symbol": symbol, "folds": len(folds),
        "oos_total_pct": (compound - 1) * 100,
        "oos_avg_pct": sum(oos) / len(oos) if oos else 0,
        "oos_positive_folds": wins,
        "oos_trades": trades,
        "is_avg_pct": is_ret,
    }
    c = "green" if summary["oos_total_pct"] > 0 else "red"
    con.print(
        f"  окон: {len(folds)}  ·  прибыльных вне выборки: "
        f"[{'green' if wins*2>=len(folds) else 'red'}]{wins}/{len(folds)}[/]"
        f"  ·  сделок вне выборки: {trades}\n"
        f"  средний доход на обучении: {is_ret:+.2f}%  ·  "
        f"вне выборки: [{c}]{summary['oos_avg_pct']:+.2f}%[/]  ·  "
        f"накопленный вне выборки: [{c}]{summary['oos_total_pct']:+.2f}%[/]\n")
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="NAS100,DJ30,GER40")
    p.add_argument("--tf", default="M1")
    p.add_argument("--bars", type=int, default=99_000)
    p.add_argument("--is-len", type=int, default=25_000)
    p.add_argument("--oos-len", type=int, default=8_000)
    p.add_argument("--json", default="logs/walkforward.json")
    a = p.parse_args()

    out = []
    for sym in [s.strip() for s in a.symbols.split(",")]:
        try:
            folds = walk(sym, a.tf, a.bars, a.is_len, a.oos_len)
            if not folds:
                con.print(f"[yellow]{sym}: слишком мало данных для окон[/]")
                continue
            s = report(sym, folds)
            s["fold_detail"] = folds
            out.append(s)
        except Exception as e:
            con.print(f"[red]{sym}: {type(e).__name__}: {e}[/]")

    if out:
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        con.print(f"[dim]{a.json}[/]")
