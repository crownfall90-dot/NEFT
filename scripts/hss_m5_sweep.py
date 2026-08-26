"""Матрица HSS M5: рекомендации + свои варианты. Лучший → hss_report_m5.

    python scripts/hss_m5_sweep.py
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

con = Console()
OUT = ROOT / "dashboard"

WEAK = {"ES35", "UK100", "CHINA50", "NZDUSD+", "EURUSD+"}
CORE = {"DJ30", "FRA40", "USOUSD", "GBPUSD+", "USDCAD+", "USDCHF+"}


def _summ(rows: list[dict]) -> dict:
    ok = [r for r in rows if "metrics" in r]
    trades = sum(r["metrics"]["trades"] for r in ok)
    wins = sum(r["metrics"]["wins"] for r in ok)
    losses = sum(r["metrics"]["losses"] for r in ok)
    pnl = sum(r["metrics"]["net_profit"] for r in ok)
    return {
        "symbols": len(ok),
        "errors": len(rows) - len(ok),
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / trades * 100) if trades else 0.0,
        "net_profit": round(pnl, 2),
        "return_pct_sum": round(sum(r["metrics"]["return_pct"] for r in ok), 2),
        "books": [{
            "symbol": r.get("requested") or r["symbol"],
            "trades": r["metrics"]["trades"],
            "win_rate": r["metrics"]["win_rate"],
            "return_pct": r["metrics"]["return_pct"],
            "profit_factor": r["metrics"]["profit_factor"],
            "net_profit": r["metrics"]["net_profit"],
        } for r in ok],
    }


def _pick(exps: list[dict]) -> dict:
    """Лучшее решение: сначала плюс/мин. минус, при равенстве — WR и число сделок."""
    thick = [e for e in exps if e["summary"]["trades"] >= 20]
    pool = thick or exps
    green = [e for e in pool if e["summary"]["net_profit"] >= 0]
    use = green or pool
    return max(use, key=lambda e: (
        e["summary"]["net_profit"],
        e["summary"]["win_rate"],
        e["summary"]["trades"],
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
            con.print(f"[dim]кэш {req}…[/]")
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
    core = [s for s in all_syms if s in CORE]

    experiments = [
        dict(id="base_rr15_1020_all", title="было: 1:1.5 · 10–20 · все CFD",
             rr=1.5, session=(10, 20), pullback=2, symbols=all_syms),
        dict(id="rec_rr10_1020_all", title="рек.1: R:R 1:1 · 10–20 · все",
             rr=1.0, session=(10, 20), pullback=2, symbols=all_syms),
        dict(id="rec_rr10_1020_cut", title="рек.2: 1:1 · 10–20 · без слабых",
             rr=1.0, session=(10, 20), pullback=2, symbols=cut),
        dict(id="rec_rr10_1619_all", title="рек.3: 1:1 · Kill Zone 16–19 · все",
             rr=1.0, session=(16, 19), pullback=2, symbols=all_syms),
        dict(id="rec_rr10_1619_cut", title="рек.все: 1:1 · 16–19 · без слабых",
             rr=1.0, session=(16, 19), pullback=2, symbols=cut),
        dict(id="rec_rr10_1619_core", title="рек.ядро: 1:1 · 16–19 · высокий WR",
             rr=1.0, session=(16, 19), pullback=2, symbols=core),
        dict(id="idea_match_1619_cut", title="идея: цвет doji = направление",
             rr=1.0, session=(16, 19), pullback=2, symbols=cut,
             matching=True),
        dict(id="idea_volmax_1619_cut", title="идея: объём doji ≥ max(1–3)",
             rr=1.0, session=(16, 19), pullback=2, symbols=cut,
             vol_mode="max"),
        dict(id="idea_london_1116_cut", title="идея: только Лондон 11–16",
             rr=1.0, session=(11, 16), pullback=2, symbols=cut),
        dict(id="idea_ny_1620_cut", title="идея: NY 16–20",
             rr=1.0, session=(16, 20), pullback=2, symbols=cut),
        dict(id="idea_pb3_1619_cut", title="идея: pullback 3",
             rr=1.0, session=(16, 19), pullback=3, symbols=cut),
        dict(id="idea_cross5_1619_cut", title="идея: ≥5 баров после EMA-cross",
             rr=1.0, session=(16, 19), pullback=2, symbols=cut,
             min_cross=5),
        dict(id="idea_rr12_1619_cut", title="идея: R:R 1:1.2 · 16–19 · без слабых",
             rr=1.2, session=(16, 19), pullback=2, symbols=cut),
        dict(id="idea_rr10_820_cut", title="идея: 1:1 · 8–20 · без слабых",
             rr=1.0, session=(8, 20), pullback=2, symbols=cut),
        dict(id="ta_sr_1020_cut", title="ТА: doji у S/R · 1:1 · 10–20 · без слабых",
             rr=1.0, session=(10, 20), pullback=2, symbols=cut, ta_filter="sr"),
        dict(id="ta_sr_1619_cut", title="ТА: doji у S/R · 1:1 · 16–19 · без слабых",
             rr=1.0, session=(16, 19), pullback=2, symbols=cut, ta_filter="sr"),
        dict(id="ta_sr_rsi_1020_cut", title="ТА: S/R + RSI · 1:1 · 10–20 · без слабых",
             rr=1.0, session=(10, 20), pullback=2, symbols=cut, ta_filter="sr_rsi"),
        dict(id="ta_sr_room_1020_cut", title="ТА: S/R + место до TP · 1:1 · 10–20",
             rr=1.0, session=(10, 20), pullback=2, symbols=cut, ta_filter="sr_room"),
        dict(id="ta_full_1020_cut", title="ТА: S/R+RSI+room · 1:1 · 10–20 · без слабых",
             rr=1.0, session=(10, 20), pullback=2, symbols=cut, ta_filter="full"),
        dict(id="ta_sr_1020_all", title="ТА: doji у S/R · 1:1 · 10–20 · все CFD",
             rr=1.0, session=(10, 20), pullback=2, symbols=all_syms, ta_filter="sr"),
    ]

    results = []
    for exp in experiments:
        con.print(f"\n[cyan]▸ {exp['id']}[/] {exp['title']}")
        rows = []
        for req in exp["symbols"]:
            bro, df = frames[req]
            try:
                r = run_symbol(
                    bro, df, balance=balance, risk=risk, rr=float(exp["rr"]),
                    pullback=int(exp["pullback"]), session=exp["session"],
                    ema=ema,
                    vol_mode=exp.get("vol_mode", "min"),
                    require_matching_doji=bool(exp.get("matching")),
                    min_bars_since_cross=int(exp.get("min_cross") or 0),
                    ta_filter=exp.get("ta_filter", "off"),
                    lite=True,
                )
            except Exception as e:
                con.print(f"[red]  {req}: {e}[/]")
                rows.append({"symbol": req, "error": str(e)})
                continue
            r["requested"] = req
            rows.append(r)
        s = _summ(rows)
        lo, hi = exp["session"]
        rec = {
            "id": exp["id"],
            "title": exp["title"],
            "params": {
                "tf": "M5", "rr": exp["rr"], "session": f"{lo}-{hi}",
                "pullback": exp["pullback"],
                "vol_mode": exp.get("vol_mode", "min"),
                "matching_doji": bool(exp.get("matching")),
                "min_bars_since_cross": int(exp.get("min_cross") or 0),
                "ta_filter": exp.get("ta_filter", "off"),
                "symbols": exp["symbols"],
            },
            "summary": s,
        }
        results.append(rec)
        c = "green" if s["net_profit"] >= 0 else "red"
        con.print(
            f"  [{c}]N {s['trades']} · WR {s['win_rate']:.1f}% · "
            f"Σ ${s['net_profit']:+,.2f} · символов {s['symbols']}[/]"
        )

    winner = _pick(results)
    best_wr = max(
        (e for e in results if e["summary"]["trades"] >= 30),
        key=lambda e: e["summary"]["win_rate"],
        default=winner,
    )

    sweep = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "strategy": "HSS",
        "tf": "M5",
        "days": days,
        "balance": balance,
        "risk_pct": risk,
        "winner_id": winner["id"],
        "best_wr_id": best_wr["id"],
        "experiments": results,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "hss_sweep_m5.json").write_text(_json_dump(sweep), encoding="utf-8")

    t = Table(title="HSS M5 · 90д · сравнение вариантов",
              box=None, pad_edge=False, title_style="bold cyan")
    for col in ("вариант", "сделок", "WR%", "Σ PnL", "симв."):
        t.add_column(col, justify="right" if col != "вариант" else "left")
    for e in sorted(results, key=lambda x: -x["summary"]["net_profit"]):
        s = e["summary"]
        mark = " ★" if e["id"] == winner["id"] else ""
        c = "green" if s["net_profit"] >= 0 else "red"
        t.add_row(
            e["id"] + mark,
            str(s["trades"]),
            f"{s['win_rate']:.1f}",
            f"[{c}]{s['net_profit']:+.2f}[/]",
            str(s["symbols"]),
        )
    con.print(t)
    con.print(f"[bold]лучший PnL:[/] {winner['id']} · {winner['title']}")
    con.print(f"[bold]лучший WR (≥30 сделок):[/] {best_wr['id']} · "
              f"WR {best_wr['summary']['win_rate']:.1f}%")

    # Полный отчёт + графики для победителя.
    wexp = next(x for x in experiments if x["id"] == winner["id"])
    con.print(f"\n[cyan]графики победителя {wexp['id']}…[/]")
    rows = []
    for req in wexp["symbols"]:
        bro, df = frames[req]
        r = run_symbol(
            bro, df, balance=balance, risk=risk, rr=float(wexp["rr"]),
            pullback=int(wexp["pullback"]), session=wexp["session"],
            ema=ema,
            vol_mode=wexp.get("vol_mode", "min"),
            require_matching_doji=bool(wexp.get("matching")),
            min_bars_since_cross=int(wexp.get("min_cross") or 0),
            ta_filter=wexp.get("ta_filter", "off"),
            lite=False,
        )
        r["requested"] = req
        chart_rel = write_chart(
            req, r.pop("_df"), r["markers"], r["trades"], r["metrics"],
            trade_tf="M5",
        )
        r["chart"] = chart_rel
        r.pop("_equity", None)
        rows.append(r)

    ok = [r for r in rows if "metrics" in r]
    total_trades = sum(r["metrics"]["trades"] for r in ok)
    total_pnl = sum(r["metrics"]["net_profit"] for r in ok)
    wins = sum(r["metrics"]["wins"] for r in ok)
    losses = sum(r["metrics"]["losses"] for r in ok)
    lo, hi = wexp["session"]
    sess_label = f"{lo}-{hi}"
    report = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "strategy": "HSS",
        "days": days,
        "winner_of": "hss_sweep_m5",
        "params": {
            "tf": "M5",
            "balance": balance, "risk_pct": risk, "rr": wexp["rr"],
            "pullback": wexp["pullback"], "ema": ema, "session": sess_label,
            "entry_mode": "stop",
            "vol_mode": wexp.get("vol_mode", "min"),
            "matching_doji": bool(wexp.get("matching")),
            "min_bars_since_cross": int(wexp.get("min_cross") or 0),
            "ta_filter": wexp.get("ta_filter", "off"),
            "variant": wexp["id"],
            "variant_title": wexp["title"],
        },
        "summary": {
            "symbols": len(ok),
            "errors": 0,
            "trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total_trades * 100) if total_trades else 0.0,
            "net_profit": round(total_pnl, 2),
            "return_pct_sum": round(sum(r["metrics"]["return_pct"] for r in ok), 2),
        },
        "books": [{
            "symbol": r.get("requested") or r["symbol"],
            "broker": r.get("symbol"),
            "from": r.get("from"),
            "to": r.get("to"),
            "days": r.get("days"),
            "bars": r.get("bars"),
            "metrics": r.get("metrics"),
            "setups": r.get("setups"),
            "expired": r.get("expired"),
            "rejected": r.get("rejected"),
            "trades_n": len(r.get("trades") or []),
            "chart": r.get("chart"),
            "error": r.get("error"),
        } for r in rows],
    }
    (OUT / "hss_report_m5.json").write_text(_json_dump(report), encoding="utf-8")
    con.print("[dim]сравнение → http://127.0.0.1:8787/hss_sweep_m5.html[/]")
    con.print("[dim]графики победителя → http://127.0.0.1:8787/hss_report_m5.html[/]")


if __name__ == "__main__":
    main()
