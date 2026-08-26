"""Мои CFD-боты (не HSS / London / Martingale) · топ-3 на $1000.

Стратегии:
  • OrbPulse  — пробой NY opening range (30м после 16:30)
  • VwapSnap  — возврат к дневному VWAP после перерастяжения
  • SlowTide  — тренд EMA21/55 + откат к медленной EMA

    python scripts/original_cfd_top3.py
"""
from __future__ import annotations

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
from neft.strategies.coil_break import CoilBreak
from neft.strategies.orb_pulse import OrbPulse
from neft.strategies.slow_tide import SlowTide
from neft.strategies.vwap_snap import VwapSnap

con = Console()
OUT = ROOT / "dashboard"
BALANCE = 1000.0
RISK = 0.5
TARGET_DD = 12.0

ORB_SYMS = ["NAS100", "DJ30", "XAUUSD+", "UKOUSD", "GER40", "FRA40"]
VWAP_SYMS = ["NAS100", "DJ30", "XAUUSD+", "GBPUSD+", "USDJPY+", "EURUSD+",
             "USDCAD+", "UKOUSD", "GER40"]
TIDE_SYMS = ["NAS100", "DJ30", "XAUUSD+", "GBPUSD+", "USDJPY+", "EURJPY+",
             "USDCHF+", "UKOUSD", "GER40", "FRA40"]
COIL_SYMS = ["NAS100", "DJ30", "XAUUSD+", "GER40", "FRA40", "UKOUSD", "GBPUSD+"]


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
        "time": _ts(r.time), "open": float(r.open), "high": float(r.high),
        "low": float(r.low), "close": float(r.close),
    } for r in df.itertuples(index=False)]


def _equity(eq: pd.Series, points: int = 900) -> list[dict]:
    step = max(1, len(eq) // points)
    return [{"t": str(i), "v": round(float(v), 2)} for i, v in eq.iloc[::step].items()]


def _markers(trades) -> list[dict]:
    out = []
    for t in trades:
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        buy = side == "buy"
        if t.opened_at is not None:
            out.append({
                "time": _ts(t.opened_at), "price": float(t.entry),
                "kind": "entry", "side": side,
                "position": "belowBar" if buy else "aboveBar",
                "color": "#3dd6c3" if buy else "#ff7b78",
            })
        if t.closed_at is not None:
            tp = (t.reason or "").startswith("tp")
            out.append({
                "time": _ts(t.closed_at), "price": float(t.exit),
                "kind": "exit", "label": "TP" if tp else "SL",
                "position": "aboveBar" if tp else "belowBar",
                "color": "#ffc107" if tp else "#ef5350",
            })
    return out


def _trades(trades) -> list[dict]:
    rows = []
    for t in trades:
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        rows.append({
            "side": side, "volume": float(t.volume),
            "entry": float(t.entry), "exit": float(t.exit),
            "pnl": round(float(t.pnl), 4), "reason": t.reason,
            "opened_at": str(t.opened_at) if t.opened_at else "",
            "closed_at": str(t.closed_at) if t.closed_at else "",
            "sl": float(t.sl) if getattr(t, "sl", None) is not None else None,
            "tp": float(t.tp) if getattr(t, "tp", None) is not None else None,
            "label": "TP" if (t.reason or "").startswith("tp") else "SL",
        })
    return rows


def load_m5(sym: str) -> pd.DataFrame:
    p = ROOT / "data" / f"{sym.replace('+', 'plus')}_M1_90d.csv"
    if not p.exists():
        raise FileNotFoundError(sym)
    return data.resample_ohlc(pd.read_csv(p, parse_dates=["time"]), "5min")


def score(m: dict) -> float:
    if m.get("ruined") or m.get("trades", 0) < 5:
        return -999.0
    ret = float(m["return_pct"])
    dd = max(float(m["max_drawdown_pct"]), 0.25)
    calmar = ret / dd
    dd_pen = 1.0 if dd <= TARGET_DD else TARGET_DD / dd
    n = int(m["trades"])
    n_bonus = min(1.0, n / 15.0) * (1.0 if n <= 100 else 100 / n)
    wr = float(m["win_rate"]) / 100.0
    return calmar * dd_pen * n_bonus * (0.8 + 0.4 * wr)


def _rm(risk: float = RISK) -> RiskManager:
    return RiskManager(
        start_balance=BALANCE,
        limits=RiskLimits(
            risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
            min_risk_per_trade_pct=0.0, max_volume=100.0,
            max_daily_loss_pct=100.0, max_drawdown_pct=100.0,
            min_free_margin_pct=0.0,
        ),
    )


def pack(title, family, idea, sym, strat, df, res, m) -> dict:
    fee = commission_per_lot(sym)
    return {
        "id": f"{family.lower().replace(' ', '_')}_{sym.replace('+', 'plus')}",
        "title": title,
        "family": family,
        "idea": idea,
        "symbol": sym,
        "metrics": m.as_dict(),
        "score": score(m.as_dict()),
        "equity": _equity(res.equity),
        "markers": _markers(res.trades),
        "trades": _trades(res.trades),
        "candles": _candles(df),
        "commission": round(sum(fee * t.volume for t in res.trades), 2),
        "setups": int(getattr(strat, "setups_seen", 0) or 0),
    }


def run_orb(sym: str, df: pd.DataFrame) -> dict:
    spec = symbols.load(sym)
    rm = _rm()
    strat = OrbPulse(risk_pct=RISK, risk_manager=rm, spec=spec, rr=1.8)
    res = Backtester(strat, rm, costs_for(spec), BALANCE, sym).run(df)
    m = metrics.compute(res.equity, res.trades, BALANCE, res.ruined)
    return pack(
        f"OrbPulse · {sym}", "OrbPulse",
        "Пробой 30м NY opening range после 16:30. Одна сделка в день. "
        "SL за OR, TP 1.8R. Фильтр ширины OR через ATR.",
        sym, strat, df, res, m,
    )


def run_vwap(sym: str, df: pd.DataFrame) -> dict:
    spec = symbols.load(sym)
    rm = _rm()
    strat = VwapSnap(risk_pct=RISK, risk_manager=rm, spec=spec)
    res = Backtester(strat, rm, costs_for(spec), BALANCE, sym).run(df)
    m = metrics.compute(res.equity, res.trades, BALANCE, res.ruined)
    return pack(
        f"VwapSnap · {sym}", "VwapSnap",
        "Пик отклонения >1.85 ATR от дневного VWAP + разворот к магниту. "
        "1 сделка/день, TP=VWAP, окно NY 16–20.",
        sym, strat, df, res, m,
    )


def run_tide(sym: str, df: pd.DataFrame) -> dict:
    spec = symbols.load(sym)
    rm = _rm()
    strat = SlowTide(risk_pct=RISK, risk_manager=rm, spec=spec)
    res = Backtester(strat, rm, costs_for(spec), BALANCE, sym).run(df)
    m = metrics.compute(res.equity, res.trades, BALANCE, res.ruined)
    return pack(
        f"SlowTide · {sym}", "SlowTide",
        "Тренд EMA21/55 (разделение ≥0.45 ATR). Откат к медленной EMA, "
        "close за быстрой. TP 2R, ≤2 сделок/день, окно 15–20.",
        sym, strat, df, res, m,
    )


def run_coil(sym: str, df: pd.DataFrame) -> dict:
    spec = symbols.load(sym)
    rm = _rm()
    strat = CoilBreak(risk_pct=RISK, risk_manager=rm, spec=spec, rr=1.7)
    res = Backtester(strat, rm, costs_for(spec), BALANCE, sym).run(df)
    m = metrics.compute(res.equity, res.trades, BALANCE, res.ruined)
    return pack(
        f"CoilBreak · {sym}", "CoilBreak",
        "Азиатский койл 02–08 → первый пробой тела в Лондоне 11–15. "
        "Ширина койла фильтруется ATR. Одна сделка в день, TP 1.7R.",
        sym, strat, df, res, m,
    )


def main() -> None:
    con.print("[bold cyan]Оригинальные CFD-боты[/] · $1000 · риск 0.5% · Bybit fees · ~90д")
    cands: list[dict] = []

    for sym in ORB_SYMS:
        try:
            df = load_m5(sym)
        except FileNotFoundError:
            continue
        con.print(f"  OrbPulse {sym}…")
        cands.append(run_orb(sym, df))

    for sym in COIL_SYMS:
        try:
            df = load_m5(sym)
        except FileNotFoundError:
            continue
        con.print(f"  CoilBreak {sym}…")
        cands.append(run_coil(sym, df))

    for sym in VWAP_SYMS:
        try:
            df = load_m5(sym)
        except FileNotFoundError:
            continue
        con.print(f"  VwapSnap {sym}…")
        cands.append(run_vwap(sym, df))

    for sym in TIDE_SYMS:
        try:
            df = load_m5(sym)
        except FileNotFoundError:
            continue
        con.print(f"  SlowTide {sym}…")
        cands.append(run_tide(sym, df))

    ranked = sorted(cands, key=lambda c: c["score"], reverse=True)

    t = Table(title="Кандидаты (мои стратегии)")
    t.add_column("#", justify="right")
    t.add_column("бот")
    t.add_column("ret%", justify="right")
    t.add_column("DD%", justify="right")
    t.add_column("N", justify="right")
    t.add_column("WR%", justify="right")
    t.add_column("score", justify="right")
    for i, c in enumerate(ranked[:20], 1):
        m = c["metrics"]
        col = "green" if m["return_pct"] > 0 else "red"
        t.add_row(
            str(i), c["title"], f"[{col}]{m['return_pct']:+.2f}[/]",
            f"{m['max_drawdown_pct']:.1f}", str(m["trades"]),
            f"{m['win_rate']:.0f}", f"{c['score']:.2f}",
        )
    con.print(t)

    # Топ-3: сначала плюсовые, по возможности разные семейства.
    top: list[dict] = []
    used: set[str] = set()
    pos = [c for c in ranked if c["metrics"]["return_pct"] > 0 and c["score"] > 0]
    pool = pos if len(pos) >= 3 else ranked
    for c in pool:
        if len(top) >= 3:
            break
        if c["family"] in used:
            continue
        top.append(c)
        used.add(c["family"])
    for c in pool:
        if len(top) >= 3:
            break
        if c in top:
            continue
        top.append(c)

    con.print("\n[bold green]ТОП-3 МОИХ БОТОВ[/]")
    for i, c in enumerate(top, 1):
        m = c["metrics"]
        con.print(
            f"  {i}. {c['title']}: {m['return_pct']:+.2f}% · DD {m['max_drawdown_pct']:.1f}% · "
            f"N={m['trades']} WR={m['win_rate']:.0f}% · score={c['score']:.2f}"
        )
        con.print(f"     [dim]{c['idea']}[/]")

    def slim(c: dict, chart: bool) -> dict:
        o = {k: v for k, v in c.items()
             if k not in ("candles", "markers", "trades", "equity")}
        o["score"] = round(c["score"], 3)
        o["equity"] = c["equity"]
        if chart:
            o["candles"] = c["candles"]
            o["markers"] = c["markers"]
            o["trades"] = c["trades"]
        else:
            o["candles"], o["markers"], o["trades"] = [], [], c["trades"][:40]
        return o

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "Авторские боты: OrbPulse / CoilBreak / VwapSnap / SlowTide (не из вашей машины)",
        "venue": "Bybit TradFi MT5 CFD",
        "deposit": BALANCE,
        "risk_pct": RISK,
        "days": 90,
        "top": [slim(c, True) for c in top],
        "all": [{
            "title": c["title"], "family": c["family"], "symbol": c["symbol"],
            "score": round(c["score"], 3), "metrics": c["metrics"],
            "idea": c["idea"], "commission": c["commission"],
        } for c in ranked],
        "ideas": [
            {"name": "OrbPulse", "blurb": "NY opening range 30м → первый чистый пробой"},
            {"name": "CoilBreak", "blurb": "Азиатский койл → лондонский выход тела"},
            {"name": "VwapSnap", "blurb": "Разворот к дневному VWAP после ATR-пика"},
            {"name": "SlowTide", "blurb": "EMA21/55 tide + откат к медленной EMA"},
        ],
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "original_cfd_top3.json").write_text(_json_dump(payload), encoding="utf-8")
    path = OUT / "original_cfd_top3.html"
    path.write_text(HTML.replace("__PAYLOAD__", _json_dump(payload)), encoding="utf-8")
    con.print(f"\n[green]Отчёт → {path}[/]")


HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Мои CFD-боты · Топ-3</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{
  --bg:#0a0c10; --panel:#12171f; --line:#243041; --text:#eef2f7; --muted:#8b97a8;
  --green:#3dd6c3; --red:#ff7b78; --gold:#e0b84e; --violet:#9b7bff;
  --mono:ui-monospace,"SF Mono",Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--sans)}
header{padding:22px 24px 16px;border-bottom:1px solid var(--line);
  background:radial-gradient(1200px 400px at 10% -20%,rgba(155,123,255,.18),transparent),
             linear-gradient(180deg,#141a24,#0a0c10)}
header h1{margin:0;font:750 24px/1.15 var(--sans);letter-spacing:-.03em}
header .sub{color:var(--muted);margin:8px 0 0;font-size:13px;max-width:70ch}
.ideas{display:flex;gap:10px;flex-wrap:wrap;padding:14px 22px;border-bottom:1px solid var(--line)}
.ideas span{background:#1a2230;border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-size:12px;color:var(--muted)}
.ideas b{color:var(--violet);font-weight:700}
.podium{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;padding:18px 22px}
@media(max-width:960px){.podium{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px;cursor:pointer}
.card:hover,.card.active{border-color:var(--gold);box-shadow:0 0 0 1px rgba(224,184,78,.25)}
.card .rank{font:700 11px var(--sans);color:var(--gold);letter-spacing:.1em}
.card h2{margin:8px 0 6px;font:650 17px/1.25 var(--sans)}
.card .idea{color:var(--muted);font-size:12px;min-height:3.4em}
.kpis{display:flex;flex-wrap:wrap;gap:12px;margin-top:12px}
.kpis div span{display:block;font-size:11px;color:var(--muted)}
.kpis div b{font:650 16px var(--mono)}
.pos{color:var(--green)}.neg{color:var(--red)}
.toolbar{padding:10px 22px;border:solid var(--line);border-width:1px 0;background:var(--panel);display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.toolbar strong{font-size:15px}.toolbar span{color:var(--muted);font-size:12px}
.grid2{display:grid;grid-template-columns:1.1fr .9fr;border-bottom:1px solid var(--line)}
@media(max-width:960px){.grid2{grid-template-columns:1fr}}
.panel{padding:12px 18px;border-right:1px solid var(--line)}
.panel h3{margin:0 0 8px;font:600 11px var(--sans);color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
#eqChart{height:260px;background:#07090c}#pxChart{height:420px;background:#07090c}
table{width:100%;border-collapse:collapse;font:12px/1.35 var(--mono)}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font:600 11px var(--sans);position:sticky;top:0;background:#151b24}
.wrap{max-height:300px;overflow:auto}
.note{padding:12px 22px;color:var(--muted);font-size:12px}
</style>
</head>
<body>
<header>
  <h1>Мои CFD-боты · Топ-3</h1>
  <p class="sub" id="sub">Три авторские идеи с нуля — без HSS, London S/R и мартингейла. $1000 · риск 0.5% · комиссии Bybit.</p>
</header>
<div class="ideas" id="ideas"></div>
<section class="podium" id="podium"></section>
<div class="toolbar"><strong id="selTitle">—</strong><span id="selMeta"></span></div>
<div class="grid2">
  <div class="panel"><h3>Эквити</h3><div id="eqChart"></div></div>
  <div class="panel"><h3>Все прогоны</h3>
    <div class="wrap"><table>
      <thead><tr><th>бот</th><th>ret%</th><th>DD%</th><th>N</th><th>WR</th><th>score</th></tr></thead>
      <tbody id="allBody"></tbody>
    </table></div>
  </div>
</div>
<div class="panel" style="border-bottom:1px solid var(--line)"><h3>График</h3><div id="pxChart"></div></div>
<div class="wrap" style="padding:0 18px 16px">
  <table>
    <thead><tr><th>#</th><th>side</th><th>лот</th><th>вход</th><th>выход</th><th>pnl</th><th></th><th>время</th></tr></thead>
    <tbody id="trades"></tbody>
  </table>
</div>
<p class="note">Score = Calmar × штраф DD&gt;12% × бонус за число сделок × винрейт. Пессимистичный бэктест: спред из истории + комиссия на открытии.</p>
<script type="application/json" id="data">__PAYLOAD__</script>
<script>
const D=JSON.parse(document.getElementById("data").textContent);
const money=v=>(v<0?"−":"+")+"$"+Math.abs(v).toFixed(2);
const pct=v=>(v>=0?"+":"")+v.toFixed(2)+"%";
let sel=0,eqC,pxC;
document.getElementById("sub").textContent=
  `${D.venue} · депозит $${D.deposit} · риск ${D.risk_pct}% · ~${D.days}д · ${D.note}`;
document.getElementById("ideas").innerHTML=(D.ideas||[]).map(x=>
  `<span><b>${x.name}</b> — ${x.blurb}</span>`).join("");
document.getElementById("podium").innerHTML=D.top.map((c,i)=>{
  const m=c.metrics, cls=m.return_pct>=0?"pos":"neg";
  return `<div class="card ${i===0?"active":""}" onclick="select(${i})">
    <div class="rank">#${i+1} · ${c.family}</div>
    <h2>${c.title}</h2>
    <div class="idea">${c.idea||""}</div>
    <div class="kpis">
      <div><span>P&L</span><b class="${cls}">${pct(m.return_pct)}</b></div>
      <div><span>Max DD</span><b class="neg">${m.max_drawdown_pct.toFixed(1)}%</b></div>
      <div><span>Сделок</span><b>${m.trades}</b></div>
      <div><span>WR</span><b>${m.win_rate.toFixed(0)}%</b></div>
      <div><span>Score</span><b>${c.score}</b></div>
    </div></div>`;
}).join("");
document.getElementById("allBody").innerHTML=D.all.map(c=>{
  const m=c.metrics, cls=m.return_pct>=0?"pos":"neg";
  return `<tr><td>${c.title}</td><td class="${cls}">${pct(m.return_pct)}</td>
    <td>${m.max_drawdown_pct.toFixed(1)}</td><td>${m.trades}</td>
    <td>${m.win_rate.toFixed(0)}%</td><td>${c.score}</td></tr>`;
}).join("");
function mk(el){return LightweightCharts.createChart(el,{
  layout:{background:{color:"#07090c"},textColor:"#8b97a8"},
  grid:{vertLines:{color:"#1a2230"},horzLines:{color:"#1a2230"}},
  rightPriceScale:{borderColor:"#243041"},
  timeScale:{borderColor:"#243041",timeVisible:true}});}
function parseT(s){const raw=String(s||"").trim().replace(" ","T");if(!raw)return 0;
  const z=/Z$|[+-]\d{2}:?\d{2}$/.test(raw)?raw:raw+"Z";const ms=Date.parse(z);return ms?Math.floor(ms/1000):0;}
function select(i){sel=i;[...document.querySelectorAll(".card")].forEach((e,j)=>e.classList.toggle("active",j===i));paint();}
function paint(){
  const c=D.top[sel], m=c.metrics;
  document.getElementById("selTitle").textContent=c.title;
  document.getElementById("selMeta").textContent=
    `${pct(m.return_pct)} · DD ${m.max_drawdown_pct.toFixed(1)}% · N=${m.trades} · WR ${m.win_rate.toFixed(0)}% · fee $${c.commission||0}`;
  const eqEl=document.getElementById("eqChart"); if(eqC)eqC.remove(); eqC=mk(eqEl);
  const a=eqC.addAreaSeries({lineColor:m.return_pct>=0?"#3dd6c3":"#ff7b78",
    topColor:m.return_pct>=0?"rgba(61,214,195,.25)":"rgba(255,123,120,.25)",
    bottomColor:"rgba(0,0,0,0)",lineWidth:2});
  a.setData((c.equity||[]).map(p=>({time:parseT(p.t),value:p.v})).filter(x=>x.time));
  eqC.timeScale().fitContent();
  new ResizeObserver(()=>eqC.applyOptions({width:eqEl.clientWidth,height:eqEl.clientHeight})).observe(eqEl);
  const pxEl=document.getElementById("pxChart"); if(pxC)pxC.remove(); pxC=mk(pxEl);
  const cs=pxC.addCandlestickSeries({upColor:"#26a69a",downColor:"#ef5350",borderVisible:false,
    wickUpColor:"#26a69a",wickDownColor:"#ef5350"});
  const candles=c.candles||[];
  if(candles.length){
    cs.setData(candles.map(x=>({time:x.time,open:x.open,high:x.high,low:x.low,close:x.close})));
    cs.setMarkers((c.markers||[]).map(mk=>({time:mk.time,position:mk.position,color:mk.color,
      shape:mk.kind==="entry"?(mk.side==="buy"?"arrowUp":"arrowDown"):"circle",text:mk.label||""})));
  }
  pxC.timeScale().fitContent();
  new ResizeObserver(()=>pxC.applyOptions({width:pxEl.clientWidth,height:pxEl.clientHeight})).observe(pxEl);
  document.getElementById("trades").innerHTML=(c.trades||[]).slice(0,200).map((t,i)=>
    `<tr><td>${i+1}</td><td class="${t.side==="buy"?"pos":"neg"}">${t.side}</td>
     <td>${t.volume}</td><td>${(+t.entry).toFixed(5)}</td><td>${(+t.exit).toFixed(5)}</td>
     <td class="${t.pnl>=0?"pos":"neg"}">${money(t.pnl)}</td><td>${t.label}</td>
     <td>${(t.opened_at||"").slice(5,16)}</td></tr>`).join("");
}
select(0);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
