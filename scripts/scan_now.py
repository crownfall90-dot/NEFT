"""Один проход сканера: где сейчас сетап HSS / Flow / London S/R.

    python scripts/scan_now.py
    python scripts/scan_now.py --bars 8000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.table import Table

from neft.backtest import crypto_data
from neft.core.routing import CRYPTO_UNIVERSE, banned
from neft.core.scanner import Hit, rank_hits, score_flow, score_hss, score_lsr
from neft.strategies.factory import crypto_strategy
from scripts.compare_crypto import spec_for

con = Console()


def prep(symbol: str, tf: str, kind: str, bars: int, risk: float):
    df = crypto_data.load(symbol, tf, bars)
    spec = spec_for(symbol, float(df.close.median()))
    route = {"strategy": kind, "tf": tf, "session": (16, 19) if kind == "London S/R" else None}
    strat = crypto_strategy(route, rr=1.5 if kind == "Flow" else 1.0,
                            risk=risk, risk_manager=None, spec=spec)
    strat.prepare(df)
    return strat.df


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bars", type=int, default=40_000)
    p.add_argument("--risk", type=float, default=0.5)
    p.add_argument("--top", type=int, default=3)
    a = p.parse_args()
    hits: list[Hit] = []
    for sym in CRYPTO_UNIVERSE:
        if banned(sym):
            continue
        try:
            d1 = prep(sym, "1m", "HSS", a.bars, a.risk)
            sc, why = score_hss(d1)
            if sc:
                hits.append(Hit(sym, "1m", "HSS", sc, why))
            d5 = prep(sym, "5m", "Flow", min(a.bars, 8_000), a.risk)
            sc, why = score_flow(d5)
            if sc:
                hits.append(Hit(sym, "5m", "Flow", sc, why))
            sc, why = score_lsr(d5)
            if sc:
                hits.append(Hit(sym, "5m", "London S/R", sc, why))
        except Exception as e:  # noqa: BLE001
            con.print(f"[dim]{sym}: {type(e).__name__}: {e}[/]")
    top = rank_hits(hits, a.top, min_score=0.8)
    t = Table(title="Сканер вселенной (без BTC/ENA)", box=None)
    t.add_column("монета"); t.add_column("ТФ"); t.add_column("стратегия")
    t.add_column("score", justify="right"); t.add_column("почему")
    for h in sorted(hits, key=lambda x: -x.score)[:18]:
        mark = " ← топ" if any(h.symbol == x.symbol for x in top) and h.score == max(
            x.score for x in hits if x.symbol == h.symbol) else ""
        t.add_row(h.symbol.split("/")[0], h.tf, h.strategy, f"{h.score:.1f}", h.why + mark)
    con.print(t)
    if top:
        con.print("[green]сейчас в работе: " +
                  ", ".join(f"{h.symbol.split('/')[0]} {h.strategy} {h.score:.1f}"
                            for h in top) + "[/]")
    else:
        con.print("[yellow]нет монеты выше порога — ждём[/]")


if __name__ == "__main__":
    main()
