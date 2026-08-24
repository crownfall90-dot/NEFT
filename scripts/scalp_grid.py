"""Сетка параметров скальпинга. Ищем, есть ли вообще устойчивая зона."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.table import Table

from neft.backtest import data
from scripts.backtest_scalp import run

con = Console()


def grid(tf: str, symbol: str = "EURUSD+", bars: int = 50_000, spread: float = 1.0):
    df = data.load(symbol, tf, bars)
    t = Table(title=f"{symbol} · {tf}  ({len(df)} баров)", box=None, pad_edge=False, title_style="bold cyan")
    for col in ("RR", "откат", "сделок", "винрейт", "б/у винрейт", "PF", "доход", "просадка"):
        t.add_column(col, justify="right" if col != "RR" else "left")

    combos = [(rr, pb) for rr in (1.0, 1.5, 2.0, 3.0) for pb in (2, 3)]
    for n, (rr, pb) in enumerate(combos, 1):
        # Каждая комбинация — это заново прогретый ScalpHA.prepare() на
        # 50к барах (несколько секунд). Без прогресса выглядит зависшим.
        con.print(f"[dim]RR 1:{rr} · откат {pb}  ({n}/{len(combos)})...[/]", end="\r")
        strat, res, m = run(df, rr=rr, pullback=pb, risk=1.0,
                            symbol=symbol, spread=spread)
        con.print(" " * 40, end="\r")
        if m.trades < 5:
            t.add_row(f"1:{rr}", str(pb), str(m.trades), "—", "—", "—", "—", "—")
            continue
        breakeven = 100 / (1 + rr)
        edge = m.win_rate - breakeven
        c = "green" if m.net_profit > 0 else "red"
        t.add_row(f"1:{rr}", str(pb), str(m.trades),
                  f"{m.win_rate:.1f}%", f"{breakeven:.1f}%",
                  f"[{c}]{m.profit_factor:.3f}[/]",
                  f"[{c}]{m.return_pct:+.2f}%[/]",
                  f"{m.max_drawdown_pct:.1f}%")
    con.print(t); con.print()


if __name__ == "__main__":
    args = sys.argv[1:]
    symbol = args[0] if args and not args[0].startswith("M") else "EURUSD+"
    tfs = [a for a in args if a.startswith("M") or a.startswith("H")] or ["M1", "M5"]
    for tf in tfs:
        try:
            grid(tf, symbol)
        except Exception as e:
            con.print(f"[red]{symbol} {tf}: {e}[/]")
