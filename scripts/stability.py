"""Устойчивость по периодам: одни и те же правила на последовательных отрезках.

Параметры не подбираются — они фиксированы автором стратегии. Проверяем только,
повторяется ли результат во времени. Если эдж реальный, он виден в большинстве
отрезков, а не в одном удачном.
"""
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
WARMUP = 300


def periods(symbol, tf="M1", bars=99_000, chunks=6, rr=1.0, session=(16, 19)):
    df = data.load(symbol, tf, bars).reset_index(drop=True)
    size = len(df) // chunks
    rows = []
    for k in range(chunks):
        lo = max(0, k * size - WARMUP)
        hi = (k + 1) * size
        part = df.iloc[lo:hi]
        st, res, m = run(part, rr=rr, pullback=2, risk=1.0,
                         symbol=symbol, session=session)
        rows.append({
            "from": part.time.iloc[WARMUP if k else 0], "to": part.time.iloc[-1],
            "trades": m.trades, "wr": m.win_rate, "pf": m.profit_factor,
            "ret": m.return_pct, "dd": m.max_drawdown_pct,
        })
    return rows


if __name__ == "__main__":
    syms = sys.argv[1:] or ["DJ30", "NAS100", "EURUSD+"]
    for sym in syms:
        con.print(f"[dim]{sym} считаю...[/]", end="\r")
        try:
            rows = periods(sym)
        except Exception as e:
            con.print(f"[red]{sym}: {e}[/]" + " " * 20); continue
        con.print(" " * 40, end="\r")
        t = Table(title=f"{sym} · RR 1:1 · шесть последовательных отрезков",
                  box=None, pad_edge=False, title_style="bold cyan")
        for c in ("период", "сделок", "винрейт", "PF", "доход", "просадка"):
            t.add_column(c, justify="right" if c != "период" else "left")
        pos = tot = 0
        for r in rows:
            c = "green" if r["ret"] > 0 else "red"
            t.add_row(f"{r['from']:%d.%m} — {r['to']:%d.%m}", str(r["trades"]),
                      f"{r['wr']:.0f}%" if r["trades"] else "—",
                      f"{r['pf']:.2f}" if r["trades"] else "—",
                      f"[{c}]{r['ret']:+.2f}%[/]", f"{r['dd']:.1f}%")
            pos += r["ret"] > 0
            tot += r["trades"]
        con.print(t)
        allwr = sum(r["wr"] * r["trades"] for r in rows) / tot if tot else 0
        c = "green" if pos * 2 >= len(rows) else "red"
        con.print(f"  прибыльных отрезков: [{c}]{pos}/{len(rows)}[/]  ·  "
                  f"всего сделок: {tot}  ·  средневзвешенный винрейт: "
                  f"[bold]{allwr:.1f}%[/]\n")
