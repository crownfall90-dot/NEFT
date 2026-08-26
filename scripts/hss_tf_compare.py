"""HSS на M15 и H1 — те же правила, 90д CFD.

    python scripts/hss_tf_compare.py
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

from hss_cfd_report import (
    REPORT_BY_TF, TF_RULE, _json_dump, run_symbol, write_chart,
)
from hss_m5_sweep import WEAK, _summ

con = Console()
OUT = ROOT / "dashboard"


def _pick(exps: list[dict]) -> dict:
    thick = [e for e in exps if e["summary"]["trades"] >= 8]
    pool = thick or exps
    green = [e for e in pool if e["summary"]["net_profit"] >= 0]
    use = green or pool
    return max(use, key=lambda e: (
        e["summary"]["net_profit"],
        e["summary"]["win_rate"],
        e["summary"]["trades"],
    ))


def _write_full(exp: dict, frames: dict, balance: float, risk: float, ema: int,
                days: int) -> None:
    trade_tf = exp["tf"]
    rows = []
    for req in exp["symbols"]:
        bro, by_tf = frames[req]
        df = by_tf[trade_tf]
        r = run_symbol(
            bro, df, balance=balance, risk=risk, rr=float(exp["rr"]),
            pullback=2, session=exp["session"], ema=ema, lite=False,
        )
        r["requested"] = req
        r["chart"] = write_chart(
            req, r.pop("_df"), r["markers"], r["trades"], r["metrics"],
            trade_tf=trade_tf,
        )
        r.pop("_equity", None)
        rows.append(r)
    ok = [r for r in rows if "metrics" in r]
    trades = sum(r["metrics"]["trades"] for r in ok)
    pnl = sum(r["metrics"]["net_profit"] for r in ok)
    wins = sum(r["metrics"]["wins"] for r in ok)
    losses = sum(r["metrics"]["losses"] for r in ok)
    sess = "24h" if exp["session"] is None else f"{exp['session'][0]}-{exp['session'][1]}"
    report = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "strategy": "HSS",
        "days": days,
        "params": {
            "tf": trade_tf, "balance": balance, "risk_pct": risk,
            "rr": exp["rr"], "pullback": 2, "ema": ema, "session": sess,
            "entry_mode": "stop", "vol_mode": "min",
            "variant": exp["id"], "variant_title": exp["title"],
        },
        "summary": {
            "symbols": len(ok), "errors": 0, "trades": trades,
            "wins": wins, "losses": losses,
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
    path = OUT / REPORT_BY_TF[trade_tf]
    path.write_text(_json_dump(report), encoding="utf-8")
    con.print(f"[dim]графики {trade_tf} → {path.name}[/]")


def main() -> None:
    cfg = machine_config.load()
    hss = (cfg.get("strategies") or {}).get("hss") or {}
    balance = float(cfg.get("deposit") or 1000)
    risk = float(cfg.get("risk_pct") or 0.5)
    ema = int(hss.get("ema") or 100)
    days = 90

    want = list(cfg.get("mt5_symbols") or mt5sym.CFD_SYMBOLS)
    if not mt5.initialize():
        con.print(f"[red]MT5: {mt5.last_error()}[/]")
        sys.exit(1)
    try:
        resolved, missing = mt5sym.resolve_many(want)
        if missing:
            con.print(f"[yellow]нет в терминале: {', '.join(missing)}[/]")
        frames: dict[str, tuple[str, dict]] = {}
        for req, bro in resolved:
            df_m1 = data.load_days(bro, "M1", days, refresh=False)
            if len(df_m1):
                end = df_m1.time.iloc[-1]
                df_m1 = df_m1[df_m1.time >= end - pd.Timedelta(days=days)].reset_index(drop=True)
            by_tf = {}
            for tf, rule in (("M15", TF_RULE["M15"]), ("H1", TF_RULE["H1"])):
                d = data.resample_ohlc(df_m1, rule)
                if d.empty:
                    continue
                by_tf[tf] = d
            if by_tf:
                frames[req] = (bro, by_tf)
    finally:
        mt5.shutdown()

    all_syms = list(frames)
    cut = [s for s in all_syms if s not in WEAK]

    experiments = [
        dict(id="m15_12_1619_cut", title="M15 · 1:1.2 · 16–19 · без слабых",
             tf="M15", rr=1.2, session=(16, 19), symbols=cut),
        dict(id="m15_12_1020_cut", title="M15 · 1:1.2 · 10–20 · без слабых",
             tf="M15", rr=1.2, session=(10, 20), symbols=cut),
        dict(id="m15_12_820_cut", title="M15 · 1:1.2 · 8–20 · без слабых",
             tf="M15", rr=1.2, session=(8, 20), symbols=cut),
        dict(id="m15_10_1619_cut", title="M15 · 1:1 · 16–19 · без слабых",
             tf="M15", rr=1.0, session=(16, 19), symbols=cut),
        dict(id="m15_12_1619_all", title="M15 · 1:1.2 · 16–19 · все CFD",
             tf="M15", rr=1.2, session=(16, 19), symbols=all_syms),
        dict(id="h1_12_1619_cut", title="H1 · 1:1.2 · 16–19 · без слабых",
             tf="H1", rr=1.2, session=(16, 19), symbols=cut),
        dict(id="h1_12_1020_cut", title="H1 · 1:1.2 · 10–20 · без слабых",
             tf="H1", rr=1.2, session=(10, 20), symbols=cut),
        dict(id="h1_12_820_cut", title="H1 · 1:1.2 · 8–20 · без слабых",
             tf="H1", rr=1.2, session=(8, 20), symbols=cut),
        dict(id="h1_10_820_cut", title="H1 · 1:1 · 8–20 · без слабых",
             tf="H1", rr=1.0, session=(8, 20), symbols=cut),
        dict(id="h1_12_24h_cut", title="H1 · 1:1.2 · 24ч · без слабых",
             tf="H1", rr=1.2, session=None, symbols=cut),
        dict(id="h1_12_820_all", title="H1 · 1:1.2 · 8–20 · все CFD",
             tf="H1", rr=1.2, session=(8, 20), symbols=all_syms),
    ]

    results = []
    for exp in experiments:
        con.print(f"\n[cyan]▸ {exp['id']}[/] {exp['title']}")
        rows = []
        for req in exp["symbols"]:
            bro, by_tf = frames[req]
            df = by_tf.get(exp["tf"])
            if df is None or df.empty:
                continue
            r = run_symbol(
                bro, df, balance=balance, risk=risk, rr=float(exp["rr"]),
                pullback=2, session=exp["session"], ema=ema, lite=True,
            )
            r["requested"] = req
            rows.append(r)
        s = _summ(rows)
        rec = {"id": exp["id"], "title": exp["title"], "tf": exp["tf"],
               "params": {"rr": exp["rr"],
                          "session": "24h" if exp["session"] is None
                          else f"{exp['session'][0]}-{exp['session'][1]}"},
               "summary": s}
        results.append(rec)
        c = "green" if s["net_profit"] >= 0 else "red"
        con.print(
            f"  [{c}]N {s['trades']} · лузеров {s['losses']} · "
            f"WR {s['win_rate']:.1f}% · Σ ${s['net_profit']:+,.2f}[/]"
        )

    t = Table(title="HSS · M15 vs H1 · 90д CFD", box=None, pad_edge=False,
              title_style="bold cyan")
    for col in ("вариант", "ТФ", "сделок", "лузеров", "WR%", "Σ PnL"):
        t.add_column(col, justify="right" if col != "вариант" else "left")
    for e in sorted(results, key=lambda x: -x["summary"]["net_profit"]):
        s = e["summary"]
        c = "green" if s["net_profit"] >= 0 else "red"
        t.add_row(e["id"], e["tf"], str(s["trades"]), str(s["losses"]),
                  f"{s['win_rate']:.1f}", f"[{c}]{s['net_profit']:+.2f}[/]")
    con.print(t)

    best_m15 = _pick([e for e in results if e["tf"] == "M15"])
    best_h1 = _pick([e for e in results if e["tf"] == "H1"])
    con.print(f"[bold]лучший M15:[/] {best_m15['id']} · "
              f"WR {best_m15['summary']['win_rate']:.1f}% · "
              f"${best_m15['summary']['net_profit']:+.2f}")
    con.print(f"[bold]лучший H1:[/] {best_h1['id']} · "
              f"WR {best_h1['summary']['win_rate']:.1f}% · "
              f"${best_h1['summary']['net_profit']:+.2f}")

    sweep = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "goal": "m15_h1",
        "winner_m15": best_m15["id"],
        "winner_h1": best_h1["id"],
        "experiments": results,
    }
    (OUT / "hss_tf_compare.json").write_text(_json_dump(sweep), encoding="utf-8")

    exp_m15 = next(x for x in experiments if x["id"] == best_m15["id"])
    exp_h1 = next(x for x in experiments if x["id"] == best_h1["id"])
    _write_full(exp_m15, frames, balance, risk, ema, days)
    _write_full(exp_h1, frames, balance, risk, ema, days)
    con.print("[dim]http://127.0.0.1:8787/hss_report_m15.html[/]")
    con.print("[dim]http://127.0.0.1:8787/hss_report_h1.html[/]")


if __name__ == "__main__":
    main()
