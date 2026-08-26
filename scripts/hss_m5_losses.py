"""HSS M5: меньше убыточных сделок при PnL ≥ 0.

    python scripts/hss_m5_losses.py
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
from hss_m5_sweep import WEAK, _summ

con = Console()
OUT = ROOT / "dashboard"

# В победителе 1:1.2·16–19 эти ещё в минусе.
DOGS = {"AUDUSD+", "USOUSD", "EURGBP+", "NAS100", "GER40", "GBPJPY+"}
# Уже были в плюсе на том же сетапе.
PLUS = {
    "DJ30", "FRA40", "XAUUSD+", "UKOUSD", "GBPUSD+",
    "USDJPY+", "USDCAD+", "USDCHF+", "EURJPY+",
}


def _pick_fewer_losses(exps: list[dict]) -> dict:
    """Среди плюсовых с ≥20 сделками — минимум лузеров, затем максимум PnL."""
    green = [e for e in exps
             if e["summary"]["net_profit"] >= 0 and e["summary"]["trades"] >= 20]
    pool = green or [e for e in exps if e["summary"]["net_profit"] >= 0] or exps
    return min(pool, key=lambda e: (
        e["summary"]["losses"],
        -e["summary"]["net_profit"],
        -e["summary"]["win_rate"],
    ))


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
        frames: dict[str, tuple[str, pd.DataFrame]] = {}
        for req, bro in resolved:
            df_m1 = data.load_days(bro, "M1", days, refresh=False)
            if len(df_m1):
                end = df_m1.time.iloc[-1]
                df_m1 = df_m1[df_m1.time >= end - pd.Timedelta(days=days)].reset_index(drop=True)
            df = data.resample_ohlc(df_m1, "5min")
            if df.empty:
                continue
            frames[req] = (bro, df)
    finally:
        mt5.shutdown()

    all_syms = list(frames)
    cut = [s for s in all_syms if s not in WEAK]
    no_aud = [s for s in cut if s != "AUDUSD+"]
    plus = [s for s in all_syms if s in PLUS]

    experiments = [
        dict(id="win_12_1619_cut", title="было: 1:1.2 · 16–19 · без слабых",
             rr=1.2, session=(16, 19), symbols=cut),
        dict(id="no_aud", title="то же − AUDUSD+",
             rr=1.2, session=(16, 19), symbols=no_aud),
        dict(id="plus_only", title="только плюсовые символы победителя",
             rr=1.2, session=(16, 19), symbols=plus),
        dict(id="volmax_12", title="1:1.2 · 16–19 · cut · vol max",
             rr=1.2, session=(16, 19), symbols=cut, vol_mode="max"),
        dict(id="volmax_plus", title="1:1.2 · 16–19 · plus · vol max",
             rr=1.2, session=(16, 19), symbols=plus, vol_mode="max"),
        dict(id="match_12", title="1:1.2 · 16–19 · cut · цвет doji",
             rr=1.2, session=(16, 19), symbols=cut, matching=True),
        dict(id="match_plus", title="1:1.2 · 16–19 · plus · цвет doji",
             rr=1.2, session=(16, 19), symbols=plus, matching=True),
        dict(id="plus_10", title="1:1 · 16–19 · только plus",
             rr=1.0, session=(16, 19), symbols=plus),
    ]

    results = []
    for exp in experiments:
        con.print(f"\n[cyan]▸ {exp['id']}[/] {exp['title']}")
        rows = []
        for req in exp["symbols"]:
            bro, df = frames[req]
            r = run_symbol(
                bro, df, balance=balance, risk=risk, rr=float(exp["rr"]),
                pullback=2, session=exp["session"], ema=ema,
                vol_mode=exp.get("vol_mode", "min"),
                require_matching_doji=bool(exp.get("matching")),
                lite=True,
            )
            r["requested"] = req
            rows.append(r)
        s = _summ(rows)
        lo, hi = exp["session"]
        rec = {
            "id": exp["id"],
            "title": exp["title"],
            "params": {
                "tf": "M5", "rr": exp["rr"], "session": f"{lo}-{hi}",
                "vol_mode": exp.get("vol_mode", "min"),
                "matching_doji": bool(exp.get("matching")),
                "symbols": exp["symbols"],
            },
            "summary": s,
        }
        results.append(rec)
        con.print(
            f"  N {s['trades']} · лузеров {s['losses']} · WR {s['win_rate']:.1f}% · "
            f"Σ ${s['net_profit']:+,.2f}"
        )

    picked = _pick_fewer_losses(results)
    best_pnl = max(results, key=lambda e: e["summary"]["net_profit"])

    sweep = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "goal": "fewer_losses",
        "winner_id": picked["id"],
        "best_pnl_id": best_pnl["id"],
        "experiments": results,
    }
    (OUT / "hss_sweep_m5.json").write_text(_json_dump(sweep), encoding="utf-8")

    t = Table(title="HSS M5 · меньше лузеров", box=None, pad_edge=False,
              title_style="bold cyan")
    for col in ("вариант", "сделок", "лузеров", "WR%", "Σ PnL"):
        t.add_column(col, justify="right" if col != "вариант" else "left")
    for e in sorted(results, key=lambda x: x["summary"]["losses"]):
        s = e["summary"]
        mark = " ★" if e["id"] == picked["id"] else ""
        c = "green" if s["net_profit"] >= 0 else "red"
        t.add_row(e["id"] + mark, str(s["trades"]), str(s["losses"]),
                  f"{s['win_rate']:.1f}", f"[{c}]{s['net_profit']:+.2f}[/]")
    con.print(t)
    con.print(f"[bold]меньше лузеров при плюсе:[/] {picked['id']} · "
              f"{picked['summary']['losses']} лузеров · "
              f"WR {picked['summary']['win_rate']:.1f}% · "
              f"${picked['summary']['net_profit']:+.2f}")

    wexp = next(x for x in experiments if x["id"] == picked["id"])
    con.print(f"\n[cyan]графики {wexp['id']}…[/]")
    rows = []
    for req in wexp["symbols"]:
        bro, df = frames[req]
        r = run_symbol(
            bro, df, balance=balance, risk=risk, rr=float(wexp["rr"]),
            pullback=2, session=wexp["session"], ema=ema,
            vol_mode=wexp.get("vol_mode", "min"),
            require_matching_doji=bool(wexp.get("matching")),
            lite=False,
        )
        r["requested"] = req
        r["chart"] = write_chart(
            req, r.pop("_df"), r["markers"], r["trades"], r["metrics"],
            trade_tf="M5",
        )
        r.pop("_equity", None)
        rows.append(r)
    ok = [r for r in rows if "metrics" in r]
    trades = sum(r["metrics"]["trades"] for r in ok)
    pnl = sum(r["metrics"]["net_profit"] for r in ok)
    wins = sum(r["metrics"]["wins"] for r in ok)
    losses = sum(r["metrics"]["losses"] for r in ok)
    lo, hi = wexp["session"]
    report = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "strategy": "HSS",
        "days": days,
        "winner_of": "hss_m5_losses",
        "params": {
            "tf": "M5", "balance": balance, "risk_pct": risk,
            "rr": wexp["rr"], "pullback": 2, "ema": ema,
            "session": f"{lo}-{hi}",
            "entry_mode": "stop",
            "vol_mode": wexp.get("vol_mode", "min"),
            "matching_doji": bool(wexp.get("matching")),
            "ta_filter": "off",
            "variant": wexp["id"],
            "variant_title": wexp["title"],
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
            "metrics": r.get("metrics"),
            "setups": r.get("setups"),
            "chart": r.get("chart"), "error": r.get("error"),
        } for r in rows],
    }
    (OUT / "hss_report_m5.json").write_text(_json_dump(report), encoding="utf-8")
    con.print("[dim]http://127.0.0.1:8787/hss_report_m5.html[/]")
    con.print("[dim]http://127.0.0.1:8787/hss_sweep_m5.html[/]")


if __name__ == "__main__":
    main()
