"""Бэктест HSS на всех CFD за N месяцев + отчёт с метками на графике.

    python scripts/hss_cfd_report.py
    python scripts/hss_cfd_report.py --days 90 --tf M5
    python scripts/hss_cfd_report.py --days 90 --refresh

Открыть: http://127.0.0.1:<port>/hss_report.html
         http://127.0.0.1:<port>/hss_report_m5.html
         http://127.0.0.1:<port>/hss_report_m15.html
         http://127.0.0.1:<port>/hss_report_h1.html
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
from neft.strategies.scalp_ha import ScalpHA

con = Console()
OUT = ROOT / "dashboard"
CHARTS = OUT / "hss_charts"
CHARTS_BY_TF = {
    "M1": OUT / "hss_charts",
    "M5": OUT / "hss_charts_m5",
    "M15": OUT / "hss_charts_m15",
    "H1": OUT / "hss_charts_h1",
}
REPORT_BY_TF = {
    "M1": "hss_report.json",
    "M5": "hss_report_m5.json",
    "M15": "hss_report_m15.json",
    "H1": "hss_report_h1.json",
}

TF_RULE = {"M1": "1min", "M5": "5min", "M15": "15min", "H1": "1h"}
OVERVIEW_RULE = {"M1": "5min", "M5": "15min", "M15": "1h", "H1": "4h"}


def _json_dump(obj) -> str:
    """JSON без Infinity/NaN — иначе браузер падает на parse."""
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
    out = []
    for r in df.itertuples(index=False):
        out.append({
            "time": _ts(r.time),
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
        })
    return out


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
    """Подпись ha_close ≷ EMA на баре doji (как в правиле 2)."""
    if df is None or signal_at is None or "ha_close" not in df.columns:
        return ""
    ts = pd.Timestamp(signal_at)
    hits = df.index[df.time == ts]
    if len(hits) == 0:
        return ""
    row = df.iloc[int(hits[0])]
    hc, ema = float(row.ha_close), float(row.ema)
    if hc > ema:
        return "ha>EMA"
    if hc < ema:
        return "ha<EMA"
    return "ha=EMA"


def _markers(trades, df: pd.DataFrame | None = None) -> list[dict]:
    """Вход / выход — точки на барах; без doji."""
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


def run_symbol(sym: str, df: pd.DataFrame, *, balance: float, risk: float,
               rr: float, pullback: int, session, ema: int,
               vol_mode: str = "min", vol_window: int = 3,
               require_matching_doji: bool = False,
               min_bars_since_cross: int = 0,
               ta_filter: str = "off",
               lite: bool = False) -> dict:
    spec = symbols.load(sym)
    limits = RiskLimits(
        risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
        max_volume=100.0, max_daily_loss_pct=100.0,
        max_drawdown_pct=100.0, min_free_margin_pct=0.0,
    )
    rm = RiskManager(start_balance=balance, limits=limits)
    strat = ScalpHA(
        ema_period=ema, pullback_bars=pullback, rr=rr,
        vol_mode=vol_mode, vol_window=vol_window, entry_mode="stop",
        require_matching_doji=require_matching_doji,
        min_bars_since_cross=min_bars_since_cross,
        ta_filter=ta_filter,
        session=session, risk_pct=risk, risk_manager=rm, spec=spec,
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
        "skipped_session": int(getattr(strat, "skipped_session", 0) or 0),
        "skipped_fee": int(getattr(strat, "skipped_fee", 0) or 0),
        "skipped_ema_touch": int(getattr(strat, "skipped_ema_touch", 0) or 0),
        "skipped_structure": int(getattr(strat, "skipped_structure", 0) or 0),
        "skipped_ta": int(getattr(strat, "skipped_ta", 0) or 0),
    }
    if lite:
        out["trades"] = []
        out["markers"] = []
        return out
    out["trades"] = _trade_rows(res.trades, strat.df)
    out["markers"] = _markers(res.trades, strat.df)
    out["_df"] = df
    out["_equity"] = res.equity
    return out


def write_chart(sym: str, df_trade: pd.DataFrame, markers: list[dict],
                trades: list[dict], metrics_d: dict, *, trade_tf: str = "M1",
                df_m1: pd.DataFrame | None = None,
                chart_dir: str | None = None) -> str:
    """Свечи торгового ТФ + обзор. M1-отчёт: M5 обзор / M1 зум.
    M5-отчёт: M15 обзор / M5 зум (клик по сделке)."""
    dest = OUT / chart_dir if chart_dir else CHARTS_BY_TF.get(trade_tf, CHARTS)
    dest.mkdir(parents=True, exist_ok=True)
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
    # Старые ключи: HTML M1 ждёт candles_m5 / candles_m1.
    if trade_tf == "M1":
        payload["candles_m5"] = payload["candles_overview"]
        payload["candles_m1"] = payload["candles_trade"]
    else:
        payload["candles_m5"] = payload["candles_trade"]
        payload["candles_m15"] = payload["candles_overview"]
        if df_m1 is not None and len(df_m1):
            payload["candles_m1"] = _candles(df_m1)
        else:
            payload["candles_m1"] = []
    safe = sym.replace("+", "plus")
    (dest / f"{safe}.json").write_text(_json_dump(payload), encoding="utf-8")
    return f"{dest.name}/{safe}.json"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--balance", type=float, default=None)
    p.add_argument("--risk", type=float, default=None)
    p.add_argument("--rr", type=float, default=None)
    p.add_argument("--pullback", type=int, default=None)
    p.add_argument("--session", default=None,
                   help="часы UTC+3, напр. 8-20; пусто = из конфига")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--tf", default="M1", choices=("M1", "M5", "M15", "H1"),
                   help="таймфрейм сигналов HSS (по умолчанию M1)")
    p.add_argument("--symbols", default=None,
                   help="через запятую; иначе все CFD из конфига")
    a = p.parse_args()
    trade_tf = a.tf.upper()

    cfg = machine_config.load()
    hss = (cfg.get("strategies") or {}).get("hss") or {}
    balance = float(a.balance if a.balance is not None else cfg.get("deposit") or 1000)
    risk = float(a.risk if a.risk is not None else cfg.get("risk_pct") or 0.5)
    rr = float(a.rr if a.rr is not None else hss.get("rr") or cfg.get("rr") or 1.0)
    pullback = int(a.pullback if a.pullback is not None
                   else hss.get("pullback") or cfg.get("pullback_bars") or 2)
    ema = int(hss.get("ema") or 100)
    if a.session:
        lo, hi = (int(x) for x in a.session.split("-", 1))
        session = (lo, hi)
        sess_label = f"{lo}-{hi}"
    elif hss.get("all_day") or cfg.get("hss_24h"):
        session = None
        sess_label = "24h"
    else:
        raw = hss.get("session") or cfg.get("hss_session") or [8, 20]
        session = (int(raw[0]), int(raw[1]))
        sess_label = f"{session[0]}-{session[1]}"

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
            con.print(f"[cyan]→ {req}[/] ({bro}) · загрузка {a.days}д M1 → {trade_tf}…")
            try:
                df_m1 = data.load_days(bro, "M1", a.days, refresh=a.refresh)
            except Exception as e:
                con.print(f"[red]  история: {e}[/]")
                rows.append({"symbol": req, "error": str(e)})
                continue
            # Обрезаем ровно до окна days от конца, затем агрегируем в торговый ТФ.
            if len(df_m1):
                end = df_m1.time.iloc[-1]
                df_m1 = df_m1[df_m1.time >= end - pd.Timedelta(days=a.days)].reset_index(drop=True)
            if trade_tf == "M1":
                df = df_m1
            else:
                df = data.resample_ohlc(df_m1, TF_RULE[trade_tf])
            if df.empty:
                con.print("[red]  пустые бары после ресемпла[/]")
                rows.append({"symbol": req, "error": "нет баров"})
                continue
            con.print(f"[dim]  {len(df)} баров {trade_tf} · {df.time.iloc[0]} — {df.time.iloc[-1]}[/]")
            try:
                r = run_symbol(
                    bro, df, balance=balance, risk=risk, rr=rr,
                    pullback=pullback, session=session, ema=ema,
                )
            except Exception as e:
                con.print(f"[red]  бэктест: {e}[/]")
                rows.append({"symbol": req, "error": str(e)})
                continue
            r["requested"] = req
            chart_rel = write_chart(
                req, r.pop("_df"), r["markers"], r["trades"], r["metrics"],
                trade_tf=trade_tf,
            )
            r["chart"] = chart_rel
            r.pop("_equity", None)
            m = r["metrics"]
            c = "green" if m["net_profit"] > 0 else "red"
            con.print(
                f"  [{c}]сделок {m['trades']} · WR {m['win_rate']:.1f}% · "
                f"PF {m['profit_factor']:.2f} · {m['return_pct']:+.2f}% · "
                f"DD {m['max_drawdown_pct']:.1f}%[/]"
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
        "strategy": "HSS",
        "days": a.days,
        "params": {
            "tf": trade_tf,
            "balance": balance, "risk_pct": risk, "rr": rr,
            "pullback": pullback, "ema": ema, "session": sess_label,
            "entry_mode": "stop", "vol_mode": "min",
        },
        "summary": {
            "symbols": len(ok),
            "errors": len(rows) - len(ok),
            "trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total_trades * 100) if total_trades else 0.0,
            "net_profit": round(total_pnl, 2),
            "return_pct_sum": round(
                sum(r["metrics"]["return_pct"] for r in ok), 2),
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
                f"{CHARTS_BY_TF.get(trade_tf, CHARTS).name}/"
                f"{(r.get('requested') or r['symbol']).replace('+', 'plus')}.json"
            ),
            "error": r.get("error"),
        } for r in rows],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / REPORT_BY_TF.get(trade_tf, "hss_report.json")
    path.write_text(_json_dump(report), encoding="utf-8")

    t = Table(title=f"HSS · {trade_tf} · CFD · {a.days}д · сессия {sess_label} · "
                    f"R:R {rr} · риск {risk}% · депозит ${balance:.0f}",
              box=None, pad_edge=False, title_style="bold cyan")
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
    con.print(t)
    con.print(
        f"[bold]Итого:[/] {len(ok)} символов · {total_trades} сделок · "
        f"WR {(wins / total_trades * 100) if total_trades else 0:.1f}% · "
        f"Σ PnL [{('green' if total_pnl >= 0 else 'red')}]"
        f"${total_pnl:+,.2f}[/]"
    )
    html = {"M5": "hss_report_m5.html", "M15": "hss_report_m15.html",
            "H1": "hss_report_h1.html"}.get(trade_tf, "hss_report.html")
    con.print(f"[dim]отчёт → {path}[/]")
    con.print(f"[dim]график → dashboard/{html}[/]")
    con.print(f"[dim]http://127.0.0.1:8787/{html}[/]")


if __name__ == "__main__":
    main()
