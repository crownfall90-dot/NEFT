"""Один счёт $1000 на несколько инструментов.

Бэктест каждого инструмента даёт свою кривую. Реальный счёт — один: сделки
идут вперемешку по времени, а просадка считается по общему капиталу. Именно
поэтому диверсификация снижает просадку: убыток на одном инструменте часто
приходится на прибыль другого.

Здесь сделки всех инструментов сводятся в один хронологический поток, риск
на каждую считается от ТЕКУЩЕГО общего капитала, а не от стартового.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from rich.console import Console
from rich.table import Table

from neft.backtest import data
from neft.backtest.engine import Backtester, Costs
from neft.core import symbols
from neft.core.portfolio import Portfolio
from neft.core.risk import RiskLimits, RiskManager
from neft.core.routing import BREAKOUT_RR, enabled_for
from neft.strategies.london_breakout import LondonBreakout
from neft.strategies.london_sr import LondonSR
from neft.strategies.scalp_ha import ScalpHA

con = Console()


def breakout_trades(symbol, bars, risk, balance):
    """Пробой лондонского бокса живёт на M5 — отдельный прогон, общий счёт."""
    if not enabled_for(symbol).get("Breakout", False):
        return []
    spec = symbols.load(symbol)
    df = data.load(symbol, "M5", bars).reset_index(drop=True)
    limits = RiskLimits(risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
                        max_volume=100.0, max_daily_loss_pct=100.0,
                        max_drawdown_pct=100.0, min_free_margin_pct=0.0)
    rm = RiskManager(start_balance=balance, limits=limits)
    strat = LondonBreakout(rr=BREAKOUT_RR.get(symbol, 1.5), risk_pct=risk,
                           risk_manager=rm, spec=spec)
    costs = Costs(spread_points=1.0, contract_size=spec.contract_size,
                  point=spec.point)
    res = Backtester(strat, rm, costs, start_balance=balance).run(df)

    # Время берём напрямую из ClosedTrade — движок пишет его при закрытии
    # позиции, никакой реконструкции по кривой эквити не нужно.
    out = []
    for t in res.trades:
        out.append({
            "symbol": symbol, "strategy": "Breakout", "pnl": t.pnl,
            "reason": t.reason, "bars": t.bars_held, "risk_frac": risk / 100,
            "r_multiple": t.pnl / (balance * risk / 100),
            "time": t.closed_at,
        })
    return out


def trades_of(symbol, bars, risk, balance):
    """Сделки одного инструмента с отметками времени входа и выхода."""
    spec = symbols.load(symbol)
    df = data.load(symbol, "M1", bars).reset_index(drop=True)
    limits = RiskLimits(risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
                        max_volume=100.0, max_daily_loss_pct=100.0,
                        max_drawdown_pct=100.0, min_free_margin_pct=0.0)
    rm = RiskManager(start_balance=balance, limits=limits)
    pf = Portfolio()
    flags = enabled_for(symbol)
    if flags.get("HSS", True):
        pf.add(ScalpHA(rr=1.0, pullback_bars=2, session=(16, 19), vol_mode="min",
                       vol_window=2, entry_mode="stop",
                       risk_pct=risk, risk_manager=rm, spec=spec), "HSS")
    if flags.get("London S/R", True):
        pf.add(LondonSR(london=(11, 16), ny=(16, 23), min_rr=1.0,
                        risk_pct=risk, risk_manager=rm, spec=spec), "London S/R")
    if not pf.slots:
        return []

    costs = Costs(spread_points=1.0, contract_size=spec.contract_size,
                  point=spec.point)
    res = Backtester(pf, rm, costs, start_balance=balance).run(df)

    out = []
    for t in res.trades:
        risk_frac = risk / 100
        out.append({
            "symbol": symbol, "strategy": "M1", "pnl": t.pnl, "reason": t.reason,
            "bars": t.bars_held, "risk_frac": risk_frac,
            "r_multiple": t.pnl / (balance * risk_frac) if risk_frac else 0,
            "time": t.closed_at,
        })
    return out


def clip(all_trades):
    """Обрезает все потоки до ОБЩЕГО окна.

    Стратегии живут на разных таймфреймах, и терминал отдаёт по 99 000 баров
    на каждый — поэтому M5 покрывает в пять раз больший период, чем M1.
    Складывать их напрямую нельзя: получится счёт, где половину времени
    работала часть стратегий. Сравнивать можно только на пересечении.
    """
    if not all_trades:
        return all_trades, None, None
    by_src = {}
    for t in all_trades:
        by_src.setdefault((t["symbol"], t.get("strategy", "M1")), []).append(t["time"])
    start = max(min(v) for v in by_src.values())
    end = min(max(v) for v in by_src.values())
    return [t for t in all_trades if start <= t["time"] <= end], start, end


def simulate(all_trades, balance, risk, dd_stop=None):
    """Прогоняет объединённый поток сделок по одному счёту."""
    all_trades = sorted(all_trades, key=lambda t: t["time"])
    eq, peak, halted = balance, balance, False
    curve, taken = [], []
    for t in all_trades:
        if halted:
            break
        # риск считается от текущего капитала — так работает реальный счёт
        pnl = t["r_multiple"] * eq * (risk / 100)
        eq += pnl
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100
        curve.append({"time": t["time"], "equity": eq, "dd": dd})
        taken.append({**t, "pnl_real": pnl, "equity": eq, "dd": dd})
        if dd_stop is not None and dd >= dd_stop:
            halted = True
    return pd.DataFrame(curve), taken, halted


def report(name, curve, taken, balance, halted):
    if not taken:
        return None
    wins = sum(1 for t in taken if t["pnl_real"] > 0)
    end = curve.equity.iloc[-1]
    dd = curve.dd.max()
    wr = wins / len(taken) * 100
    return {
        "name": name, "trades": len(taken), "win_rate": wr,
        "dd": dd, "end": end, "ret": (end / balance - 1) * 100,
        "halted": halted,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--bars", type=int, default=99_000)
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--dd-stop", type=float, default=None,
                   help="остановить торговлю при просадке, %%")
    a = p.parse_args()

    pool = {}
    for sym in ("NAS100", "DJ30", "EURUSD+", "XAUUSD+", "GER40"):
        try:
            m1 = trades_of(sym, a.bars, 1.0, a.balance)
            m5 = breakout_trades(sym, a.bars, 1.0, a.balance)
            pool[sym] = m1 + m5
            con.print(f"[dim]{sym}: {len(m1)} M1 + {len(m5)} пробой = "
                      f"{len(pool[sym])} сделок[/]")
        except Exception as e:
            con.print(f"[red]{sym}: {e}[/]")

    sets = {
        "DJ30 один": ["DJ30"],
        "DJ30 + NAS100": ["DJ30", "NAS100"],
        "DJ30 + EURUSD+": ["DJ30", "EURUSD+"],
        "DJ30 + NAS100 + EURUSD+": ["DJ30", "NAS100", "EURUSD+"],
        "все пять": ["NAS100", "DJ30", "EURUSD+", "XAUUSD+", "GER40"],
    }

    t = Table(title=f"ОДИН СЧЁТ ${a.balance:.0f} · несколько инструментов "
                    f"(депозит — единица сравнения тестов)",
              box=None, pad_edge=False, title_style="bold cyan")
    for c in ("состав", "окно", "риск", "сделок", "винрейт", "просадка", "итог $",
              "доход"):
        t.add_column(c, justify="right" if c != "состав" else "left")

    for label, syms in sets.items():
        merged = [x for s in syms for x in pool.get(s, [])]
        merged, w0, w1 = clip(merged)
        if not merged:
            continue
        window = f"{w0:%d.%m.%y}—{w1:%d.%m.%y}" if w0 is not None else ""
        first = True
        for risk in (0.5, 0.75, 1.0):
            curve, taken, halted = simulate(merged, a.balance, risk, a.dd_stop)
            r = report(label, curve, taken, a.balance, halted)
            if r is None:
                continue
            c = "green" if r["ret"] > 0 else "red"
            t.add_row(label if first else "", window if first else "",
                      f"{risk}%", str(r["trades"]),
                      f"{r['win_rate']:.1f}%", f"{r['dd']:.1f}%",
                      f"{r['end']:,.0f}", f"[{c}]{r['ret']:+.2f}%[/]")
            first = False
    con.print(t)
