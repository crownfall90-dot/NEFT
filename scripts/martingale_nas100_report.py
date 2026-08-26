"""Мартингейл на NAS100 · Bybit CFD (MT5) · 90 дней.

Депозит $1000, базовый риск 0.2% на сделку, комиссия Bybit Tight-Spread
($3/лот при открытии) + спред из истории MT5.

    python scripts/martingale_nas100_report.py
    python scripts/martingale_nas100_report.py --days 90 --sl 20 --tp 20

Отчёт: dashboard/martingale_nas100.html
       dashboard/martingale_nas100.json
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

import pandas as pd
from rich.console import Console
from rich.table import Table

from neft.backtest import data, metrics
from neft.backtest.engine import Backtester
from neft.core import symbols
from neft.core.bybit_cfd_fees import commission_per_lot, costs_for
from neft.core.config import ROOT
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.martingale import Martingale

con = Console()
OUT = ROOT / "dashboard"
SYMBOL = "NAS100"
BALANCE = 1000.0
RISK_PCT = 0.2
DAYS = 90
MULT = 2.0
MAX_STEPS = 5
LEVERAGE = 100
# NY cash на сервере Bybit UTC+3 ≈ 16:30–22:00.
SESSION = (16, 22)
EMA = 50
SL_ATR = 1.25
RR = 1.15


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


def _trade_rows(trades) -> list[dict]:
    rows = []
    for t in trades:
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
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
            "sl": float(t.sl) if getattr(t, "sl", None) is not None else None,
            "tp": float(t.tp) if getattr(t, "tp", None) is not None else None,
            "why": getattr(t, "signal_reason", "") or "",
            "label": "TP" if (t.reason or "").startswith("tp") else (
                "SL" if t.reason == "sl" else (t.reason or "out").upper()
            ),
        })
    return rows


def _markers(trades) -> list[dict]:
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
                "position": "belowBar" if buy else "aboveBar",
                "color": "#3dd6c3" if buy else "#ff7b78",
                "shape": "circle",
            })
        if t.closed_at is not None:
            is_tp = (t.reason or "").startswith("tp")
            marks.append({
                "time": _ts(t.closed_at),
                "price": float(t.exit),
                "kind": "exit",
                "side": side,
                "reason": t.reason,
                "label": "TP" if is_tp else "SL",
                "position": "aboveBar" if is_tp else "belowBar",
                "color": "#ffc107" if is_tp else "#ef5350",
                "shape": "circle",
            })
    return marks


def _equity_curve(eq: pd.Series, points: int = 1200) -> list[dict]:
    step = max(1, len(eq) // points)
    return [{"t": str(i), "v": round(float(v), 2)} for i, v in eq.iloc[::step].items()]


def run(
    df: pd.DataFrame, *,
    balance: float, risk: float,
    mult: float, steps: int, leverage: int,
    session: tuple[int, int] | None,
    ema: int, sl_atr: float, rr: float,
    sl_points: float | None, tp_points: float | None,
) -> tuple[Martingale, object, metrics.Metrics]:
    spec = symbols.load(SYMBOL)
    fee = commission_per_lot(SYMBOL)
    # Мартингейл наращивает риск: потолок широкий, чтобы серия жила до max_steps.
    limits = RiskLimits(
        risk_per_trade_pct=risk,
        max_risk_per_trade_pct=max(40.0, risk * (mult ** max(steps - 1, 0)) * 1.25),
        min_risk_per_trade_pct=0.0,
        max_volume=float(spec.volume_max),
        max_daily_loss_pct=100.0,
        max_drawdown_pct=100.0,
        min_free_margin_pct=0.0,
    )
    rm = RiskManager(start_balance=balance, limits=limits)
    kw: dict = dict(
        base_volume=float(spec.volume_min),
        risk_pct=risk,
        risk_manager=rm,
        spec=spec,
        multiplier=mult,
        max_steps=steps,
        session=session,
        ema_period=ema,
        sl_atr=sl_atr,
        rr=rr,
        commission_per_lot=fee,
        use_atr=sl_points is None,
        cooldown_bars=3,
        reset_cooldown_bars=18,
    )
    if sl_points is not None:
        kw["sl_points"] = sl_points
        kw["tp_points"] = tp_points if tp_points is not None else sl_points
    strat = Martingale(**kw)
    costs = costs_for(spec, leverage=leverage)
    res = Backtester(strat, rm, costs, start_balance=balance, symbol=SYMBOL).run(df)
    m = metrics.compute(res.equity, res.trades, balance, res.ruined)
    return strat, res, m


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=DAYS)
    p.add_argument("--balance", type=float, default=BALANCE)
    p.add_argument("--risk", type=float, default=RISK_PCT,
                   help="базовый риск %% депозита (по умолчанию 0.2)")
    p.add_argument("--sl", type=float, default=None,
                   help="фикс SL в пунктах цены; по умолчанию ATR")
    p.add_argument("--tp", type=float, default=None,
                   help="фикс TP; по умолчанию RR×SL + комиссионная подушка")
    p.add_argument("--sl-atr", type=float, default=SL_ATR)
    p.add_argument("--rr", type=float, default=RR)
    p.add_argument("--ema", type=int, default=EMA)
    p.add_argument("--mult", type=float, default=MULT)
    p.add_argument("--steps", type=int, default=MAX_STEPS)
    p.add_argument("--leverage", type=int, default=LEVERAGE)
    p.add_argument("--session", default="16-22",
                   help="часы сервера UTC+3, напр. 16-22; all = 24h")
    p.add_argument("--tf", default="M5", choices=("M1", "M5", "M15"),
                   help="ТФ сигналов")
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args()

    if args.session in ("all", "24h", "-", ""):
        session = None
        sess_label = "24h"
    else:
        lo, hi = (int(x) for x in args.session.split("-", 1))
        session = (lo, hi)
        sess_label = f"{lo}-{hi}"

    mode = (f"ATR×{args.sl_atr} RR={args.rr}" if args.sl is None
            else f"SL/TP {args.sl}/{args.tp or args.sl}")
    con.print(f"[cyan]NAS100[/] · мартингейл · депозит ${args.balance:.0f} · "
              f"риск {args.risk}% · {mode} · EMA{args.ema} · "
              f"сессия {sess_label} · ×{args.mult} · max_steps={args.steps}")

    df_m1 = data.load_days(SYMBOL, "M1", args.days, refresh=args.refresh)
    rule = {"M1": "1min", "M5": "5min", "M15": "15min"}[args.tf]
    df = df_m1 if args.tf == "M1" else data.resample_ohlc(df_m1, rule)
    con.print(f"[dim]{SYMBOL} {args.tf}: {len(df)} баров, "
              f"{df.time.iloc[0]} — {df.time.iloc[-1]}[/]")

    spec = symbols.load(SYMBOL)
    fee = commission_per_lot(SYMBOL)
    con.print(f"[dim]контракт={spec.contract_size}  point={spec.point}  "
              f"лот min/step={spec.volume_min}/{spec.volume_step}  "
              f"комиссия Bybit ${fee}/лот (open)  плечо {args.leverage}x[/]")

    strat, res, m = run(
        df, balance=args.balance, risk=args.risk,
        mult=args.mult, steps=args.steps, leverage=args.leverage,
        session=session, ema=args.ema, sl_atr=args.sl_atr, rr=args.rr,
        sl_points=args.sl, tp_points=args.tp,
    )

    # Комиссии суммарно: Bybit только на открытии.
    total_commission = sum(fee * t.volume for t in res.trades)

    color = "green" if m.net_profit > 0 else "red"
    t = Table(title="Мартингейл · NAS100 · 3 месяца", box=None, pad_edge=False)
    t.add_column("метрика", style="dim")
    t.add_column("значение", justify="right")
    t.add_row("Период", f"{df.time.iloc[0]} — {df.time.iloc[-1]}")
    t.add_row("Стартовый баланс", f"${m.start_balance:,.2f}")
    t.add_row("Итоговый баланс", f"[{color}]${m.end_balance:,.2f}[/]")
    t.add_row("Прибыль", f"[{color}]${m.net_profit:,.2f} ({m.return_pct:+.2f}%)[/]")
    t.add_row("Сделок", f"{m.trades}")
    t.add_row("Винрейт", f"{m.win_rate:.1f}%  ({m.wins}W / {m.losses}L)")
    t.add_row("Profit factor", f"{m.profit_factor:.3f}")
    t.add_row("Макс. просадка", f"[red]{m.max_drawdown_pct:.1f}%  "
                                f"(${m.max_drawdown_abs:,.2f})[/]")
    t.add_row("Макс. серия убытков", f"[red]{m.max_loss_streak}[/]")
    t.add_row("Крупнейший win / loss",
              f"${m.largest_win:,.2f} / ${m.largest_loss:,.2f}")
    t.add_row("Матожидание", f"${m.expectancy:+.4f}")
    t.add_row("Комиссии Bybit (сумма)", f"${total_commission:,.2f}")
    t.add_row("Потолок серии (сбросы)", f"{strat.resets}")
    t.add_row("Макс. шаг мартингейла", f"{strat.max_step_seen}")
    t.add_row("Отказов риск-слоя", f"{strat.blocked_by_risk}")
    t.add_row("Отклонено сигналов", f"{len(res.rejected)}")
    t.add_row("Сетапов / skip sess/filt/cd",
              f"{strat.setups_seen} / {strat.skipped_session}/"
              f"{strat.skipped_filter}/{strat.skipped_cooldown}")
    con.print(t)

    if res.ruined:
        con.print(f"\n[bold red]СЧЁТ СЛИТ[/] — {res.ruin_time}")
    if res.halt_reason:
        con.print(f"[yellow]Kill-switch:[/] {res.halt_reason}")

    # Типичный SL для лестницы: медиана ATR×k или фикс.
    if strat.df is not None and "atr" in strat.df.columns and args.sl is None:
        typ_sl = float(strat.df["atr"].median()) * float(args.sl_atr)
    else:
        typ_sl = float(args.sl or 20.0)
    base_lot = strat._base_lot(typ_sl)
    ladder = []
    for n in range(args.steps):
        lot = round(base_lot * args.mult ** n, 2)
        loss = lot * typ_sl * float(spec.contract_size)
        ladder.append({
            "step": n,
            "lot": lot,
            "sl_loss": round(loss, 2),
            "risk_pct": round(loss / args.balance * 100, 3),
            "open_fee": round(fee * lot, 2),
        })

    overview = data.resample_ohlc(df, "15min" if args.tf != "M15" else "1h")
    trades_rows = _trade_rows(res.trades)
    marks = _markers(res.trades)

    # EMA на торговых свечах — линия на графике.
    ema_line = []
    if strat.df is not None and "ema" in strat.df.columns:
        for r in strat.df.itertuples(index=False):
            if r.ema == r.ema:  # not NaN
                ema_line.append({"time": _ts(r.time), "value": float(r.ema)})

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": SYMBOL,
        "broker": "Bybit TradFi MT5",
        "fee_model": "Tight-Spread: commission on open only",
        "commission_per_lot": fee,
        "timeframe": args.tf,
        "bars": len(df),
        "period": [str(df.time.iloc[0]), str(df.time.iloc[-1])],
        "days": max(1, (df.time.iloc[-1] - df.time.iloc[0]).days),
        "params": {
            "balance": args.balance,
            "risk_pct": args.risk,
            "sl_points": args.sl,
            "tp_points": args.tp,
            "sl_atr": args.sl_atr,
            "rr": args.rr,
            "ema": args.ema,
            "session": sess_label,
            "typical_sl": round(typ_sl, 2),
            "multiplier": args.mult,
            "max_steps": args.steps,
            "leverage": args.leverage,
            "contract_size": float(spec.contract_size),
            "point": float(spec.point),
            "volume_min": float(spec.volume_min),
            "volume_step": float(spec.volume_step),
            "spread_default": float(spec.default_spread),
            "setups": int(strat.setups_seen),
            "skipped_session": int(strat.skipped_session),
            "skipped_filter": int(strat.skipped_filter),
            "skipped_cooldown": int(strat.skipped_cooldown),
        },
        "metrics": m.as_dict(),
        "total_commission": round(total_commission, 2),
        "resets": strat.resets,
        "max_step": strat.max_step_seen,
        "blocked_by_risk": strat.blocked_by_risk,
        "rejected": len(res.rejected),
        "ruined": res.ruined,
        "ruin_time": str(res.ruin_time) if res.ruin_time else None,
        "ladder": ladder,
        "equity": _equity_curve(res.equity),
        "trades": trades_rows,
        "markers": marks,
        "ema": ema_line,
        "candles_trade": _candles(df),
        "candles_overview": _candles(overview),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    report_json = OUT / "martingale_nas100.json"
    report_json.write_text(_json_dump(payload), encoding="utf-8")
    con.print(f"[dim]JSON → {report_json}[/]")

    # HTML-обёртка: данные инлайном, чтобы открыть без сервера.
    html_src = (OUT / "martingale_nas100_template.html")
    if not html_src.exists():
        # Шаблон создаётся рядом со скриптом при первом запуске — см. write ниже.
        pass
    html_path = OUT / "martingale_nas100.html"
    template = HTML_TEMPLATE.replace("__PAYLOAD__", _json_dump(payload))
    html_path.write_text(template, encoding="utf-8")
    con.print(f"[green]Отчёт → {html_path}[/]")
    con.print(f"[dim]Открыть файл в браузере или через admin: /martingale_nas100.html[/]")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Мартингейл · NAS100 · Bybit CFD</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{
  --bg:#0b0d10; --panel:#14181f; --line:#243041; --text:#eef2f7; --muted:#8b97a8;
  --green:#26a69a; --green-bright:#3dd6c3; --red:#ef5350; --red-bright:#ff7b78;
  --amber:#ffc107; --ema:#c9a227; --mono:ui-monospace,"SF Mono",Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--sans)}
header{padding:18px 22px 12px;border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,#121722 0%,var(--bg) 100%)}
header h1{margin:0;font:600 20px/1.2 var(--sans);letter-spacing:-.02em}
header .sub{color:var(--muted);margin:6px 0 0;font-size:13px}
.kpis{display:flex;flex-wrap:wrap;gap:18px;padding:14px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
.kpis div span{display:block;color:var(--muted);font-size:12px}
.kpis div b{font:600 18px/1.2 var(--mono)}
.pos{color:var(--green-bright)}.neg{color:var(--red-bright)}
.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:0;border-bottom:1px solid var(--line)}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.panel{padding:14px 18px;border-right:1px solid var(--line)}
.panel:last-child{border-right:0}
.panel h2{margin:0 0 10px;font:600 13px var(--sans);color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
#eqChart{height:220px;background:#080a0d;border-radius:6px}
.chart-wrap{position:relative;border-bottom:1px solid var(--line);background:#080a0d}
.toolbar{display:flex;gap:8px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap;background:var(--panel)}
.toolbar button{background:#1a2230;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 11px;font:600 12px var(--sans);cursor:pointer}
.toolbar button:hover{border-color:#3a4a60}
.toolbar button.active{border-color:var(--ema);color:var(--ema);background:rgba(201,162,39,.12)}
.legend{display:flex;gap:12px;flex-wrap:wrap;margin-left:8px}
.legend span{font-size:11px;color:var(--muted)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px;vertical-align:-1px}
#pxChart{height:520px}
table{width:100%;border-collapse:collapse;font:12px/1.35 var(--mono)}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{color:var(--muted);font:600 11px var(--sans);position:sticky;top:0;background:#151b24;z-index:1}
.trades{max-height:280px;overflow:auto;padding:0 0 8px;background:var(--panel)}
.trades tr{cursor:pointer}
.trades tr:hover{background:rgba(91,159,212,.08)}
.trades tr.active{background:rgba(201,162,39,.16);outline:1px solid rgba(201,162,39,.3)}
.side-buy{color:var(--green-bright)}.side-sell{color:var(--red-bright)}
.note{padding:12px 22px;color:var(--muted);font-size:12px;border-top:1px solid var(--line)}
</style>
</head>
<body>
<header>
  <h1>Мартингейл · NAS100 · Bybit CFD (MT5)</h1>
  <p class="sub" id="sub">загрузка…</p>
</header>
<section class="kpis" id="kpis"></section>
<div class="grid">
  <div class="panel">
    <h2>Эквити</h2>
    <div id="eqChart"></div>
  </div>
  <div class="panel">
    <h2>Лестница (типичный ATR-SL)</h2>
    <table>
      <thead><tr><th>шаг</th><th>лот</th><th>убыток SL</th><th>риск %</th><th>комиссия</th></tr></thead>
      <tbody id="ladder"></tbody>
    </table>
  </div>
</div>
<div class="toolbar">
  <strong id="chartTitle">Цена</strong>
  <button type="button" id="btnTrade" class="active" onclick="setTf('trade')">M5 · сделки</button>
  <button type="button" id="btnOver" onclick="setTf('overview')">обзор</button>
  <button type="button" id="btnEma" class="active" onclick="toggleEma()">EMA</button>
  <button type="button" onclick="fitAll()">весь период</button>
  <div class="legend">
    <span><i style="background:#c9a227"></i>EMA</span>
    <span><i style="background:#3dd6c3"></i>вход buy</span>
    <span><i style="background:#ff7b78"></i>вход sell</span>
    <span><i style="background:#ffc107"></i>TP</span>
    <span><i style="background:#ef5350"></i>SL</span>
  </div>
</div>
<div class="chart-wrap"><div id="pxChart"></div></div>
<div class="trades">
  <table>
    <thead><tr><th>#</th><th>side</th><th>лот</th><th>вход</th><th>выход</th><th>pnl</th><th>why</th><th></th><th>время</th></tr></thead>
    <tbody id="trades"></tbody>
  </table>
</div>
<p class="note" id="note"></p>
<script type="application/json" id="data">__PAYLOAD__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const money = v => (v<0?"−":"") + "$" + Math.abs(v).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const pct = v => (v>=0?"+":"") + v.toFixed(2) + "%";
const m = D.metrics;
const cls = m.net_profit >= 0 ? "pos" : "neg";
const P = D.params || {};
let tf = "trade", useEma = true, pxChart, candleSeries, emaSeries, priceLines = [];
let selected = -1;

document.getElementById("sub").textContent =
  `${D.symbol} · ${D.timeframe} · ${D.period[0]} → ${D.period[1]} · ${D.bars.toLocaleString("ru-RU")} баров · ` +
  `депозит $${P.balance} · риск ${P.risk_pct}% · ` +
  (P.sl_points!=null ? `SL/TP ${P.sl_points}/${P.tp_points}` : `ATR×${P.sl_atr} RR=${P.rr} · EMA${P.ema} · сессия ${P.session}`) +
  ` · комиссия $${D.commission_per_lot}/лот · ${D.broker}`;

document.getElementById("chartTitle").textContent = `${D.symbol} · ${D.timeframe}`;

document.getElementById("kpis").innerHTML = [
  ["Итог", money(m.end_balance), cls],
  ["P&L", `${money(m.net_profit)} (${pct(m.return_pct)})`, cls],
  ["Сделок", String(m.trades), ""],
  ["Win rate", `${m.win_rate.toFixed(1)}%`, ""],
  ["PF", m.profit_factor.toFixed(2), ""],
  ["Max DD", `${m.max_drawdown_pct.toFixed(1)}%`, "neg"],
  ["Серия SL", String(m.max_loss_streak), "neg"],
  ["Комиссии", money(D.total_commission||0), ""],
  ["Макс шаг", String(D.max_step), ""],
  ["Сбросы", String(D.resets), ""],
].map(([k,v,c]) => `<div><span>${k}</span><b class="${c}">${v}</b></div>`).join("");

document.getElementById("ladder").innerHTML = (D.ladder||[]).map(r =>
  `<tr><td>${r.step}</td><td>${r.lot}</td><td>${money(r.sl_loss)}</td><td>${r.risk_pct}%</td><td>${money(r.open_fee)}</td></tr>`
).join("");

const trades = D.trades || [];
document.getElementById("trades").innerHTML = trades.map((t,i) =>
  `<tr data-i="${i}" onclick="focusTrade(${i})">
    <td>${i+1}</td>
    <td class="side-${t.side}">${t.side}</td>
    <td>${t.volume}</td>
    <td>${t.entry.toFixed(2)}</td>
    <td>${t.exit.toFixed(2)}</td>
    <td class="${t.pnl>=0?"pos":"neg"}">${money(t.pnl)}</td>
    <td>${t.why||""}</td>
    <td>${t.label||t.reason}</td>
    <td>${(t.opened_at||"").slice(5,16)}</td>
  </tr>`
).join("");

document.getElementById("note").textContent =
  `Клик по сделке — зум + линии SL/TP. Вход: откат к EMA по тренду, сессия NY. ` +
  `Bybit Tight-Spread: $${D.commission_per_lot}/лот на открытии. ` +
  (D.ruined ? `СЧЁТ СЛИТ @ ${D.ruin_time}.` : "");

function mkChart(el) {
  return LightweightCharts.createChart(el, {
    layout: { background: { color: "#080a0d" }, textColor: "#8b97a8" },
    grid: { vertLines: { color: "#1a2230" }, horzLines: { color: "#1a2230" } },
    rightPriceScale: { borderColor: "#243041" },
    timeScale: { borderColor: "#243041", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0 },
  });
}

function parseT(s){
  const raw = String(s||"").trim().replace(" ","T");
  if(!raw) return 0;
  const withZ = /Z$|[+-]\d{2}:?\d{2}$/.test(raw) ? raw : raw+"Z";
  const ms = Date.parse(withZ);
  return ms ? Math.floor(ms/1000) : 0;
}

(function equity() {
  const el = document.getElementById("eqChart");
  const chart = mkChart(el);
  const s = chart.addAreaSeries({
    lineColor: m.net_profit>=0 ? "#3dd6c3" : "#ff7b78",
    topColor: m.net_profit>=0 ? "rgba(61,214,195,.25)" : "rgba(255,123,120,.25)",
    bottomColor: "rgba(0,0,0,0)", lineWidth: 2,
  });
  s.setData((D.equity||[]).map(p => ({time: parseT(p.t), value: p.v})).filter(x => x.time));
  chart.timeScale().fitContent();
  new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth, height: el.clientHeight })).observe(el);
})();

function clearLines(){
  if(!candleSeries) return;
  for(const pl of priceLines){ try{ candleSeries.removePriceLine(pl); }catch(e){} }
  priceLines = [];
}

function markerList(){
  return (D.markers||[]).map(mk => ({
    time: mk.time,
    position: mk.position || (mk.kind==="entry" ? "belowBar" : "aboveBar"),
    color: mk.color,
    shape: mk.kind==="entry" ? (mk.side==="buy"?"arrowUp":"arrowDown") : "circle",
    text: mk.kind==="exit" ? (mk.label||"") : "",
  })).filter(x => x.time);
}

function paintPrice(){
  const el = document.getElementById("pxChart");
  if(pxChart){ pxChart.remove(); pxChart = null; }
  pxChart = mkChart(el);
  candleSeries = pxChart.addCandlestickSeries({
    upColor:"#26a69a", downColor:"#ef5350", borderVisible:false,
    wickUpColor:"#26a69a", wickDownColor:"#ef5350",
  });
  const src = tf==="trade" ? (D.candles_trade||[]) : (D.candles_overview||D.candles_trade||[]);
  candleSeries.setData(src.map(c => ({time:c.time, open:c.open, high:c.high, low:c.low, close:c.close})));
  candleSeries.setMarkers(markerList());

  emaSeries = pxChart.addLineSeries({
    color: "#c9a227", lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
  });
  const emaSrc = (D.ema||[]).filter(p => p.time);
  // На обзоре EMA с M5 слишком плотная — рисуем как есть, LWCharts сам редюсит.
  if(useEma && emaSrc.length) emaSeries.setData(emaSrc);
  else emaSeries.setData([]);

  pxChart.timeScale().fitContent();
  new ResizeObserver(() => pxChart.applyOptions({ width: el.clientWidth, height: el.clientHeight })).observe(el);
  if(selected >= 0) zoomTrade(selected);
}

function setTf(mode){
  tf = mode;
  document.getElementById("btnTrade").classList.toggle("active", mode==="trade");
  document.getElementById("btnOver").classList.toggle("active", mode==="overview");
  paintPrice();
}
function toggleEma(){
  useEma = !useEma;
  document.getElementById("btnEma").classList.toggle("active", useEma);
  paintPrice();
}
function fitAll(){ selected = -1; clearLines(); highlight(-1); if(pxChart) pxChart.timeScale().fitContent(); }

function highlight(i){
  [...document.querySelectorAll("#trades tr[data-i]")].forEach(tr =>
    tr.classList.toggle("active", +tr.dataset.i === i));
}

function focusTrade(i){
  selected = i;
  highlight(i);
  if(tf !== "trade"){ setTf("trade"); return; }
  zoomTrade(i);
}

function zoomTrade(i){
  const t = trades[i];
  if(!t || !pxChart || !candleSeries) return;
  clearLines();
  const t0 = parseT(t.opened_at);
  const t1 = parseT(t.closed_at) || t0;
  if(!t0) return;
  const pad = 40 * ({M1:60,M5:300,M15:900}[D.timeframe]||300);
  pxChart.timeScale().setVisibleRange({ from: t0 - pad, to: t1 + pad });
  const add = (price, color, title) => {
    if(price==null) return;
    priceLines.push(candleSeries.createPriceLine({
      price: +price, color, lineWidth: 1, lineStyle: 2,
      axisLabelVisible: true, title,
    }));
  };
  add(t.entry, "#5b9fd4", "IN");
  add(t.sl, "#ef5350", "SL");
  add(t.tp, "#ffc107", "TP");
  add(t.exit, t.pnl>=0 ? "#3dd6c3" : "#ff7b78", "OUT");
}

paintPrice();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
