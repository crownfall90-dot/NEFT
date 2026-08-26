"""Профили CFD-ботов под разные цели.

  NanoFade  — много сделок, минимальная просадка
  ApexShot  — мало сделок, минимум лузов, качество
  Цель: суммарно ~5–15% в месяц при $1000 и комиссиях Bybit

    python scripts/cfd_profile_bots.py
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
from neft.strategies.apex_shot import ApexShot
from neft.strategies.orb_pulse import OrbPulse
from neft.strategies.pulse_clip import PulseClip
from neft.strategies.vwap_snap import VwapSnap

con = Console()
OUT = ROOT / "dashboard"
BALANCE = 1000.0

SYMS = [
    "NAS100", "DJ30", "GER40", "FRA40", "XAUUSD+", "UKOUSD",
    "GBPUSD+", "USDJPY+", "USDCAD+", "USDCHF+", "EURJPY+", "EURUSD+",
]


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
                "time": _ts(t.opened_at), "kind": "entry", "side": side,
                "position": "belowBar" if buy else "aboveBar",
                "color": "#3dd6c3" if buy else "#ff7b78",
            })
        if t.closed_at is not None:
            tp = (t.reason or "").startswith("tp")
            out.append({
                "time": _ts(t.closed_at), "kind": "exit",
                "label": "TP" if tp else "SL",
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


def monthly_returns(eq: pd.Series) -> list[dict]:
    if eq.empty:
        return []
    s = eq.copy()
    s.index = pd.to_datetime(s.index)
    by = s.resample("ME").last().dropna()
    if len(by) >= 2:
        prev = float(s.iloc[0])
        out = []
        for ts, v in by.items():
            ret = (float(v) / prev - 1) * 100
            out.append({
                "month": str(pd.Timestamp(ts))[:7],
                "return_pct": round(ret, 2),
                "end": round(float(v), 2),
            })
            prev = float(v)
        return out
    # Мало календарных месяцев — 3 равных куска периода.
    n = len(s)
    chunk = max(1, n // 3)
    out = []
    for k in range(3):
        a = float(s.iloc[k * chunk])
        b = float(s.iloc[min(n - 1, (k + 1) * chunk - 1)])
        if a <= 0:
            continue
        out.append({
            "month": f"P{k+1}",
            "return_pct": round((b / a - 1) * 100, 2),
            "end": round(b, 2),
        })
    return out


def avg_month_pct(m: dict, days: int, monthly: list[dict]) -> float:
    if monthly:
        return sum(x["return_pct"] for x in monthly) / len(monthly)
    return float(m["return_pct"]) / max(days / 30.0, 1.0)


def score_profile(m: dict, *, profile: str, avg_mo: float) -> float:
    if m.get("ruined") or m["trades"] < 5:
        return -999.0
    ret = float(m["return_pct"])
    dd = max(float(m["max_drawdown_pct"]), 0.2)
    wr = float(m["win_rate"])
    n = int(m["trades"])
    # попадание в коридор 5–15% / мес
    if 5 <= avg_mo <= 15:
        band = 1.4
    elif 3 <= avg_mo < 5 or 15 < avg_mo <= 20:
        band = 1.0
    elif avg_mo > 0:
        band = 0.55
    else:
        band = 0.15

    if profile == "hf_low_dd":
        # много сделок + низкий DD
        return (ret / dd) * (min(n, 120) / 40) * (wr / 50) * band * (12 / max(dd, 1))
    if profile == "quality":
        # WR и мало лузов важнее частоты
        losses = max(1, int(m["losses"]))
        return (ret / dd) * (wr / 40) * (1.0 / losses) * n**0.3 * band * 8
    # balanced monthly
    return (ret / dd) * band * (wr / 45) * min(1.2, n / 20)


def _rm(risk: float) -> RiskManager:
    return RiskManager(
        start_balance=BALANCE,
        limits=RiskLimits(
            risk_per_trade_pct=risk, max_risk_per_trade_pct=max(3.0, risk * 3),
            min_risk_per_trade_pct=0.0, max_volume=100.0,
            max_daily_loss_pct=100.0, max_drawdown_pct=100.0,
            min_free_margin_pct=0.0,
        ),
    )


def run_one(sym: str, df: pd.DataFrame, *, family: str, risk: float, make) -> dict:
    spec = symbols.load(sym)
    fee = commission_per_lot(sym)
    rm = _rm(risk)
    strat = make(rm, spec, fee)
    res = Backtester(strat, rm, costs_for(spec), BALANCE, sym).run(df)
    m = metrics.compute(res.equity, res.trades, BALANCE, res.ruined)
    days = max(1, (df.time.iloc[-1] - df.time.iloc[0]).days)
    monthly = monthly_returns(res.equity)
    avg_mo = avg_month_pct(m.as_dict(), days, monthly)
    md = m.as_dict()
    return {
        "title": f"{family} · {sym}",
        "family": family,
        "symbol": sym,
        "risk_pct": risk,
        "metrics": md,
        "days": days,
        "monthly": monthly,
        "avg_month_pct": round(avg_mo, 2),
        "in_band": 5 <= avg_mo <= 15,
        "equity": _equity(res.equity),
        "markers": _markers(res.trades),
        "trades": _trades(res.trades),
        "candles": _candles(df),
        "commission": round(sum(fee * t.volume for t in res.trades), 2),
        "setups": int(getattr(strat, "setups_seen", 0) or 0),
    }


def main() -> None:
    con.print("[bold cyan]Профили CFD[/] · $1000 · цель 5–15%/мес · Bybit fees")
    cands: list[dict] = []

    for sym in SYMS:
        try:
            df = load_m5(sym)
        except FileNotFoundError:
            continue

        con.print(f"  PulseClip {sym}…")
        cands.append(run_one(
            sym, df, family="PulseClip", risk=0.45,
            make=lambda rm, spec, fee: PulseClip(
                risk_pct=0.45, risk_manager=rm, spec=spec,
                commission_per_lot=fee, max_per_day=6,
            ),
        ))

        con.print(f"  ApexShot {sym}…")
        cands.append(run_one(
            sym, df, family="ApexShot", risk=1.0,
            make=lambda rm, spec, fee: ApexShot(
                risk_pct=1.0, risk_manager=rm, spec=spec,
                commission_per_lot=fee, rr=2.0, max_per_day=1,
            ),
        ))

        if sym in ("USDJPY+", "XAUUSD+", "GER40", "UKOUSD", "GBPUSD+", "DJ30"):
            con.print(f"  VwapSnap {sym}…")
            cands.append(run_one(
                sym, df, family="VwapSnap", risk=1.0,
                make=lambda rm, spec, fee: VwapSnap(
                    risk_pct=1.0, risk_manager=rm, spec=spec, max_per_day=1,
                ),
            ))
            con.print(f"  OrbPulse {sym}…")
            cands.append(run_one(
                sym, df, family="OrbPulse", risk=1.0,
                make=lambda rm, spec, fee: OrbPulse(
                    risk_pct=1.0, risk_manager=rm, spec=spec, rr=1.8,
                ),
            ))

    for c in cands:
        fam = c["family"]
        if fam == "PulseClip":
            c["profile"] = "hf_low_dd"
            c["profile_title"] = "Много сделок · мин. DD"
        elif fam == "ApexShot":
            c["profile"] = "quality"
            c["profile_title"] = "Мало сделок · мин. лузов"
        else:
            c["profile"] = "monthly"
            c["profile_title"] = "Коридор 5–15%/мес"
        c["score"] = score_profile(
            c["metrics"], profile=c["profile"], avg_mo=c["avg_month_pct"],
        )

    def best(pred):
        pool = [c for c in cands if pred(c) and c["score"] > -100]
        return max(pool, key=lambda x: x["score"]) if pool else None

    pick_hf = best(lambda c: c["family"] == "PulseClip"
                   and c["metrics"]["return_pct"] > 0
                   and c["metrics"]["trades"] >= 30)
    if pick_hf is None:
        pick_hf = best(lambda c: c["family"] == "PulseClip"
                       and c["metrics"]["return_pct"] > 0)
    # Качество: максимум WR при минимуме лузов (не обязательно ApexShot).
    pick_q = best(lambda c: c["metrics"]["trades"] >= 5
                  and c["metrics"]["win_rate"] >= 50
                  and c["metrics"]["return_pct"] > 0
                  and c["metrics"]["losses"] <= 10
                  and c["metrics"]["max_drawdown_pct"] <= 10)
    if pick_q is None:
        pick_q = best(lambda c: c["family"] == "ApexShot"
                      and c["metrics"]["trades"] >= 5
                      and c["metrics"]["return_pct"] > 0)
    used0 = {id(pick_hf), id(pick_q)}
    pick_band = best(lambda c: c["in_band"] and c["metrics"]["return_pct"] > 0
                     and id(c) not in used0)
    if pick_band is None:
        pick_band = best(lambda c: 4 <= c["avg_month_pct"] <= 18
                         and c["metrics"]["return_pct"] > 0
                         and id(c) not in used0)

    top = []
    for p, label in (
        (pick_hf, "Много сделок · мин. DD"),
        (pick_q, "Мало сделок · мин. лузов"),
        (pick_band, "Коридор ~5–15%/мес"),
    ):
        if p is None:
            continue
        p = dict(p)
        p["profile_title"] = label
        if p not in top:
            top.append(p)

    # Таблица
    ranked = sorted(cands, key=lambda c: c["score"], reverse=True)
    t = Table(title="Профили (фрагмент)")
    t.add_column("#", justify="right")
    t.add_column("бот")
    t.add_column("ret%", justify="right")
    t.add_column("%/мес", justify="right")
    t.add_column("DD%", justify="right")
    t.add_column("N", justify="right")
    t.add_column("WR%", justify="right")
    t.add_column("L", justify="right")
    t.add_column("band", justify="center")
    for i, c in enumerate(ranked[:22], 1):
        m = c["metrics"]
        col = "green" if m["return_pct"] > 0 else "red"
        t.add_row(
            str(i), c["title"],
            f"[{col}]{m['return_pct']:+.2f}[/]",
            f"{c['avg_month_pct']:+.1f}",
            f"{m['max_drawdown_pct']:.1f}",
            str(m["trades"]), f"{m['win_rate']:.0f}",
            str(m["losses"]),
            "✓" if c["in_band"] else "",
        )
    con.print(t)

    con.print("\n[bold green]ВЫБОР ПО ПРОФИЛЯМ[/]")
    labels = [
        "① Часто + низкий DD",
        "② Качество / мало лузов",
        "③ Коридор ~5–15%/мес",
    ]
    for lab, c in zip(labels, top):
        m = c["metrics"]
        con.print(
            f"  {lab}: [cyan]{c['title']}[/] · {m['return_pct']:+.1f}% за {c['days']}д "
            f"(~{c['avg_month_pct']:+.1f}%/мес) · DD {m['max_drawdown_pct']:.1f}% · "
            f"N={m['trades']} WR={m['win_rate']:.0f}% L={m['losses']}"
        )
        if c.get("monthly"):
            months = ", ".join(f"{x['month']} {x['return_pct']:+.1f}%" for x in c["monthly"])
            con.print(f"     месяцы: {months}")

    def slim(c: dict) -> dict:
        return {
            "title": c["title"], "family": c["family"], "symbol": c["symbol"],
            "profile": c["profile"], "profile_title": c["profile_title"],
            "risk_pct": c["risk_pct"], "score": round(c["score"], 3),
            "metrics": c["metrics"], "days": c["days"],
            "monthly": c["monthly"], "avg_month_pct": c["avg_month_pct"],
            "in_band": c["in_band"], "commission": c["commission"],
            "equity": c["equity"], "candles": c["candles"],
            "markers": c["markers"], "trades": c["trades"],
        }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deposit": BALANCE,
        "target": "5–15% в месяц суммарно",
        "venue": "Bybit TradFi MT5 CFD",
        "profiles": [
            {"key": "hf_low_dd", "title": "Много сделок · мин. просадка",
             "bot": "PulseClip", "blurb": "Клипы по тренду EMA34: откат → продолжение, TP быстрый, стоп дня по лузам"},
            {"key": "quality", "title": "Мало сделок · мин. лузов",
             "bot": "ApexShot", "blurb": "A+ фильтр: тренд+ATR не в пике+поглощение у EMA55, RR≈2, пауза после луза"},
            {"key": "monthly", "title": "Коридор 5–15%/мес",
             "bot": "лучший в band", "blurb": "Отбор по среднему %/мес в коридоре 5–15 при живом DD"},
        ],
        "top": [slim(c) for c in top],
        "all": [{
            "title": c["title"], "family": c["family"], "symbol": c["symbol"],
            "profile": c["profile"], "avg_month_pct": c["avg_month_pct"],
            "in_band": c["in_band"], "score": round(c["score"], 3),
            "metrics": c["metrics"], "risk_pct": c["risk_pct"],
            "commission": c["commission"], "monthly": c["monthly"],
        } for c in ranked],
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "cfd_profiles.json").write_text(_json_dump(payload), encoding="utf-8")
    path = OUT / "cfd_profiles.html"
    path.write_text(HTML.replace("__PAYLOAD__", _json_dump(payload)), encoding="utf-8")
    con.print(f"\n[green]Отчёт → {path}[/]")


HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CFD · профили ботов · 5–15%/мес</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{
  --bg:#090b10; --panel:#131820; --line:#243041; --text:#eef2f7; --muted:#8b97a8;
  --green:#3dd6c3; --red:#ff7b78; --gold:#e0b84e; --blue:#6aa8ff;
  --mono:ui-monospace,"SF Mono",Consolas,monospace; --sans:system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--sans)}
header{padding:22px 24px 14px;border-bottom:1px solid var(--line);
  background:radial-gradient(900px 320px at 80% -30%,rgba(106,168,255,.16),transparent),linear-gradient(180deg,#151b26,#090b10)}
header h1{margin:0;font:750 22px/1.2 var(--sans);letter-spacing:-.03em}
header .sub{color:var(--muted);margin:8px 0 0;font-size:13px;max-width:75ch}
.profiles{display:flex;gap:10px;flex-wrap:wrap;padding:12px 22px;border-bottom:1px solid var(--line)}
.profiles span{background:#1a2230;border:1px solid var(--line);border-radius:10px;padding:8px 12px;font-size:12px;color:var(--muted);max-width:340px}
.profiles b{color:var(--blue);display:block;margin-bottom:2px}
.podium{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;padding:18px 22px}
@media(max-width:980px){.podium{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;cursor:pointer}
.card.active,.card:hover{border-color:var(--gold)}
.card .tag{font:700 11px var(--sans);color:var(--gold);letter-spacing:.08em}
.card h2{margin:8px 0 4px;font:650 16px var(--sans)}
.card .meta{color:var(--muted);font-size:12px;min-height:2.8em}
.kpis{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
.kpis div span{display:block;font-size:11px;color:var(--muted)}
.kpis div b{font:650 15px var(--mono)}
.pos{color:var(--green)}.neg{color:var(--red)}
.band{display:inline-block;padding:2px 8px;border-radius:999px;background:rgba(61,214,195,.12);color:var(--green);font-size:11px;font-weight:700}
.toolbar{padding:10px 22px;border:solid var(--line);border-width:1px 0;background:var(--panel);display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.toolbar strong{font-size:15px}.toolbar span{color:var(--muted);font-size:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--line)}
@media(max-width:980px){.grid2{grid-template-columns:1fr}}
.panel{padding:12px 18px;border-right:1px solid var(--line)}
.panel h3{margin:0 0 8px;font:600 11px var(--sans);color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
#eqChart{height:240px;background:#07090c}#pxChart{height:400px;background:#07090c}
table{width:100%;border-collapse:collapse;font:12px/1.35 var(--mono)}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font:600 11px var(--sans);position:sticky;top:0;background:#151b24}
.wrap{max-height:320px;overflow:auto}
.note{padding:12px 22px;color:var(--muted);font-size:12px}
.months{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.months i{font-style:normal;background:#1a2230;border:1px solid var(--line);border-radius:8px;padding:4px 8px;font:12px var(--mono)}
</style>
</head>
<body>
<header>
  <h1>CFD · профили ботов</h1>
  <p class="sub" id="sub">Частые сделки / качество / коридор 5–15% в месяц. $1000 · комиссии Bybit.</p>
</header>
<div class="profiles" id="profiles"></div>
<section class="podium" id="podium"></section>
<div class="toolbar"><strong id="selTitle">—</strong><span id="selMeta"></span></div>
<div class="grid2">
  <div class="panel"><h3>Эквити</h3><div id="eqChart"></div>
    <div class="months" id="months"></div>
  </div>
  <div class="panel"><h3>Все прогоны</h3>
    <div class="wrap"><table>
      <thead><tr><th>бот</th><th>ret%</th><th>%/м</th><th>DD</th><th>N</th><th>WR</th><th>L</th><th></th></tr></thead>
      <tbody id="allBody"></tbody>
    </table></div>
  </div>
</div>
<div class="panel" style="border-bottom:1px solid var(--line)"><h3>График</h3><div id="pxChart"></div></div>
<div class="wrap" style="padding:0 18px 16px">
  <table><thead><tr><th>#</th><th>side</th><th>лот</th><th>вход</th><th>выход</th><th>pnl</th><th></th><th>время</th></tr></thead>
  <tbody id="trades"></tbody></table>
</div>
<p class="note">PulseClip: продолжение тренда после отката к EMA (частые клипы). ApexShot: редкие A+ входы. %/мес — средний по календарным месяцам эквити.</p>
<script type="application/json" id="data">__PAYLOAD__</script>
<script>
const D=JSON.parse(document.getElementById("data").textContent);
const money=v=>(v<0?"−":"+")+"$"+Math.abs(v).toFixed(2);
const pct=v=>(v>=0?"+":"")+Number(v).toFixed(2)+"%";
let sel=0,eqC,pxC;
document.getElementById("sub").textContent=
  `${D.venue} · депозит $${D.deposit} · цель: ${D.target}`;
document.getElementById("profiles").innerHTML=(D.profiles||[]).map(p=>
  `<span><b>${p.title}</b>${p.bot}: ${p.blurb}</span>`).join("");
const tags=["① Часто + низкий DD","② Качество / мало лузов","③ ~5–15%/мес"];
document.getElementById("podium").innerHTML=D.top.map((c,i)=>{
  const m=c.metrics, cls=m.return_pct>=0?"pos":"neg";
  return `<div class="card ${i===0?"active":""}" onclick="select(${i})">
    <div class="tag">${tags[i]||c.profile_title}</div>
    <h2>${c.title}</h2>
    <div class="meta">${c.profile_title||""} · риск ${c.risk_pct}% ${c.in_band?'<span class="band">в коридоре</span>':''}</div>
    <div class="kpis">
      <div><span>P&L</span><b class="${cls}">${pct(m.return_pct)}</b></div>
      <div><span>~%/мес</span><b class="${cls}">${pct(c.avg_month_pct)}</b></div>
      <div><span>Max DD</span><b class="neg">${m.max_drawdown_pct.toFixed(1)}%</b></div>
      <div><span>Сделок</span><b>${m.trades}</b></div>
      <div><span>WR / L</span><b>${m.win_rate.toFixed(0)}% / ${m.losses}</b></div>
    </div></div>`;
}).join("");
document.getElementById("allBody").innerHTML=D.all.slice(0,40).map(c=>{
  const m=c.metrics, cls=m.return_pct>=0?"pos":"neg";
  return `<tr><td>${c.title}</td><td class="${cls}">${pct(m.return_pct)}</td>
    <td class="${cls}">${pct(c.avg_month_pct)}</td><td>${m.max_drawdown_pct.toFixed(1)}</td>
    <td>${m.trades}</td><td>${m.win_rate.toFixed(0)}</td><td>${m.losses}</td>
    <td>${c.in_band?"✓":""}</td></tr>`;
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
    `${pct(m.return_pct)} за ${c.days}д · ~${pct(c.avg_month_pct)}/мес · DD ${m.max_drawdown_pct.toFixed(1)}% · N=${m.trades} WR ${m.win_rate.toFixed(0)}% L=${m.losses}`;
  document.getElementById("months").innerHTML=(c.monthly||[]).map(x=>
    `<i class="${x.return_pct>=0?"pos":"neg"}">${x.month}: ${pct(x.return_pct)}</i>`).join("");
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
  if((c.candles||[]).length){
    cs.setData(c.candles.map(x=>({time:x.time,open:x.open,high:x.high,low:x.low,close:x.close})));
    cs.setMarkers((c.markers||[]).map(mk=>({time:mk.time,position:mk.position,color:mk.color,
      shape:mk.kind==="entry"?(mk.side==="buy"?"arrowUp":"arrowDown"):"circle",text:mk.label||""})));
  }
  pxC.timeScale().fitContent();
  new ResizeObserver(()=>pxC.applyOptions({width:pxEl.clientWidth,height:pxEl.clientHeight})).observe(pxEl);
  document.getElementById("trades").innerHTML=(c.trades||[]).slice(0,250).map((t,i)=>
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
