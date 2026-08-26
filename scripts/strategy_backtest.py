"""Бэктест любой CFD-стратегии за N дней + отчёт с графиками.

    python scripts/strategy_backtest.py --strategy hss --days 90
    python scripts/strategy_backtest.py --strategy apex_shot --days 90

Открыть: http://127.0.0.1:<port>/hss_report.html
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import MetaTrader5 as mt5
import pandas as pd
from rich.console import Console
from rich.table import Table

from neft.backtest import data, metrics
from neft.backtest.engine import Backtester
from neft.core import machine_config, symbols
from neft.core import mt5_symbols as mt5sym
from neft.core.bybit_cfd_fees import costs_for
from neft.core.config import ROOT
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.cfd_factory import CFD_CATALOG, CFD_KEYS, build_cfd_strategy, strategy_tf

con = Console()
OUT = ROOT / "dashboard"
CHARTS = OUT / "backtest_charts"
REPORT_PATH = OUT / "backtest_report.json"

TF_RULE = {"M1": "1min", "M5": "5min", "M15": "15min", "H1": "1h"}
OVERVIEW_RULE = {"M1": "5min", "M5": "15min", "M15": "1h", "H1": "4h"}


def _json_dump(obj) -> str:
    def fix(o):
        if isinstance(o, float):
            if o != o:
                return None
            if o == float("inf"):
                return 999.0
            if o == float("-inf"):
                return -999.0
            return o
        if isinstance(o, dict):
            return {k: fix(v) for k, v in o.items()}
        if isinstance(o, list):
            return [fix(v) for v in o]
        return o
    return json.dumps(fix(obj), ensure_ascii=False, allow_nan=False, default=str)


def _ts(t) -> int:
    if t is None or (isinstance(t, float) and pd.isna(t)):
        return 0
    ts = pd.Timestamp(t)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return int(ts.timestamp())


def _candles(df: pd.DataFrame) -> list[dict]:
    return [{
        "time": _ts(r.time),
        "open": float(r.open),
        "high": float(r.high),
        "low": float(r.low),
        "close": float(r.close),
    } for r in df.itertuples(index=False)]


def _close_label(reason: str, entry: float, exit_: float) -> str:
    r = (reason or "").lower()
    if r.startswith("tp"):
        return "TP"
    if r == "sl" and abs(float(exit_) - float(entry)) < max(1e-9, abs(entry) * 1e-6):
        return "BE"
    if r == "sl":
        return "SL"
    if r == "stop_out":
        return "SO"
    return (r or "out").upper()


def _ema_tag(df: pd.DataFrame | None, signal_at) -> str:
    if df is None or signal_at is None:
        return ""
    if "ha_close" in df.columns and "ema" in df.columns:
        hc_col, ema_col = "ha_close", "ema"
    elif "close" in df.columns and "ema_fast" in df.columns:
        hc_col, ema_col = "close", "ema_fast"
    else:
        return ""
    ts = pd.Timestamp(signal_at)
    hits = df.index[df.time == ts]
    if len(hits) == 0:
        return ""
    row = df.iloc[int(hits[0])]
    hc, ema_v = float(row[hc_col]), float(row[ema_col])
    if hc > ema_v:
        return "ha>EMA"
    if hc < ema_v:
        return "ha<EMA"
    return "ha=EMA"


def _markers(trades, df: pd.DataFrame | None = None) -> list[dict]:
    marks = []
    for t in trades:
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        buy = side == "buy"
        if t.opened_at is not None:
            marks.append({
                "time": _ts(t.opened_at),
                "price": float(t.entry),
                "kind": "entry",
                "side": side,
                "sl": float(t.sl) if getattr(t, "sl", None) is not None else None,
                "tp": float(t.tp) if getattr(t, "tp", None) is not None else None,
                "position": "belowBar" if buy else "aboveBar",
                "color": "#3dd6c3" if buy else "#ff7b78",
                "shape": "circle",
            })
        if t.closed_at is not None:
            lab = _close_label(t.reason, t.entry, t.exit)
            is_tp = lab == "TP"
            marks.append({
                "time": _ts(t.closed_at),
                "price": float(t.exit),
                "kind": "exit",
                "side": side,
                "reason": t.reason,
                "label": lab,
                "position": "aboveBar" if is_tp else "belowBar",
                "color": "#ffc107" if is_tp else "#ef5350",
                "shape": "circle",
            })
    return marks


def _trade_rows(trades, df: pd.DataFrame | None = None) -> list[dict]:
    rows = []
    for t in trades:
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        sig_at = getattr(t, "signal_at", None)
        rows.append({
            "side": side,
            "volume": float(t.volume),
            "entry": float(t.entry),
            "exit": float(t.exit),
            "pnl": round(float(t.pnl), 4),
            "reason": t.reason,
            "bars": int(t.bars_held),
            "opened_at": str(t.opened_at) if t.opened_at is not None else "",
            "closed_at": str(t.closed_at) if t.closed_at is not None else "",
            "signal_at": str(sig_at) if sig_at is not None else "",
            "sl": float(t.sl) if getattr(t, "sl", None) is not None else None,
            "tp": float(t.tp) if getattr(t, "tp", None) is not None else None,
            "why": getattr(t, "signal_reason", "") or "",
            "ema_tag": _ema_tag(df, sig_at),
            "label": _close_label(t.reason, t.entry, t.exit),
        })
    return rows


def run_symbol(
    sym: str,
    df: pd.DataFrame,
    *,
    strategy_key: str,
    cfg: dict,
    balance: float,
    risk: float,
    lite: bool = False,
) -> dict:
    spec = symbols.load(sym)
    limits = RiskLimits(
        risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
        max_volume=100.0, max_daily_loss_pct=100.0,
        max_drawdown_pct=100.0, min_free_margin_pct=0.0,
    )
    rm = RiskManager(start_balance=balance, limits=limits)
    strat = build_cfd_strategy(
        strategy_key, cfg=cfg, risk=risk, risk_manager=rm, spec=spec, symbol=sym,
    )
    costs = costs_for(spec)
    res = Backtester(strat, rm, costs, start_balance=balance, symbol=sym).run(df)
    m = metrics.compute(res.equity, res.trades, balance, res.ruined)
    days = max(1, (df.time.iloc[-1] - df.time.iloc[0]).days)
    out = {
        "symbol": sym,
        "bars": len(df),
        "from": str(df.time.iloc[0]),
        "to": str(df.time.iloc[-1]),
        "days": days,
        "metrics": m.as_dict(),
        "setups": int(getattr(strat, "setups_seen", 0) or 0),
        "expired": int(res.expired),
        "rejected": len(res.rejected),
    }
    for attr in ("skipped_session", "skipped_fee", "skipped_ema_touch",
                 "skipped_structure", "skipped_ta", "skipped"):
        if hasattr(strat, attr):
            out[attr] = int(getattr(strat, attr) or 0)
    if lite:
        out["trades"] = []
        out["markers"] = []
        return out
    strat_df = getattr(strat, "df", None)
    out["trades"] = _trade_rows(res.trades, strat_df)
    out["markers"] = _markers(res.trades, strat_df)
    out["_df"] = df
    return out


def write_chart(
    sym: str,
    df_trade: pd.DataFrame,
    markers: list[dict],
    trades: list[dict],
    metrics_d: dict,
    *,
    trade_tf: str = "M1",
) -> str:
    CHARTS.mkdir(parents=True, exist_ok=True)
    overview_rule = OVERVIEW_RULE.get(trade_tf, "15min")
    overview = data.resample_ohlc(df_trade, overview_rule)
    payload = {
        "symbol": sym,
        "tf": trade_tf,
        "metrics": metrics_d,
        "trades": trades,
        "markers": markers,
        "candles_trade": _candles(df_trade),
        "candles_overview": _candles(overview),
        "period": [str(df_trade.time.iloc[0]), str(df_trade.time.iloc[-1])],
    }
    if trade_tf == "M1":
        payload["candles_m5"] = payload["candles_overview"]
        payload["candles_m1"] = payload["candles_trade"]
    else:
        payload["candles_m5"] = payload["candles_trade"]
        payload["candles_m15"] = payload["candles_overview"]
        payload["candles_m1"] = []
    safe = sym.replace("+", "plus")
    rel = f"backtest_charts/{safe}.json"
    (OUT / rel).write_text(_json_dump(payload), encoding="utf-8")
    return rel


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", default="hss", choices=CFD_KEYS)
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--balance", type=float, default=None)
    p.add_argument("--risk", type=float, default=None)
    p.add_argument("--tf", default=None, help="M1|M5|M15|H1; иначе из конфига")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--symbols", default=None,
                   help="через запятую; иначе все CFD из конфига")
    p.add_argument("--no-charts", action="store_true")
    a = p.parse_args()

    cfg = machine_config.load()
    meta = CFD_CATALOG[a.strategy]
    trade_tf = (a.tf or strategy_tf(a.strategy, cfg)).upper()
    if trade_tf not in TF_RULE:
        con.print(f"[red]неизвестный ТФ: {trade_tf}[/]")
        sys.exit(2)

    st = (cfg.get("strategies") or {}).get(a.strategy) or {}
    balance = float(a.balance if a.balance is not None else cfg.get("deposit") or 1000)
    risk = float(a.risk if a.risk is not None else cfg.get("risk_pct") or 0.5)

    if a.strategy == "hss":
        hss = st
        if hss.get("all_day") or cfg.get("hss_24h"):
            sess_label = "24h"
        else:
            raw = hss.get("session") or cfg.get("hss_session") or [16, 19]
            sess_label = f"{int(raw[0])}-{int(raw[1])}"
        params = {
            "tf": trade_tf, "balance": balance, "risk_pct": risk,
            "rr": float(st.get("rr") or cfg.get("rr") or 1.0),
            "pullback": int(st.get("pullback") or cfg.get("pullback_bars") or 2),
            "ema": int(st.get("ema") or 100),
            "session": sess_label,
            "entry_mode": str(st.get("entry_mode") or "market"),
        }
    else:
        params = {
            "tf": trade_tf, "balance": balance, "risk_pct": risk,
            **{k: v for k, v in st.items() if k not in ("enabled",)},
        }

    want = ([s.strip() for s in a.symbols.split(",") if s.strip()]
            if a.symbols else list(cfg.get("mt5_symbols") or mt5sym.CFD_SYMBOLS))

    if not mt5.initialize():
        con.print(f"[red]MT5: {mt5.last_error()}[/]")
        sys.exit(1)
    try:
        resolved, missing = mt5sym.resolve_many(want)
        if missing:
            con.print(f"[yellow]нет в терминале: {', '.join(missing)}[/]")
        if not resolved:
            con.print("[red]ни одного CFD не резолвнулось[/]")
            sys.exit(2)

        rows = []
        for req, bro in resolved:
            con.print(f"[cyan]→ {req}[/] ({bro}) · {a.strategy} · {a.days}д M1 → {trade_tf}…")
            try:
                df_m1 = data.load_days(bro, "M1", a.days, refresh=a.refresh)
            except Exception as e:
                con.print(f"[red]  история: {e}[/]")
                rows.append({"symbol": req, "error": str(e)})
                continue
            if len(df_m1):
                end = df_m1.time.iloc[-1]
                df_m1 = df_m1[df_m1.time >= end - pd.Timedelta(days=a.days)].reset_index(drop=True)
            df = df_m1 if trade_tf == "M1" else data.resample_ohlc(df_m1, TF_RULE[trade_tf])
            if df.empty:
                con.print("[red]  пустые бары[/]")
                rows.append({"symbol": req, "error": "нет баров"})
                continue
            try:
                r = run_symbol(
                    bro, df, strategy_key=a.strategy, cfg=cfg,
                    balance=balance, risk=risk, lite=a.no_charts,
                )
            except Exception as e:
                con.print(f"[red]  бэктест: {e}[/]")
                rows.append({"symbol": req, "error": str(e)})
                continue
            r["requested"] = req
            if not a.no_charts:
                chart_rel = write_chart(
                    req, r.pop("_df"), r["markers"], r["trades"], r["metrics"],
                    trade_tf=trade_tf,
                )
                r["chart"] = chart_rel
            else:
                r.pop("_df", None)
            m = r["metrics"]
            c = "green" if m["net_profit"] > 0 else "red"
            con.print(
                f"  [{c}]сделок {m['trades']} · WR {m['win_rate']:.1f}% · "
                f"PF {m['profit_factor']:.2f} · {m['return_pct']:+.2f}%[/]"
            )
            rows.append(r)
    finally:
        mt5.shutdown()

    ok = [r for r in rows if "metrics" in r]
    total_trades = sum(r["metrics"]["trades"] for r in ok)
    total_pnl = sum(r["metrics"]["net_profit"] for r in ok)
    wins = sum(r["metrics"]["wins"] for r in ok)
    losses = sum(r["metrics"]["losses"] for r in ok)

    report = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "strategy": meta["name"],
        "strategy_key": a.strategy,
        "days": a.days,
        "params": params,
        "summary": {
            "symbols": len(ok),
            "errors": len(rows) - len(ok),
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
            "chart": r.get("chart") or (
                f"backtest_charts/{(r.get('requested') or r['symbol']).replace('+', 'plus')}.json"
            ),
            "error": r.get("error"),
        } for r in rows],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(_json_dump(report), encoding="utf-8")
    # Совместимость со старыми ссылками.
    (OUT / "hss_report.json").write_text(_json_dump(report), encoding="utf-8")

    t = Table(
        title=f"{meta['name']} · {trade_tf} · CFD · {a.days}д · риск {risk}% · ${balance:.0f}",
        box=None, pad_edge=False, title_style="bold cyan",
    )
    for col in ("символ", "сделок", "WR%", "PF", "доход%", "DD%", "ожид.$", "серия−"):
        t.add_column(col, justify="right" if col != "символ" else "left")
    for r in sorted(ok, key=lambda x: -x["metrics"]["return_pct"]):
        m = r["metrics"]
        c = "green" if m["return_pct"] > 0 else "red"
        pf = m["profit_factor"]
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        t.add_row(
            r.get("requested") or r["symbol"],
            str(m["trades"]),
            f"{m['win_rate']:.1f}",
            pf_s,
            f"[{c}]{m['return_pct']:+.2f}[/]",
            f"{m['max_drawdown_pct']:.1f}",
            f"{m['expectancy']:+.3f}",
            str(m["max_loss_streak"]),
        )
    t.add_section()
    wr = report["summary"]["win_rate"]
    sc = "green" if total_pnl > 0 else "red"
    t.add_row(
        "ИТОГО", str(total_trades), f"{wr:.1f}", "—",
        f"[{sc}]{report['summary']['return_pct_sum']:+.2f}[/]", "—", "—", "—",
    )
    con.print(t)
    con.print(f"\n[dim]отчёт → {REPORT_PATH.relative_to(ROOT)}[/]")
    con.print("[dim]график → /hss_report.html[/]")


if __name__ == "__main__":
    main()
