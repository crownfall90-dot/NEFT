"""Реальный paper-прогон новостного бота + выбор лучшего режима на актив.

Календарь: FRED High USD (CPI/NFP/GDP/PPI/PCE/FOMC).
Депозит $1000, риск ≤1%. На каждый символ оставляем режим с макс. P&L.

    python scripts/news_pulse_report.py --days 365
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import MetaTrader5 as mt5
import pandas as pd
from rich.console import Console
from rich.table import Table

from neft.backtest import data, metrics
from neft.backtest.data import resample_ohlc
from neft.backtest.engine import Backtester
from neft.core import mt5_symbols as mt5sym
from neft.core import symbols as symlib
from neft.core.bybit_cfd_fees import costs_for
from neft.core.config import ROOT
from neft.core.models import Side
from neft.core.news import Event, currencies_for
from neft.core.news_historical import load_historical
from neft.core.risk import RiskLimits, RiskManager
from neft.core.strategy import Bar, ClosedTrade
from neft.strategies.news_pulse import (
    EventTracker, NewsFade, NewsImpulse, NewsStraddle,
)

con = Console()
OUT = ROOT / "dashboard"
ROUTES_PATH = ROOT / "data" / "news_routes.json"
BALANCE = 1000.0
RISK_PCT = 1.0
RR = 1.5
DAYS = 365
UTC_OFFSET = 3.0
MIN_TRADES = 2  # меньше — режим не считаем «рабочим» для выбора


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
        "open": float(r.open), "high": float(r.high),
        "low": float(r.low), "close": float(r.close),
    } for r in df.itertuples(index=False)]


def _to_events(hist) -> list[Event]:
    return [
        Event(time=e.time, currency=e.currency, impact=e.impact,
              title=e.title, forecast="", previous="", actual="")
        for e in hist
    ]


def _trade_rows(trades: list[ClosedTrade], mode: str) -> list[dict]:
    rows = []
    for i, t in enumerate(trades, 1):
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        rows.append({
            "n": i, "mode": mode, "side": side,
            "volume": float(t.volume),
            "entry": float(t.entry), "exit": float(t.exit),
            "sl": float(t.sl) if t.sl is not None else None,
            "tp": float(t.tp) if t.tp is not None else None,
            "pnl": round(float(t.pnl), 2),
            "reason": t.reason,
            "opened_at": str(t.opened_at) if t.opened_at is not None else "",
            "closed_at": str(t.closed_at) if t.closed_at is not None else "",
            "entry_time": _ts(t.opened_at),
            "exit_time": _ts(t.closed_at),
            "signal": t.signal_reason or "",
        })
    return rows


def _markers(trades: list[ClosedTrade]) -> list[dict]:
    marks = []
    for t in trades:
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        buy = side == "buy"
        if t.opened_at is not None:
            marks.append({
                "time": _ts(t.opened_at), "kind": "entry", "side": side,
                "position": "belowBar" if buy else "aboveBar",
                "color": "#3dd6c3" if buy else "#ff7b78",
                "shape": "arrowUp" if buy else "arrowDown",
                "text": "вход",
            })
        if t.closed_at is not None:
            lab = "TP" if str(t.reason).startswith("tp") else "SL"
            marks.append({
                "time": _ts(t.closed_at), "kind": "exit", "side": side,
                "position": "aboveBar" if lab == "TP" else "belowBar",
                "color": "#ffc107" if lab == "TP" else "#ef5350",
                "shape": "circle", "text": lab,
            })
    return marks


def _limits(risk: float) -> RiskLimits:
    return RiskLimits(
        risk_per_trade_pct=risk, max_risk_per_trade_pct=1.0,
        min_risk_per_trade_pct=0.1, max_volume=50.0,
        max_daily_loss_pct=100.0, max_drawdown_pct=100.0,
        max_open_positions=1, min_free_margin_pct=0.0,
    )


def run_impulse_or_fade(symbol: str, df: pd.DataFrame, events: list[Event],
                        mode: str, *, balance: float, risk: float, rr: float) -> dict:
    spec = symlib.load(symbol)
    rm = RiskManager(start_balance=balance, limits=_limits(risk))
    tracker = EventTracker()
    cls = NewsImpulse if mode == "impulse" else NewsFade
    strat = cls(
        symbol=mt5sym.canonical(symbol), tracker=tracker, utc_offset_hours=UTC_OFFSET,
        post_window_min=15, pre_minutes=2, impulse_atr=0.8, rr=rr,
        risk_pct=risk, risk_manager=rm, spec=spec,
    )
    strat.set_events(events)
    costs = costs_for(spec)
    res = Backtester(strat, rm, costs, start_balance=balance, symbol=symbol).run(df)
    m = metrics.compute(res.equity, res.trades, balance, res.ruined)
    eq = [{"t": str(t), "v": float(v)} for t, v in res.equity.items()] if len(res.equity) else []
    # equity index may be int — normalize
    if len(res.equity) and not isinstance(res.equity.index[0], (str, pd.Timestamp)):
        eq = [{"t": i + 1, "v": float(v)} for i, v in enumerate(res.equity.values)]
    return {
        "symbol": symbol, "mode": mode, "metrics": m.as_dict(),
        "trades": _trade_rows(res.trades, mode),
        "markers": _markers(res.trades),
        "equity": eq,
        "expired": int(res.expired),
        "rejected": len(res.rejected),
    }


def run_straddle(symbol: str, df: pd.DataFrame, events: list[Event],
                 *, balance: float, risk: float, rr: float) -> dict:
    """OCO paper на истории: Backtester одну ногу не тянет."""
    spec = symlib.load(symbol)
    costs = costs_for(spec)
    rm = RiskManager(start_balance=balance, limits=_limits(risk))
    tracker = EventTracker()
    strat = NewsStraddle(
        symbol=mt5sym.canonical(symbol), tracker=tracker, utc_offset_hours=UTC_OFFSET,
        post_window_min=15, pre_minutes=2, impulse_atr=0.8, rr=rr,
        risk_pct=risk, risk_manager=rm, spec=spec, expire_bars=30,
    )
    strat.set_events(events)
    strat.set_equity(balance)
    prepared = strat.prepare(df).reset_index(drop=True)

    equity = balance
    peak = balance
    trades: list[ClosedTrade] = []
    eq_pts = [equity]
    pending = None  # (buy_sig, sell_sig, expire_i)
    pos = None
    i = 50
    while i < len(prepared) - 2:
        row = prepared.iloc[i]
        # manage position
        if pos is not None:
            side, vol, entry, sl, tp, opened_at, opened_i, signal = pos
            buy = side == "buy"
            hit_sl = row.low <= sl if buy else row.high >= sl
            hit_tp = row.high >= tp if buy else row.low <= tp
            if hit_sl or hit_tp:
                reason = "sl" if hit_sl else "tp"
                exit_px = sl if hit_sl else tp
                d = 1 if buy else -1
                gross = (exit_px - entry) * d * vol * costs.contract_size
                pnl = gross - costs.commission_per_lot * vol
                equity += pnl
                peak = max(peak, equity)
                trades.append(ClosedTrade(
                    side=Side(side),
                    volume=vol, entry=entry, exit=exit_px, pnl=pnl,
                    reason=reason, bars_held=i - opened_i,
                    opened_at=opened_at, closed_at=row.time,
                    signal_reason=signal, sl=sl, tp=tp,
                ))
                pos = None
                eq_pts.append(equity)
            i += 1
            continue

        # manage OCO pending
        if pending is not None:
            buy_s, sell_s, expire_i = pending
            if i >= expire_i:
                pending = None
                strat.on_signal_rejected(buy_s, "expire")
                i += 1
                continue
            hit_buy = row.high >= float(buy_s.entry)
            hit_sell = row.low <= float(sell_s.entry)
            fill = None
            if hit_buy and not hit_sell:
                fill = buy_s
            elif hit_sell and not hit_buy:
                fill = sell_s
            elif hit_buy and hit_sell:
                fill = sell_s  # пессимистично
            if fill is not None:
                spread = max(float(row.spread or 0), costs.spread_points) * costs.point
                side = fill.side.value
                entry = float(fill.entry) + (spread if side == "buy" else -spread)
                pos = (side, float(fill.volume), entry, float(fill.sl), float(fill.tp),
                       row.time, i, fill.reason)
                pending = None
                strat.mark_filled()
            i += 1
            continue

        strat.set_equity(equity)
        bar = Bar(row.time, row.open, row.high, row.low, row.close,
                  int(row.spread or 0), index=i, volume=float(row.tick_volume or 0))
        sig = strat.on_bar(bar, in_position=False)
        if sig is None:
            i += 1
            continue
        oco = strat.consume_oco()
        if oco is None:
            i += 1
            continue
        buy_s, sell_s = oco
        # risk check on buy leg
        sl_dist = abs(float(buy_s.entry) - float(buy_s.sl))
        ok, why = rm.approve(
            buy_s, equity=equity, free_margin=equity,
            required_margin=buy_s.volume * costs.contract_size * abs(float(buy_s.entry)) / costs.leverage,
            open_positions=0, sl_distance=sl_dist,
            contract_size=costs.contract_size, when=row.time, symbol=symbol,
        )
        if not ok:
            strat.on_signal_rejected(buy_s, why)
            i += 1
            continue
        pending = (buy_s, sell_s, i + int(buy_s.expire_bars))
        i += 1

    m = metrics.compute(pd.Series(eq_pts), trades, balance, False)
    return {
        "symbol": symbol, "mode": "straddle", "metrics": m.as_dict(),
        "trades": _trade_rows(trades, "straddle"),
        "markers": _markers(trades),
        "equity": [{"t": i + 1, "v": float(v)} for i, v in enumerate(eq_pts)],
        "expired": 0, "rejected": 0,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=DAYS)
    p.add_argument("--balance", type=float, default=BALANCE)
    p.add_argument("--risk", type=float, default=RISK_PCT)
    p.add_argument("--rr", type=float, default=RR)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--symbols", default=None,
                   help="через запятую; иначе весь CFD_SYMBOLS")
    a = p.parse_args()
    a.risk = max(0.1, min(1.0, float(a.risk)))

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(a.days))
    start_s, end_s = str(start), str(end)

    con.print(f"[bold]News Pulse paper[/] · {a.days}д · депозит ${a.balance} · риск {a.risk}%")
    con.print(f"[dim]период UTC: {start_s} → {end_s}[/]")

    hist = load_historical(start_s, end_s)
    events = _to_events(hist)
    con.print(f"FRED High (USD): {len(events)} релизов "
              f"({', '.join(sorted({e.title for e in events}))})")

    if not mt5.initialize():
        con.print(f"[red]MT5: {mt5.last_error()}[/]")
        raise SystemExit(2)
    acc = mt5.account_info()
    server = acc.server if acc else "?"
    login = acc.login if acc else "?"
    con.print(f"[dim]MT5 {login} @ {server} · режим paper (ордера не уходят)[/]")

    want = [x.strip() for x in (a.symbols.split(",") if a.symbols else mt5sym.CFD_SYMBOLS) if x.strip()]
    resolved, missing = mt5sym.resolve_many(want, tradable_only=False)
    for s in missing:
        con.print(f"[yellow]нет у брокера: {s}[/]")

    mode_books = {"impulse": [], "fade": [], "straddle": []}
    charts: dict[str, dict] = {}
    routes: dict[str, dict] = {}
    period_from = period_to = None

    try:
        for req, broker in resolved:
            curs = currencies_for(req) or currencies_for(broker)
            if "USD" not in curs:
                con.print(f"[dim]{req}: нет USD в карте валют — FRED-событий не будет, пропуск[/]")
                continue
            try:
                df = data.load_days(broker, "M1", days=a.days, refresh=a.refresh)
            except Exception as e:
                con.print(f"[yellow]{broker}: нет истории ({e})[/]")
                continue
            if len(df) < 500:
                con.print(f"[yellow]{broker}: мало баров ({len(df)})[/]")
                continue
            period_from = str(df.time.iloc[0]) if period_from is None else period_from
            period_to = str(df.time.iloc[-1])
            con.print(f"  {broker}: {len(df)} M1 · {df.time.iloc[0]} → {df.time.iloc[-1]}")

            m5 = resample_ohlc(df, "1h")  # год: H1 на графике, иначе HTML раздуется
            per_mode: dict[str, dict] = {}

            for mode in ("impulse", "fade", "straddle"):
                con.print(f"    → {mode}…", end="")
                try:
                    if mode == "straddle":
                        book = run_straddle(broker, df, events, balance=a.balance,
                                            risk=a.risk, rr=a.rr)
                    else:
                        book = run_impulse_or_fade(
                            broker, df, events, mode,
                            balance=a.balance, risk=a.risk, rr=a.rr)
                except Exception as e:
                    con.print(f" [red]fail {e}[/]")
                    continue
                m = book["metrics"]
                con.print(f" trades={m['trades']} pnl={m['net_profit']:+.2f} wr={m['win_rate']:.0f}%")
                mode_books[mode].append(book)
                per_mode[mode] = book

            # лучший режим на этот актив: макс P&L среди режимов с ≥ MIN_TRADES
            candidates = [
                (mode, b) for mode, b in per_mode.items()
                if b["metrics"]["trades"] >= MIN_TRADES
            ]
            if not candidates:
                candidates = list(per_mode.items())  # хоть что-то
            if candidates:
                best_mode, best_book = max(
                    candidates, key=lambda x: x[1]["metrics"]["net_profit"])
            else:
                best_mode, best_book = "impulse", None

            comparison = {
                mode: {
                    "trades": b["metrics"]["trades"],
                    "winrate": round(b["metrics"]["win_rate"], 1),
                    "pnl": round(b["metrics"]["net_profit"], 2),
                    "dd": round(b["metrics"]["max_drawdown_pct"], 2),
                }
                for mode, b in per_mode.items()
            }
            routes[broker] = {
                "mode": best_mode,
                "pnl": round(best_book["metrics"]["net_profit"], 2) if best_book else 0.0,
                "trades": best_book["metrics"]["trades"] if best_book else 0,
                "winrate": round(best_book["metrics"]["win_rate"], 1) if best_book else 0.0,
                "comparison": comparison,
                "logical": mt5sym.canonical(broker),
            }
            con.print(f"    [green]→ выбран {best_mode}[/] "
                      f"pnl={routes[broker]['pnl']:+.2f}")

            best_trades = []
            best_marks = []
            if best_book:
                for t in best_book["trades"]:
                    t2 = dict(t)
                    t2["symbol"] = broker
                    best_trades.append(t2)
                best_marks = list(best_book["markers"])

            charts[broker] = {
                "symbol": broker,
                "candles_m5": _candles(m5),  # фактически H1
                "trades": best_trades,
                "markers": best_marks,
                "best_mode": best_mode,
                "period": [str(df.time.iloc[0]), str(df.time.iloc[-1])],
            }
    finally:
        mt5.shutdown()

    def agg(mode: str) -> dict:
        books = mode_books[mode]
        trades_n = sum(b["metrics"]["trades"] for b in books)
        wins = sum(b["metrics"]["wins"] for b in books)
        pnl = sum(b["metrics"]["net_profit"] for b in books)
        by_sym = {b["symbol"]: round(b["metrics"]["net_profit"], 2) for b in books}
        return {
            "mode": mode,
            "symbols": len(books),
            "trades": trades_n,
            "wins": wins,
            "winrate": round(wins / trades_n * 100, 1) if trades_n else 0.0,
            "pnl": round(pnl, 2),
            "end": round(a.balance + pnl, 2),
            "by_symbol": by_sym,
            "books": [{
                "symbol": b["symbol"],
                "metrics": b["metrics"],
                "trades_n": b["metrics"]["trades"],
            } for b in books],
        }

    modes = {m: agg(m) for m in ("impulse", "fade", "straddle")}

    # портфель «только лучший режим на актив»
    opt_pnl = sum(r["pnl"] for r in routes.values())
    opt_trades = sum(r["trades"] for r in routes.values())
    opt_wins_est = sum(
        r["trades"] * r["winrate"] / 100 for r in routes.values())
    optimized = {
        "pnl": round(opt_pnl, 2),
        "end": round(a.balance + opt_pnl, 2),
        "trades": opt_trades,
        "winrate": round(opt_wins_est / opt_trades * 100, 1) if opt_trades else 0.0,
        "routes": {
            sym: {"mode": r["mode"], "pnl": r["pnl"], "trades": r["trades"],
                  "winrate": r["winrate"], "logical": r["logical"],
                  "comparison": r["comparison"]}
            for sym, r in routes.items()
        },
    }

    all_trades = []
    for sym, ch in charts.items():
        for t in ch["trades"]:
            row = dict(t)
            row["symbol"] = sym
            all_trades.append(row)

    t = Table(title=f"News Pulse · paper {a.days}д · все режимы")
    t.add_column("режим")
    t.add_column("сделок", justify="right")
    t.add_column("WR%", justify="right")
    t.add_column("P&L", justify="right")
    for m, x in modes.items():
        t.add_row(m, str(x["trades"]), f"{x['winrate']:.1f}", f"{x['pnl']:+.2f}")
    con.print(t)

    t2 = Table(title="Маршрут: лучший режим на актив")
    t2.add_column("символ")
    t2.add_column("режим")
    t2.add_column("сделок", justify="right")
    t2.add_column("WR%", justify="right")
    t2.add_column("P&L", justify="right")
    for sym, r in sorted(routes.items(), key=lambda x: -x[1]["pnl"]):
        t2.add_row(sym, r["mode"], str(r["trades"]),
                   f"{r['winrate']:.0f}", f"{r['pnl']:+.2f}")
    con.print(t2)
    con.print(f"[bold green]Оптимизированный портфель: "
              f"{optimized['pnl']:+.2f}$ · {optimized['trades']} сделок[/]")

    ROUTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    ROUTES_PATH.write_text(_json_dump({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": a.days,
        "deposit": a.balance,
        "risk_pct": a.risk,
        "min_trades": MIN_TRADES,
        "routes": {sym: r["mode"] for sym, r in routes.items()},
        "detail": optimized["routes"],
    }), encoding="utf-8")

    default = next(iter(charts), "")
    for pref in ("NAS100.f", "EURUSD.f", "XAUUSD.f"):
        if pref in charts:
            default = pref
            break

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "venue": "MT5 CFD (paper)",
        "mode_label": "paper · ордера в рынок не уходили",
        "broker": f"{login} @ {server}",
        "calendar": "FRED USD High (CPI/NFP/GDP/PPI/PCE/FOMC)",
        "calendar_events": len(events),
        "event_titles": sorted({e.title for e in events}),
        "deposit": a.balance,
        "risk_pct": a.risk,
        "rr": a.rr,
        "days": a.days,
        "period": [period_from or start_s, period_to or end_s],
        "timeframe": "M1 сигналы · H1 график",
        "utc_offset_hours": UTC_OFFSET,
        "min_trades": MIN_TRADES,
        "symbols": list(charts.keys()),
        "symbols_n": len(charts),
        "default_symbol": default,
        "modes": {k: {kk: vv for kk, vv in v.items() if kk != "books"} | {
            "books": v["books"],
        } for k, v in modes.items()},
        "optimized": optimized,
        "combined_pnl_all_modes": round(sum(m["pnl"] for m in modes.values()), 2),
        "trades": all_trades[:500],
        "trades_total": len(all_trades),
        "charts": {k: {
            "candles_m5": v["candles_m5"],
            "markers": v["markers"],
            "trades": v["trades"],
            "best_mode": v["best_mode"],
            "period": v["period"],
        } for k, v in charts.items()},
        "note": (
            f"Год (~{a.days}д). На каждый актив выбран режим с макс. P&L "
            f"(мин. {MIN_TRADES} сделок). Маршрут: data/news_routes.json. "
            "Календарь — только USD High из FRED."
        ),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "news_pulse_report.json").write_text(_json_dump(payload), encoding="utf-8")
    (OUT / "news_pulse_report.html").write_text(
        HTML.replace("__PAYLOAD__", _json_dump(payload)), encoding="utf-8")
    con.print(f"[green]Отчёт → {OUT / 'news_pulse_report.html'}[/]")


HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>News Pulse · год · лучший режим на актив</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{
  --bg:#0b0d10; --panel:#14181f; --line:#243041; --text:#eef2f7; --muted:#8b97a8;
  --green:#3dd6c3; --red:#ff7b78; --amber:#ffc107;
  --mono:ui-monospace,Consolas,monospace; --sans:system-ui,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--sans)}
header{padding:18px 22px 12px;border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,#121722 0%,var(--bg) 100%)}
header h1{margin:0;font:600 20px/1.2 var(--sans)}
header .sub{color:var(--muted);margin:6px 0 0;font-size:13px;max-width:920px}
.kpis{display:flex;flex-wrap:wrap;gap:18px;padding:14px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
.kpis div span{display:block;color:var(--muted);font-size:12px}
.kpis div b{font:600 18px/1.2 var(--mono)}
.pos{color:var(--green)}.neg{color:var(--red)}
.modes{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;padding:14px 22px;border-bottom:1px solid var(--line)}
@media(max-width:900px){.modes{grid-template-columns:1fr}}
.mode{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.mode h3{margin:0 0 6px;font:600 15px var(--sans)}
.mode .desc{color:var(--muted);font-size:12px;min-height:36px}
.mode .pnl{font:700 22px/1.2 var(--mono);margin-top:8px}
.mode .meta{color:var(--muted);font:12px var(--mono);margin-top:6px}
.opt{margin:14px 22px;padding:14px 16px;border-radius:10px;border:1px solid rgba(61,214,195,.35);background:rgba(61,214,195,.06)}
.opt h2{margin:0 0 6px;font:700 15px var(--sans)}
.opt p{margin:0;color:var(--muted);font-size:13px}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
.toolbar select,.toolbar button{background:#1a2230;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 11px;font:600 12px var(--sans)}
#pxChart{height:420px;background:#080a0d;border-bottom:1px solid var(--line)}
.grid{display:grid;grid-template-columns:1.1fr .9fr;gap:0;border-bottom:1px solid var(--line)}
@media(max-width:960px){.grid{grid-template-columns:1fr}}
.panel{padding:14px 18px;border-right:1px solid var(--line)}
.panel:last-child{border-right:0}
.panel h2{margin:0 0 10px;font:600 12px var(--sans);color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
table{width:100%;border-collapse:collapse;font:12px/1.35 var(--mono)}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{color:var(--muted);font:600 11px var(--sans);position:sticky;top:0;background:#151b24}
.trades{max-height:320px;overflow:auto}
.trades tr{cursor:pointer}
.trades tr:hover{background:rgba(91,159,212,.08)}
.trades tr.active{background:rgba(201,162,39,.16)}
.side-buy{color:var(--green)}.side-sell{color:var(--red)}
.note{padding:12px 22px 24px;color:var(--muted);font-size:12px}
.best{outline:1px solid rgba(61,214,195,.4);background:rgba(61,214,195,.07)}
.legend{font-size:11px;color:var(--muted);margin-left:8px}
</style>
</head>
<body>
<header>
  <h1>News Pulse · год · лучший режим на каждый актив</h1>
  <p class="sub" id="sub">загрузка…</p>
</header>
<section class="kpis" id="kpis"></section>
<div class="opt" id="opt"></div>
<section class="modes" id="modes"></section>
<div class="toolbar">
  <strong id="chartTitle">H1</strong>
  <select id="symSel"></select>
  <button type="button" onclick="fitAll()">весь период</button>
  <span class="legend">на графике только выбранный для актива режим · клик по сделке = зум</span>
</div>
<div id="pxChart"></div>
<div class="grid">
  <div class="panel">
    <h2>Маршрут: какой режим на каком активе</h2>
    <div class="trades"><table>
      <thead><tr><th>символ</th><th>режим</th><th>сделок</th><th>WR%</th><th>P&L</th><th>impulse</th><th>fade</th><th>straddle</th></tr></thead>
      <tbody id="routes"></tbody>
    </table></div>
  </div>
  <div class="panel">
    <h2>Сделки выбранного актива</h2>
    <div class="trades"><table>
      <thead><tr><th>#</th><th>side</th><th>pnl</th><th>why</th><th>время</th></tr></thead>
      <tbody id="trades"></tbody>
    </table></div>
  </div>
</div>
<p class="note" id="note"></p>
<script type="application/json" id="data">__PAYLOAD__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const money = v => (v<0?"−":"+")+"$"+Math.abs(v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const MODE_RU = {
  impulse:{title:"Импульс",desc:"По направлению сильной свечи после новости",color:"#3dd6c3"},
  fade:{title:"Откат",desc:"Против спайка на развороте",color:"#5b9fd4"},
  straddle:{title:"Пробой OCO",desc:"Два стопа до новости",color:"#b388ff"},
};
let sym = D.default_symbol;
let pxChart, candleSeries, priceLines=[];

document.getElementById("sub").textContent =
  `${D.venue} · ${D.mode_label} · ${D.broker} · ` +
  `${D.period[0]} → ${D.period[1]} (~${D.days}д) · ` +
  `депозит $${D.deposit} · риск ${D.risk_pct}% · RR ${D.rr} · ` +
  `${D.calendar} (${D.calendar_events}) · инструментов ${D.symbols_n} · ${D.timeframe}`;

const O = D.optimized;
document.getElementById("kpis").innerHTML = [
  ["Период", `~${D.days}д`, ""],
  ["Депозит", "$"+D.deposit, ""],
  ["Риск", D.risk_pct+"%", ""],
  ["Оптим. P&L", money(O.pnl), O.pnl>=0?"pos":"neg"],
  ["Конец $", money(O.end).replace("+",""), O.end>=D.deposit?"pos":"neg"],
  ["Сделок (маршрут)", String(O.trades), ""],
].map(([a,b,c])=>`<div><span>${a}</span><b class="${c}">${b}</b></div>`).join("");

document.getElementById("opt").innerHTML =
  `<h2>Как читается</h2><p>Прогнали <b>все 3</b> режима на каждом активе за год. ` +
  `В таблицу маршрута попал только <b>лучший по P&L</b> (минимум ${D.min_trades} сделок). ` +
  `Итоговый P&L — сумма лучших, а не сумма всех трёх сразу. ` +
  `На графике — сделки только выбранного режима для этого актива.</p>`;

document.getElementById("modes").innerHTML = ["impulse","fade","straddle"].map(m=>{
  const x=D.modes[m], ru=MODE_RU[m];
  return `<div class="mode">
    <h3 style="color:${ru.color}">${ru.title} <span style="color:var(--muted);font-weight:500">(все активы)</span></h3>
    <div class="desc">${ru.desc}</div>
    <div class="pnl ${x.pnl>=0?"pos":"neg"}">${money(x.pnl)}</div>
    <div class="meta">${x.trades} сделок · WR ${x.winrate}% — если гнать этот режим везде</div>
  </div>`;
}).join("");

const sel=document.getElementById("symSel");
(D.symbols||[]).forEach(s=>{
  const o=document.createElement("option"); o.value=s;
  const r=(O.routes||{})[s];
  o.textContent = r ? `${s} · ${MODE_RU[r.mode].title}` : s;
  if(s===sym) o.selected=true; sel.appendChild(o);
});
sel.onchange=()=>{ sym=sel.value; paint(); fill(); };

function fillRoutes(){
  const rows=Object.entries(O.routes||{}).sort((a,b)=>b[1].pnl-a[1].pnl);
  document.getElementById("routes").innerHTML = rows.map(([s,r])=>{
    const c=r.comparison||{};
    const cell = m => {
      const x=c[m]; if(!x) return "—";
      const cls = m===r.mode ? "best" : "";
      return `<td class="${cls} ${x.pnl>=0?"pos":"neg"}">${money(x.pnl)}</td>`;
    };
    return `<tr onclick="sel.value='${s}';sym='${s}';paint();fill()">
      <td>${s}</td>
      <td style="color:${MODE_RU[r.mode].color}">${MODE_RU[r.mode].title}</td>
      <td>${r.trades}</td><td>${r.winrate}</td>
      <td class="${r.pnl>=0?"pos":"neg"}">${money(r.pnl)}</td>
      ${cell("impulse")}${cell("fade")}${cell("straddle")}
    </tr>`;
  }).join("");
}

function symTrades(){ return ((D.charts[sym]||{}).trades)||[]; }

function mkChart(el){
  return LightweightCharts.createChart(el,{
    layout:{background:{color:"#080a0d"},textColor:"#8b97a8"},
    grid:{vertLines:{color:"#1a2230"},horzLines:{color:"#1a2230"}},
    rightPriceScale:{borderColor:"#243041"},
    timeScale:{borderColor:"#243041",timeVisible:true,secondsVisible:false},
  });
}

function paint(){
  const el=document.getElementById("pxChart");
  const r=(O.routes||{})[sym];
  const modeName = r ? MODE_RU[r.mode].title : "?";
  document.getElementById("chartTitle").textContent = `${sym} · H1 · ${modeName}`;
  if(pxChart){pxChart.remove();pxChart=null}
  pxChart=mkChart(el);
  candleSeries=pxChart.addCandlestickSeries({
    upColor:"#26a69a",downColor:"#ef5350",borderVisible:false,
    wickUpColor:"#26a69a",wickDownColor:"#ef5350",
  });
  const ch=D.charts[sym]||{candles_m5:[]};
  candleSeries.setData((ch.candles_m5||[]).map(c=>({time:c.time,open:c.open,high:c.high,low:c.low,close:c.close})));
  const marks=symTrades().flatMap(t=>[
    {time:t.entry_time,position:t.side==="buy"?"belowBar":"aboveBar",
     color:t.side==="buy"?"#3dd6c3":"#ff7b78",
     shape:t.side==="buy"?"arrowUp":"arrowDown",text:"вход"},
    {time:t.exit_time,position:"aboveBar",
     color:String(t.reason).startsWith("tp")?"#ffc107":"#ef5350",
     shape:"circle",text:String(t.reason).startsWith("tp")?"TP":"SL"},
  ]).filter(m=>m.time);
  candleSeries.setMarkers(marks.sort((a,b)=>a.time-b.time));
  pxChart.timeScale().fitContent();
  new ResizeObserver(()=>pxChart.applyOptions({width:el.clientWidth,height:el.clientHeight})).observe(el);
}

function fill(){
  const rows=symTrades();
  document.getElementById("trades").innerHTML = rows.length? rows.map((t,i)=>`<tr onclick="zoom(${i})">
    <td>${i+1}</td><td class="side-${t.side}">${t.side}</td>
    <td class="${t.pnl>=0?"pos":"neg"}">${money(t.pnl)}</td>
    <td>${t.reason}</td><td>${(t.opened_at||"").slice(0,16)}</td>
  </tr>`).join("") : "<tr><td colspan=5>нет сделок</td></tr>";
}

function clearLines(){
  if(!candleSeries)return;
  for(const pl of priceLines){try{candleSeries.removePriceLine(pl)}catch(e){}}
  priceLines=[];
}
function zoom(i){
  const t=symTrades()[i]; if(!t||!pxChart)return;
  clearLines();
  pxChart.timeScale().setVisibleRange({from:t.entry_time-12*3600,to:t.exit_time+12*3600});
  if(t.sl!=null) priceLines.push(candleSeries.createPriceLine({price:+t.sl,color:"#ef5350",lineWidth:2,lineStyle:2,title:"SL"}));
  if(t.tp!=null) priceLines.push(candleSeries.createPriceLine({price:+t.tp,color:"#ffc107",lineWidth:2,lineStyle:2,title:"TP"}));
  priceLines.push(candleSeries.createPriceLine({price:+t.entry,color:"#5b9fd4",lineWidth:2,title:"IN"}));
}
function fitAll(){clearLines(); if(pxChart) pxChart.timeScale().fitContent()}

document.getElementById("note").textContent = D.note;
fillRoutes(); paint(); fill();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
