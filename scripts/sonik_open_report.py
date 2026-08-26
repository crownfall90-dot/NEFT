"""Отчёт: реконструкция #SONIK на XAUUSD+ (Bybit CFD).

Анализ истории Tag Markets + бэктест аналога SonikOpen.

    python scripts/sonik_open_report.py
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
from neft.strategies.sonik_open import SonikOpen

con = Console()
OUT = ROOT / "dashboard"
SYMBOL = "XAUUSD+"
BALANCE = 1000.0
RISK = 0.3


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


def _candles(df):
    return [{
        "time": _ts(r.time), "open": float(r.open), "high": float(r.high),
        "low": float(r.low), "close": float(r.close),
    } for r in df.itertuples(index=False)]


def _equity(eq, points=900):
    step = max(1, len(eq) // points)
    return [{"t": str(i), "v": round(float(v), 2)} for i, v in eq.iloc[::step].items()]


def _markers(trades):
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


def _trades(trades):
    rows = []
    for t in trades:
        side = t.side.value if hasattr(t.side, "value") else str(t.side)
        rows.append({
            "side": side, "volume": float(t.volume),
            "entry": float(t.entry), "exit": float(t.exit),
            "pnl": round(float(t.pnl), 4),
            "opened_at": str(t.opened_at) if t.opened_at else "",
            "closed_at": str(t.closed_at) if t.closed_at else "",
            "sl": float(t.sl) if getattr(t, "sl", None) is not None else None,
            "tp": float(t.tp) if getattr(t, "tp", None) is not None else None,
            "label": "TP" if (t.reason or "").startswith("tp") else "SL",
            "bars": int(t.bars_held),
        })
    return rows


def monthly(eq: pd.Series):
    s = eq.copy()
    s.index = pd.to_datetime(s.index)
    by = s.resample("ME").last().dropna()
    if len(by) < 2:
        return []
    prev = float(s.iloc[0])
    out = []
    for ts, v in by.items():
        out.append({
            "month": str(pd.Timestamp(ts))[:7],
            "return_pct": round((float(v) / prev - 1) * 100, 2),
        })
        prev = float(v)
    return out


def _live_log_stats() -> dict:
    path = ROOT / "data" / "sonik_trades.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    df["open"] = pd.to_datetime(df["open"])
    df["close"] = pd.to_datetime(df["close"])
    df["hold"] = (df["close"] - df["open"]).dt.total_seconds() / 60.0
    df["pts"] = df["pnl"] / (df["lot"] * 100.0)
    df["win"] = df["pnl"] > 0
    wins, losses = df[df.win], df[~df.win]
    eq = 1000.0 + df.sort_values("close")["pnl"].cumsum()
    peak = eq.cummax()
    dd = ((peak - eq) / peak * 100).max()
    days = max(1, (df["open"].max() - df["open"].min()).days)
    return {
        "n": int(len(df)),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "wr": round(float(wins.shape[0] / len(df) * 100), 2),
        "pnl": round(float(df["pnl"].sum()), 2),
        "roi_pct": round(float(df["pnl"].sum()) / 10.0, 2),
        "max_dd_pct": round(float(dd), 2),
        "hold_med": round(float(df["hold"].median()), 1),
        "win_pts_med": round(float(wins["pts"].median()), 2),
        "loss_pts_med": round(float(losses["pts"].median()), 2),
        "avg_win_usd": round(float(wins["pnl"].mean()), 2),
        "avg_loss_usd": round(float(losses["pnl"].mean()), 2),
        "trades_per_day_med": float(df.groupby(df["open"].dt.date).size().median()),
        "months": round(days / 30.4, 1),
        "london_share_pct": round(
            float(((df["open"].dt.hour >= 6) & (df["open"].dt.hour < 10)).mean() * 100), 0
        ),
    }


def main():
    con.print("[bold cyan]SonikPulse[/] · реконструкция #SONIK на XAUUSD+")
    live = _live_log_stats()

    observed = {
        "broker": "Tag Markets / Amplify",
        "symbol_live": "XAUUSD.f",
        "window": "весь день 06–20; пик 06–10 (≈38%)",
        "holds_min": "med ~7.5м (лузы ~3м, винсы ~8м)",
        "trades_per_day": "med 2",
        "style": "одиночный ордер 0.01, короткий скальп, не мартингейл",
        "tp_points_example": "win med ~3.8 pts · loss med ~2.4 pts",
        "fee_line": "PF Deduction ≈ 30% (на чужой площадке)",
        "live_log": live,
        "amplify_card": {
            "roi": "+136.60%",
            "dd": "0.26%",
            "wr": "86.92%",
            "wins_losses": "359 / 54",
            "match": "лог 413 сделок совпадает с WR/ROI карточки Amplify",
        },
        "inferred_logic": [
            "Микро-тренд + откат (не только London open)",
            f"Фикс. SL≈{abs(live.get('loss_pts_med', 2.4)):.1f} pts, TP≈{live.get('win_pts_med', 3.8):.1f} pts",
            "Лузы режутся быстрее винсов (time-cut / tight SL)",
            "≤2 сделки/день, почти всегда 0.01 лот",
            "Buy≈Sell, max ~2 луза подряд",
        ],
    }

    df_m1 = pd.read_csv(ROOT / "data" / "XAUUSDplus_M1_90d.csv", parse_dates=["time"])
    df = data.resample_ohlc(df_m1, "5min")
    spec = symbols.load(SYMBOL)
    fee = commission_per_lot(SYMBOL)
    rm = RiskManager(
        start_balance=BALANCE,
        limits=RiskLimits(
            risk_per_trade_pct=RISK, max_risk_per_trade_pct=3.0,
            min_risk_per_trade_pct=0.0, max_volume=100.0,
            max_daily_loss_pct=100.0, max_drawdown_pct=100.0,
            min_free_margin_pct=0.0,
        ),
    )
    strat = SonikOpen(
        risk_pct=RISK, risk_manager=rm, spec=spec,
        commission_per_lot=fee,
        sl_points=2.6, tp_points=4.2, max_trades_day=2,
    )
    res = Backtester(strat, rm, costs_for(spec), BALANCE, SYMBOL).run(df)
    m = metrics.compute(res.equity, res.trades, BALANCE, res.ruined)
    days = max(1, (df.time.iloc[-1] - df.time.iloc[0]).days)
    mo = monthly(res.equity)
    avg_mo = (sum(x["return_pct"] for x in mo) / len(mo)) if mo else m.return_pct / (days / 30)

    holds = [t.bars_held * 5 for t in res.trades]
    t = Table(title="SonikPulse · XAUUSD+ · Bybit fees · $1000 · риск 0.3%")
    t.add_column("метрика"); t.add_column("значение", justify="right")
    color = "green" if m.net_profit > 0 else "red"
    t.add_row("Период", f"{df.time.iloc[0]} — {df.time.iloc[-1]}")
    t.add_row("Итог", f"[{color}]${m.end_balance:,.2f} ({m.return_pct:+.2f}%)[/]")
    t.add_row("~%/мес", f"{avg_mo:+.2f}")
    t.add_row("Сделок", str(m.trades))
    t.add_row("WR / лузы", f"{m.win_rate:.1f}% / {m.losses}")
    t.add_row("Max DD", f"{m.max_drawdown_pct:.2f}%")
    t.add_row("PF", f"{m.profit_factor:.2f}")
    t.add_row("Комиссии", f"${sum(fee * x.volume for x in res.trades):.2f}")
    if holds:
        t.add_row("Удержание (мед)", f"{sorted(holds)[len(holds)//2]} мин")
    con.print(t)
    if live:
        con.print(
            f"[dim]Live Amplify-лог: {live['n']} сделок · WR {live['wr']}% · "
            f"ROI +{live['roi_pct']}% · DD {live['max_dd_pct']}% · "
            f"~{live['months']} мес[/]"
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analysis": observed,
        "symbol": SYMBOL,
        "deposit": BALANCE,
        "risk_pct": RISK,
        "params": {
            "session": "06:00–20:00", "sl_points": 2.6, "tp_points": 4.2,
            "max_trades_day": 2, "tf": "M5", "commission_per_lot": fee,
        },
        "metrics": m.as_dict(),
        "avg_month_pct": round(avg_mo, 2),
        "monthly": mo,
        "days": days,
        "equity": _equity(res.equity),
        "candles": _candles(df),
        "markers": _markers(res.trades),
        "trades": _trades(res.trades),
        "commission": round(sum(fee * x.volume for x in res.trades), 2),
        "setups": strat.setups_seen,
        "live_log": live,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sonik_open.json").write_text(_json_dump(payload), encoding="utf-8")
    (OUT / "sonik_open.html").write_text(
        HTML.replace("__PAYLOAD__", _json_dump(payload)), encoding="utf-8",
    )
    con.print(f"[green]Отчёт → {OUT / 'sonik_open.html'}[/]")


HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SonikPulse · реконструкция #SONIK</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{--bg:#0a0c10;--panel:#131820;--line:#243041;--text:#eef2f7;--muted:#8b97a8;
--green:#3dd6c3;--red:#ff7b78;--gold:#e0b84e;--mono:ui-monospace,Consolas,monospace;
--sans:system-ui,-apple-system,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--sans)}
header{padding:20px 24px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#161c28,#0a0c10)}
header h1{margin:0;font:750 22px/1.2 var(--sans)}header .sub{color:var(--muted);margin-top:8px;font-size:13px;max-width:80ch}
.grid{display:grid;grid-template-columns:1.1fr .9fr;gap:0;border-bottom:1px solid var(--line)}
@media(max-width:960px){.grid{grid-template-columns:1fr}}
.panel{padding:14px 18px;border-right:1px solid var(--line)}
.panel h3{margin:0 0 10px;font:600 11px var(--sans);color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
.kpis{display:flex;flex-wrap:wrap;gap:14px;padding:14px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
.kpis div span{display:block;font-size:11px;color:var(--muted)}.kpis div b{font:650 16px var(--mono)}
.pos{color:var(--green)}.neg{color:var(--red)}
.card{background:#1a2230;border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:10px;font-size:13px}
.card b{color:var(--gold)}ul{margin:6px 0 0 18px;color:var(--muted);padding:0}li{margin:3px 0}
.warn{border-color:#8a5a2b;background:#1c1610;color:#e0c090}
#eqChart{height:220px;background:#07090c}#pxChart{height:420px;background:#07090c}
table{width:100%;border-collapse:collapse;font:12px/1.35 var(--mono)}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}th{color:var(--muted);font:600 11px var(--sans);position:sticky;top:0;background:#151b24}
.wrap{max-height:280px;overflow:auto;padding:0 18px 16px}.note{padding:12px 22px;color:var(--muted);font-size:12px}
</style>
</head>
<body>
<header>
  <h1>SonikPulse · разбор #SONIK + аналог на XAUUSD+</h1>
  <p class="sub">Полный лог Amplify (413 сделок) совпадает с карточкой ROI/WR. Ниже — выводы по поведению и бэктест аналога на Bybit CFD (90д).</p>
</header>
<section class="kpis" id="kpis"></section>
<div class="grid">
  <div class="panel">
    <h3>Что делает алгоритм (вывод по логу)</h3>
    <div class="card" id="logic"></div>
    <div class="card warn" id="truth"></div>
  </div>
  <div class="panel">
    <h3>Эквити аналога ($1000 · риск 0.3%)</h3>
    <div id="eqChart"></div>
    <div id="months" style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap"></div>
  </div>
</div>
<div class="panel" style="border-bottom:1px solid var(--line)"><h3>XAUUSD+ M5 · входы SonikPulse</h3><div id="pxChart"></div></div>
<div class="wrap"><table>
  <thead><tr><th>#</th><th>side</th><th>лот</th><th>вход</th><th>выход</th><th>pnl</th><th>мин</th><th></th><th>время</th></tr></thead>
  <tbody id="trades"></tbody>
</table></div>
<p class="note">Это не копия проприетарного кода SONIK, а стратегия по наблюдаемому поведению лога. Комиссии Bybit $6/лот на золото учтены. 90д бэктест не обязан повторить 11-месячную кривую Amplify.</p>
<script type="application/json" id="data">__PAYLOAD__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const m=D.metrics, A=D.analysis, pct=v=>(v>=0?'+':'')+Number(v).toFixed(2)+'%';
const money=v=>(v<0?'−':'+')+'$'+Math.abs(v).toFixed(2);
document.getElementById('kpis').innerHTML=[
  ['Итог', money(m.net_profit)+' ('+pct(m.return_pct)+')', m.net_profit>=0?'pos':'neg'],
  ['~%/мес', pct(D.avg_month_pct), D.avg_month_pct>=0?'pos':'neg'],
  ['Max DD', m.max_drawdown_pct.toFixed(2)+'%', 'neg'],
  ['Сделок', String(m.trades), ''],
  ['WR', m.win_rate.toFixed(1)+'%', ''],
  ['Лузы', String(m.losses), ''],
  ['Комиссии', money(D.commission||0), ''],
].map(([k,v,c])=>`<div><span>${k}</span><b class="${c}">${v}</b></div>`).join('');
document.getElementById('logic').innerHTML=
  `<b>Паттерн счёта</b><ul>`+
  A.inferred_logic.map(x=>`<li>${x}</li>`).join('')+
  `</ul><p style="margin:8px 0 0;color:#8b97a8">Окно: ${A.window}. Удержание: ${A.holds_min}. Сделок/день: ${A.trades_per_day}.</p>`;
const L=A.live_log||{}, C=A.amplify_card||{};
document.getElementById('truth').innerHTML=
  `<b>Amplify-карточка vs лог</b><ul>
  <li>ROI: ${C.roi||'—'} · по логу +${L.roi_pct||'—'}% ($1000)</li>
  <li>WR: ${C.wr||'—'} · ${C.wins_losses||''}</li>
  <li>DD: карточка ${C.dd||'—'} · по эквити лога ${L.max_dd_pct||'—'}%</li>
  <li>Период лога ~${L.months||'—'} мес · ${L.n||'—'} сделок</li>
  </ul><p style="margin:8px 0 0">${C.match||''}. Бэктест справа — наш аналог на 90д Bybit, не копия.</p>`;
document.getElementById('months').innerHTML=(D.monthly||[]).map(x=>
  `<span style="background:#1a2230;border:1px solid #243041;border-radius:8px;padding:4px 8px;font:12px var(--mono)" class="${x.return_pct>=0?'pos':'neg'}">${x.month} ${pct(x.return_pct)}</span>`).join('');
function mk(el){return LightweightCharts.createChart(el,{layout:{background:{color:'#07090c'},textColor:'#8b97a8'},
  grid:{vertLines:{color:'#1a2230'},horzLines:{color:'#1a2230'}},rightPriceScale:{borderColor:'#243041'},
  timeScale:{borderColor:'#243041',timeVisible:true}});}
function parseT(s){const raw=String(s||'').trim().replace(' ','T');if(!raw)return 0;
  const z=/Z$|[+-]\d{2}:?\d{2}$/.test(raw)?raw:raw+'Z';const ms=Date.parse(z);return ms?Math.floor(ms/1000):0;}
const eqEl=document.getElementById('eqChart'); const eqC=mk(eqEl);
eqC.addAreaSeries({lineColor:m.net_profit>=0?'#3dd6c3':'#ff7b78',topColor:m.net_profit>=0?'rgba(61,214,195,.25)':'rgba(255,123,120,.25)',bottomColor:'rgba(0,0,0,0)',lineWidth:2})
  .setData((D.equity||[]).map(p=>({time:parseT(p.t),value:p.v})).filter(x=>x.time));
eqC.timeScale().fitContent();
new ResizeObserver(()=>eqC.applyOptions({width:eqEl.clientWidth,height:eqEl.clientHeight})).observe(eqEl);
const pxEl=document.getElementById('pxChart'); const pxC=mk(pxEl);
const cs=pxC.addCandlestickSeries({upColor:'#26a69a',downColor:'#ef5350',borderVisible:false,wickUpColor:'#26a69a',wickDownColor:'#ef5350'});
cs.setData((D.candles||[]).map(x=>({time:x.time,open:x.open,high:x.high,low:x.low,close:x.close})));
cs.setMarkers((D.markers||[]).map(mk=>({time:mk.time,position:mk.position,color:mk.color,
  shape:mk.kind==='entry'?(mk.side==='buy'?'arrowUp':'arrowDown'):'circle',text:mk.label||''})));
pxC.timeScale().fitContent();
new ResizeObserver(()=>pxC.applyOptions({width:pxEl.clientWidth,height:pxEl.clientHeight})).observe(pxEl);
document.getElementById('trades').innerHTML=(D.trades||[]).map((t,i)=>
  `<tr><td>${i+1}</td><td class="${t.side==='buy'?'pos':'neg'}">${t.side}</td><td>${t.volume}</td>
   <td>${t.entry.toFixed(2)}</td><td>${t.exit.toFixed(2)}</td>
   <td class="${t.pnl>=0?'pos':'neg'}">${money(t.pnl)}</td><td>${(t.bars||0)*5}</td><td>${t.label}</td>
   <td>${(t.opened_at||'').slice(5,16)}</td></tr>`).join('');
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
