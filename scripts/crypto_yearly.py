"""Полная статистика по крипте за год: итог, по месяцам, по дням недели.

Для каждой монеты — её реально размеченные стратегии (routing.py), на своих
таймфреймах (scripts.crypto_account.route_trades). Сделки каждой монеты
сводятся в один хронологический поток и прогоняются через ту же compounding-
симуляцию, что и в crypto_account.py (scripts.account.simulate): риск на
сделку считается от ТЕКУЩЕГО капитала, а не от стартового, поэтому месячный
P&L отражает реальную траекторию счёта, а не сумму изолированных сделок.

Разбивка по месяцам показывает устойчивость во времени, разбивка по дням
недели — есть ли у крипты, торгуемой 24/7, всё же неоднородность по
будним/выходным дням (открытия биржи нет, но объём и волатильность плавают).
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

from neft.core.routing import CRYPTO_ROUTES, crypto_routes_for
from scripts.account import report, simulate
from scripts.crypto_account import route_trades

con = Console()
BAL = 1000.0
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
LABELS = {
    "BTC/USDT:USDT": "BTC", "ETH/USDT:USDT": "ETH", "BNB/USDT:USDT": "BNB",
    "XRP/USDT:USDT": "XRP", "BCH/USDT:USDT": "BCH", "HYPE/USDT:USDT": "HYPE",
    "SOL/USDT:USDT": "SOL", "DOGE/USDT:USDT": "DOGE", "ZEC/USDT:USDT": "ZEC",
    "ENA/USDT:USDT": "ENA", "SUI/USDT:USDT": "SUI", "1000PEPE/USDT:USDT": "1000PEPE",
}


def coin_taken(symbol, days, risk):
    """Все сделки монеты по всем её маршрутам, прогнанные через compounding."""
    merged, span = [], None
    for route in crypto_routes_for(symbol):
        # bars игнорируется, когда задан days — см. route_trades/crypto_data.load.
        trades = route_trades(symbol, route, 0, risk, BAL, days=days)
        for t in trades:
            t["route_label"] = f"{route['strategy']} {route['tf']}"
        merged += trades
        if trades:
            times = [t["time"] for t in trades]
            lo, hi = min(times), max(times)
            span = (lo, hi) if span is None else (min(span[0], lo), max(span[1], hi))
    merged.sort(key=lambda t: t["time"])
    if not merged:
        return [], None
    curve, taken, halted = simulate(merged, BAL, risk)
    return taken, span


def summarize(taken):
    if not taken:
        return None
    wins = [t for t in taken if t["pnl_real"] > 0]
    losses = [t for t in taken if t["pnl_real"] <= 0]
    gp = sum(t["pnl_real"] for t in wins)
    gl = -sum(t["pnl_real"] for t in losses)
    end = taken[-1]["equity"]
    return {
        "trades": len(taken), "win_rate": len(wins) / len(taken) * 100,
        "pnl": end - BAL, "pf": (gp / gl) if gl else None,
        "return_pct": (end / BAL - 1) * 100,
        "dd_estimate": max(t["dd"] for t in taken),
    }


def monthly(taken):
    if not taken:
        return {}
    df = pd.DataFrame(taken)
    df["month"] = df.time.dt.to_period("M")
    out = {}
    for m, g in df.groupby("month"):
        wins = (g.pnl_real > 0).sum()
        out[str(m)] = {"trades": len(g), "win_rate": wins / len(g) * 100,
                       "pnl": g.pnl_real.sum()}
    return out


def by_weekday(taken):
    if not taken:
        return {}
    df = pd.DataFrame(taken)
    df["wd"] = df.time.dt.weekday
    out = {}
    for wd, g in df.groupby("wd"):
        wins = (g.pnl_real > 0).sum()
        out[WEEKDAYS[wd]] = {"trades": len(g), "win_rate": wins / len(g) * 100,
                             "pnl": g.pnl_real.sum()}
    return out


def routes_used(symbol):
    return sorted({f"{r['strategy']} {r['tf']}" for r in crypto_routes_for(symbol)})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default=",".join(CRYPTO_ROUTES))
    p.add_argument("--days", type=float, default=365)
    p.add_argument("--risk", type=float, default=0.5)
    p.add_argument("--json", default="logs/crypto_yearly.json")
    a = p.parse_args()

    syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    out = {"risk_pct": a.risk, "balance": BAL, "period_days": int(a.days), "coins": {}}

    for sym in syms:
        con.print(f"\n[bold cyan]{'─'*70}[/]")
        con.print(f"[bold cyan]{sym}[/]  [dim]{', '.join(routes_used(sym))}[/]")
        try:
            taken, span = coin_taken(sym, a.days, a.risk)
        except Exception as e:
            con.print(f"  [red]{type(e).__name__}: {e}[/]")
            continue
        if not taken:
            con.print("  [dim]сделок нет[/]")
            continue

        s = summarize(taken)
        con.print(f"  период {span[0]:%Y-%m-%d} — {span[1]:%Y-%m-%d}  "
                  f"({(span[1]-span[0]).days} дней)")
        c = "green" if s["pnl"] > 0 else "red"
        wc = "green" if s["win_rate"] > 50 else "yellow"
        dc = "green" if s["dd_estimate"] <= 5 else "red"
        pf_s = f"{s['pf']:.2f}" if s["pf"] is not None else "—"
        con.print(f"  сделок [bold]{s['trades']}[/]  винрейт [{wc}]{s['win_rate']:.1f}%[/]  "
                  f"PF {pf_s}  просадка [{dc}]{s['dd_estimate']:.1f}%[/]  "
                  f"P&L [{c}]{s['pnl']:+.2f}$[/] ({s['return_pct']:+.2f}%)")

        mo = monthly(taken)
        tm = Table(box=None, pad_edge=False, show_header=True, title="по месяцам",
                  title_style="dim", padding=(0, 1))
        for c_ in ("месяц", "сделок", "винрейт", "P&L"):
            tm.add_column(c_, justify="right" if c_ != "месяц" else "left")
        for m, v in mo.items():
            cc = "green" if v["pnl"] > 0 else "red"
            wcc = "green" if v["win_rate"] > 50 else "yellow"
            tm.add_row(m, str(v["trades"]), f"[{wcc}]{v['win_rate']:.0f}%[/]",
                      f"[{cc}]{v['pnl']:+.2f}$[/]")
        con.print(tm)

        wd = by_weekday(taken)
        tw = Table(box=None, pad_edge=False, show_header=True, title="по дням недели",
                  title_style="dim", padding=(0, 1))
        for c_ in ("день", "сделок", "винрейт", "P&L"):
            tw.add_column(c_, justify="right" if c_ != "день" else "left")
        for d in WEEKDAYS:
            if d not in wd:
                continue
            v = wd[d]
            cc = "green" if v["pnl"] > 0 else "red"
            wcc = "green" if v["win_rate"] > 50 else "yellow"
            tw.add_row(d, str(v["trades"]), f"[{wcc}]{v['win_rate']:.0f}%[/]",
                      f"[{cc}]{v['pnl']:+.2f}$[/]")
        con.print(tw)

        out["coins"][sym] = {
            "label": LABELS.get(sym, sym.split("/")[0]),
            "routes": routes_used(sym),
            "period": [str(span[0])[:10], str(span[1])[:10]],
            "summary": s, "monthly": mo, "weekday": wd,
        }

    Path(a.json).write_text(json.dumps(out, ensure_ascii=False, default=str),
                            encoding="utf-8")
    con.print(f"\n[dim]{a.json}[/]")
