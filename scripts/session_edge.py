"""Есть ли у сигнала эдж в конкретные часы (Kill Zones)?

Считаем не прибыль стратегии, а поведение самого сигнала по часам:
сколько сетапов и какой винрейт при фиксированном RR.
"""
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
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.scalp_ha import ScalpHA

con = Console()


def by_hour(symbol="NAS100", tf="M1", rr=1.5, pullback=3):
    spec = symbols.load(symbol)
    df = data.load(symbol, tf, 50_000)
    limits = RiskLimits(risk_per_trade_pct=1.0, max_risk_per_trade_pct=3.0,
                        max_volume=100.0, max_daily_loss_pct=100.0,
                        max_drawdown_pct=100.0, min_free_margin_pct=0.0)
    rm = RiskManager(start_balance=1000.0, limits=limits)
    strat = ScalpHA(pullback_bars=pullback, rr=rr, risk_pct=1.0,
                    risk_manager=rm, spec=spec)
    res = Backtester(strat, rm,
                     Costs(spread_points=1.0, contract_size=spec.contract_size,
                           point=spec.point),
                     start_balance=1000.0).run(df)

    # Восстанавливаем час входа каждой сделки по индексу открытия.
    prepared = strat.df.reset_index(drop=True)
    rows = []
    idx = 0
    for t in res.trades:
        # bars_held назад от текущей позиции не даёт вход напрямую,
        # поэтому идём по журналу последовательно.
        rows.append(t)
    # Проще: пересчитываем сигналы отдельно и смотрим их часы.
    sig_hours, sig_win = [], []
    return res, strat, prepared


def signal_stats(symbol="NAS100", tf="M1", rr=1.5, pullback=2,
                 doji=0.25, lookahead=400):
    """Для каждого сетапа смотрим, что случилось раньше: стоп или тейк."""
    spec = symbols.load(symbol)
    df = data.load(symbol, tf, 50_000)
    rm = RiskManager(start_balance=1000.0,
                     limits=RiskLimits(max_volume=100.0, max_daily_loss_pct=100.0,
                                       max_drawdown_pct=100.0, min_free_margin_pct=0.0))
    strat = ScalpHA(pullback_bars=pullback, rr=rr, risk_pct=1.0,
                    doji_body_pct=doji, risk_manager=rm, spec=spec)
    d = strat.prepare(df).reset_index(drop=True)
    strat.set_equity(1000.0)

    recs = []
    high, low = d.high.to_numpy(), d.low.to_numpy()
    for i in range(strat.ema_period + 5, len(d) - lookahead):
        from neft.core.strategy import Bar
        r = d.iloc[i]
        sig = strat.on_bar(Bar(r.time, r.open, r.high, r.low, r.close,
                               r.spread, index=i, volume=r.tick_volume), False)
        if sig is None:
            continue
        buy = sig.side.value == "buy"
        won = None
        for j in range(i + 1, i + lookahead):
            hit_sl = low[j] <= sig.sl if buy else high[j] >= sig.sl
            hit_tp = high[j] >= sig.tp if buy else low[j] <= sig.tp
            if hit_sl:
                won = False; break
            if hit_tp:
                won = True; break
        if won is None:
            continue
        recs.append({"hour": r.time.hour, "side": sig.side.value, "won": won})

    s = pd.DataFrame(recs)
    breakeven = 100 / (1 + rr)
    t = Table(title=f"{symbol} {tf} · RR 1:{rr} · откат {pullback} — итог по часам "
                    f"(б/у {breakeven:.1f}%)",
              box=None, pad_edge=False, title_style="bold cyan")
    for c in ("час", "сетапов", "винрейт", "эдж", "матож. R"):
        t.add_column(c, justify="right" if c != "час" else "left")
    for h, g in s.groupby("hour"):
        if len(g) < 12:
            continue
        wr = g.won.mean() * 100
        edge = wr - breakeven
        exp_r = g.won.mean() * rr - (1 - g.won.mean())
        c = "green" if edge > 0 else "red"
        t.add_row(f"{h:02d}:00", str(len(g)), f"{wr:.1f}%",
                  f"[{c}]{edge:+.1f} п.п.[/]", f"[{c}]{exp_r:+.3f}[/]")
    con.print(t)
    tot = s.won.mean()
    con.print(f"\n[bold]Всего сетапов: {len(s)} · общий винрейт {tot*100:.1f}% "
              f"· безубыток {breakeven:.1f}% · матожидание {tot*rr-(1-tot):+.3f}R[/]")
    return s


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "NAS100"
    tf = sys.argv[2] if len(sys.argv) > 2 else "M1"
    signal_stats(sym, tf)
