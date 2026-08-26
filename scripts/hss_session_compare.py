"""HSS M5 plus_only: сравнение сессий 11–19 vs 16–19 + графики.

    python scripts/hss_session_compare.py

Открыть: http://127.0.0.1:8787/hss_session_compare.html
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import MetaTrader5 as mt5
import pandas as pd
from rich.console import Console
from rich.table import Table

from neft.backtest import data
from neft.core import machine_config
from neft.core import mt5_symbols as mt5sym
from neft.core.config import ROOT

from hss_cfd_report import _json_dump, run_symbol, write_chart
from hss_m5_sweep import _summ

con = Console()
OUT = ROOT / "dashboard"

PLUS = sorted(mt5sym.HSS_PLUS_SYMBOLS)


def _write_full(exp: dict, frames: dict, *, balance: float, risk: float,
                ema: int, days: int, rr: float = 1.2) -> dict:
    rows = []
    for req in exp["symbols"]:
        bro, df = frames[req]
        r = run_symbol(
            bro, df, balance=balance, risk=risk, rr=rr,
            pullback=2, session=exp["session"], ema=ema, lite=False,
            vol_mode="min", ta_filter="off",
        )
        r["requested"] = req
        r["chart"] = write_chart(
            req, r.pop("_df"), r["markers"], r["trades"], r["metrics"],
            trade_tf="M5", chart_dir=exp["chart_dir"],
        )
        r.pop("_equity", None)
        rows.append(r)
    ok = [r for r in rows if "metrics" in r]
    trades = sum(r["metrics"]["trades"] for r in ok)
    pnl = sum(r["metrics"]["net_profit"] for r in ok)
    wins = sum(r["metrics"]["wins"] for r in ok)
    losses = sum(r["metrics"]["losses"] for r in ok)
    lo, hi = exp["session"]
    report = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "strategy": "HSS",
        "days": days,
        "params": {
            "tf": "M5", "balance": balance, "risk_pct": risk,
            "rr": rr, "pullback": 2, "ema": ema,
            "session": f"{lo}-{hi}",
            "entry_mode": "stop", "vol_mode": "min",
            "ta_filter": "off", "symbols": "plus_only",
            "variant": exp["id"], "variant_title": exp["title"],
        },
        "summary": {
            "symbols": len(ok), "errors": len(rows) - len(ok),
            "trades": trades, "wins": wins, "losses": losses,
            "win_rate": (wins / trades * 100) if trades else 0.0,
            "net_profit": round(pnl, 2),
            "return_pct_sum": round(sum(r["metrics"]["return_pct"] for r in ok), 2),
        },
        "books": [{
            "symbol": r.get("requested") or r["symbol"],
            "broker": r.get("symbol"),
            "from": r.get("from"), "to": r.get("to"),
            "days": r.get("days"), "bars": r.get("bars"),
            "metrics": r.get("metrics"), "setups": r.get("setups"),
            "chart": r.get("chart"), "error": r.get("error"),
        } for r in rows],
    }
    path = OUT / exp["report"]
    path.write_text(_json_dump(report), encoding="utf-8")
    con.print(f"[dim]  → {path.name} · N={trades} · ${pnl:+,.2f}[/]")
    return report


def main() -> None:
    cfg = machine_config.load()
    hss = (cfg.get("strategies") or {}).get("hss") or {}
    balance = float(cfg.get("deposit") or 1000)
    risk = float(cfg.get("risk_pct") or 0.5)
    ema = int(hss.get("ema") or 100)
    rr = float(hss.get("rr") or 1.2)
    days = 90

    want = [s for s in PLUS if s in (cfg.get("mt5_symbols") or PLUS)] or list(PLUS)
    if not mt5.initialize():
        con.print(f"[red]MT5: {mt5.last_error()}[/]")
        sys.exit(1)
    try:
        resolved, missing = mt5sym.resolve_many(want)
        if missing:
            con.print(f"[yellow]нет в терминале: {', '.join(missing)}[/]")
        frames: dict[str, tuple[str, pd.DataFrame]] = {}
        for req, bro in resolved:
            con.print(f"[dim]кэш {req}…[/]")
            df_m1 = data.load_days(bro, "M1", days, refresh=False)
            if len(df_m1):
                end = df_m1.time.iloc[-1]
                df_m1 = df_m1[df_m1.time >= end - pd.Timedelta(days=days)].reset_index(drop=True)
            df = data.resample_ohlc(df_m1, "5min")
            if not df.empty:
                frames[req] = (bro, df)
    finally:
        mt5.shutdown()

    syms = [s for s in want if s in frames]
    experiments = [
        dict(id="sess_1119", title="M5 · 1:1.2 · 11–19 · plus_only",
             session=(11, 19), symbols=syms,
             chart_dir="hss_charts_m5_sess1119",
             report="hss_report_m5_sess1119.json"),
        dict(id="sess_1619", title="M5 · 1:1.2 · 16–19 · plus_only",
             session=(16, 19), symbols=syms,
             chart_dir="hss_charts_m5_sess1619",
             report="hss_report_m5_sess1619.json"),
    ]

    results = []
    reports = {}
    for exp in experiments:
        con.print(f"\n[cyan]▸ {exp['title']}[/]")
        rows = []
        for req in exp["symbols"]:
            bro, df = frames[req]
            r = run_symbol(
                bro, df, balance=balance, risk=risk, rr=rr,
                pullback=2, session=exp["session"], ema=ema, lite=True,
                vol_mode="min", ta_filter="off",
            )
            r["requested"] = req
            rows.append(r)
        s = _summ(rows)
        lo, hi = exp["session"]
        rec = {
            "id": exp["id"], "title": exp["title"],
            "report": exp["report"], "chart_dir": exp["chart_dir"],
            "params": {"tf": "M5", "rr": rr, "session": f"{lo}-{hi}",
                       "vol_mode": "min", "symbols": "plus_only"},
            "summary": s,
        }
        results.append(rec)
        c = "green" if s["net_profit"] >= 0 else "red"
        con.print(
            f"  [{c}]N {s['trades']} · лузеров {s['losses']} · "
            f"WR {s['win_rate']:.1f}% · Σ ${s['net_profit']:+,.2f}[/]"
        )

    t = Table(title="HSS M5 plus_only · сессии 11–19 vs 16–19 · 90д",
              box=None, pad_edge=False, title_style="bold cyan")
    for col in ("сессия", "сделок", "лузеров", "WR%", "Σ PnL"):
        t.add_column(col, justify="right" if col != "сессия" else "left")
    for e in results:
        s = e["summary"]
        c = "green" if s["net_profit"] >= 0 else "red"
        t.add_row(e["params"]["session"], str(s["trades"]), str(s["losses"]),
                  f"{s['win_rate']:.1f}", f"[{c}]{s['net_profit']:+.2f}[/]")
    con.print(t)

    winner = max(results, key=lambda e: (
        e["summary"]["net_profit"],
        e["summary"]["win_rate"],
        e["summary"]["trades"],
    ))
    con.print(f"\n[bold]лучше по PnL:[/] {winner['params']['session']} · "
              f"WR {winner['summary']['win_rate']:.1f}% · "
              f"${winner['summary']['net_profit']:+.2f}")

    con.print("\n[cyan]графики…[/]")
    for exp in experiments:
        reports[exp["id"]] = _write_full(
            exp, frames, balance=balance, risk=risk, ema=ema, days=days, rr=rr)

    by_sym: dict[str, dict] = {}
    for e in results:
        for b in e["summary"].get("books") or []:
            sym = b["symbol"]
            by_sym.setdefault(sym, {})[e["id"]] = b

    compare = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "goal": "session_compare",
        "days": days,
        "balance": balance,
        "risk_pct": risk,
        "symbols": syms,
        "winner_id": winner["id"],
        "experiments": results,
        "reports": {
            e["id"]: {"file": e["report"], "chart_dir": e["chart_dir"]}
            for e in experiments
        },
        "by_symbol": by_sym,
    }
    (OUT / "hss_session_compare.json").write_text(_json_dump(compare), encoding="utf-8")
    con.print("\n[bold green]http://127.0.0.1:8787/hss_session_compare.html[/]")


if __name__ == "__main__":
    main()
