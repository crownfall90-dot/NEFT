"""SonikPulse v4 на Tag MT5 · XAUUSD.f (MAE/MFE фильтр).

    python scripts/sonik_mt5_221.py
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
from neft.backtest.engine import Backtester, Costs
from neft.core import symbols
from neft.core.config import ROOT
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.sonik_open import SonikPulse

con = Console()
OUT = ROOT / "dashboard"
SYMBOL = "XAUUSD.f"
DAYS = 221
BALANCE = 1000.0
RISK = 0.25


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


def _candles(df, max_bars=12_000):
    if len(df) > max_bars:
        df = df.iloc[-max_bars:]
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


def live_stats() -> dict:
    df = pd.read_csv(ROOT / "data" / "sonik_trades.csv")
    df["open"] = pd.to_datetime(df["open"])
    df["close"] = pd.to_datetime(df["close"])
    df["hold"] = (df["close"] - df["open"]).dt.total_seconds() / 60.0
    df["pts"] = df["pnl"] / (df["lot"] * 100.0)
    df["win"] = df["pnl"] > 0
    wins, losses = df[df.win], df[~df.win]
    eq = 1000.0 + df.sort_values("close")["pnl"].cumsum()
    peak = eq.cummax()
    dd = float(((peak - eq) / peak * 100).max())
    span = (df["open"].max() - df["open"].min()).days
    return {
        "n": int(len(df)), "wins": int(len(wins)), "losses": int(len(losses)),
        "wr": round(len(wins) / len(df) * 100, 2),
        "pnl": round(float(df["pnl"].sum()), 2),
        "roi_pct": round(float(df["pnl"].sum()) / 10.0, 2),
        "max_dd_pct": round(dd, 2),
        "hold_med": round(float(df["hold"].median()), 1),
        "win_pts_med": round(float(wins["pts"].median()), 2),
        "loss_pts_med": round(float(losses["pts"].median()), 2),
        "trades_day_med": float(df.groupby(df["open"].dt.date).size().median()),
        "months": round(span / 30.4, 1),
        "from": str(df["open"].min())[:10], "to": str(df["open"].max())[:10],
        "mae_mfe_note": "live MAE med −1.8 / MFE +7.3; random impulse −4.4 / +4.5",
    }


def make_rm() -> RiskManager:
    return RiskManager(
        start_balance=BALANCE,
        limits=RiskLimits(
            risk_per_trade_pct=RISK, max_risk_per_trade_pct=3.0,
            min_risk_per_trade_pct=0.0, max_volume=100.0,
            max_daily_loss_pct=100.0, max_drawdown_pct=100.0,
            min_free_margin_pct=0.0,
        ),
    )


def run_one(df, spec, costs, **kw):
    rm = make_rm()
    strat = SonikPulse(
        risk_pct=RISK, risk_manager=rm, spec=spec,
        commission_per_lot=0.0, **kw,
    )
    res = Backtester(strat, rm, costs, BALANCE, SYMBOL).run(df)
    m = metrics.compute(res.equity, res.trades, BALANCE, res.ruined)
    return strat, res, m


def main():
    con.print("[bold cyan]SonikPulse v5[/] · Tag MT5 · volume + trailing dig")
    live = live_stats()
    con.print(
        f"[dim]лог: {live['n']} · WR {live['wr']}% · ROI +{live['roi_pct']}% · "
        f"DD {live['max_dd_pct']}%[/]"
    )
    con.print(f"[dim]{live['mae_mfe_note']}[/]")

    spec = symbols.load(SYMBOL, refresh=True)
    df_m1 = data.load_days(SYMBOL, "M1", days=DAYS, refresh=False)
    df_m5 = data.load_days(SYMBOL, "M5", days=DAYS, refresh=False)
    spr = float(df_m5[df_m5.time.dt.hour.between(7, 18)].spread.median())
    costs = Costs(
        spread_points=spr, contract_size=float(spec.contract_size),
        point=float(spec.point), commission_per_lot=0.0,
        commission_on_close=False, leverage=30,
    )

    # лучший по $ — фикс TP; vol-фильтр — низкий DD / высокий PF
    params = dict(
        max_prior_along=-1.0, min_ema_slope=0.2,
        session_from=(7, 0), session_until=(8, 30),
        session2_from=None, session2_until=None,
        min_body_atr=0.5, max_trades_day=1, cooldown_bars=15,
        min_atr=0.15, max_atr=8.0, sl_points=2.5, tp_points=4.0,
        min_vol_r=0.0, trail_arm=0.0,
    )
    strat, res, m = run_one(df_m1, spec, costs, **params)
    days = max(1, (df_m1.time.iloc[-1] - df_m1.time.iloc[0]).days)
    mo = monthly(res.equity)
    avg_mo = (sum(x["return_pct"] for x in mo) / len(mo)) if mo else m.return_pct / (days / 30)

    # варианты
    _, _, m_vol = run_one(df_m1, spec, costs, **{**params, "min_vol_r": 1.2})
    _, _, m_trail = run_one(
        df_m1, spec, costs,
        **{**params, "trail_arm": 1.5, "trail_dist": 1.2, "tp_points": 5.0},
    )
    _, _, m5m = run_one(
        df_m5, spec, costs,
        max_prior_along=-0.8, min_ema_slope=0.15,
        session_from=(7, 0), session_until=(8, 30),
        session2_from=None, session2_until=None,
        min_body_atr=0.2, max_trades_day=1, cooldown_bars=3,
        min_atr=0.5, max_atr=15.0, sl_points=2.5, tp_points=4.0,
    )
    m5_days = max(1, (df_m5.time.iloc[-1] - df_m5.time.iloc[0]).days)

    t = Table(title=f"SonikPulse v5 · {SYMBOL} · M1 · {days}д")
    t.add_column("вариант")
    t.add_column("ret%", justify="right")
    t.add_column("WR", justify="right")
    t.add_column("DD", justify="right")
    t.add_column("N", justify="right")
    t.add_column("PF", justify="right")
    t.add_row("fixed TP (основной)", f"{m.return_pct:+.2f}", f"{m.win_rate:.0f}%",
              f"{m.max_drawdown_pct:.2f}%", str(m.trades), f"{m.profit_factor:.2f}")
    t.add_row("vol_r≥1.2", f"{m_vol.return_pct:+.2f}", f"{m_vol.win_rate:.0f}%",
              f"{m_vol.max_drawdown_pct:.2f}%", str(m_vol.trades), f"{m_vol.profit_factor:.2f}")
    t.add_row("trail arm1.5", f"{m_trail.return_pct:+.2f}", f"{m_trail.win_rate:.0f}%",
              f"{m_trail.max_drawdown_pct:.2f}%", str(m_trail.trades), f"{m_trail.profit_factor:.2f}")
    t.add_row("M5 контроль", f"{m5m.return_pct:+.2f}", f"{m5m.win_rate:.0f}%",
              f"{m5m.max_drawdown_pct:.2f}%", str(m5m.trades), f"{m5m.profit_factor:.2f}")
    t.add_row("лог Amplify", f"+{live['roi_pct']}", f"{live['wr']}%",
              f"{live['max_dd_pct']}%", str(live["n"]), "—")
    con.print(t)
    con.print(
        "[dim]Volume imbalance не отделяет live от random. "
        "Trail на ИХ входах → WR~86% (как карточка), но режет средний вин. "
        "На наших входах фикс. TP лучше по $.[/]"
    )

    chart_df = data.resample_ohlc(df_m1, "5min")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "broker": "Tag Markets / T.M. Financials",
        "symbol": SYMBOL,
        "days": days,
        "deposit": BALANCE,
        "risk_pct": RISK,
        "leverage": 30,
        "costs": f"spread-only Amplify, med≈{spr:.0f}",
        "params": {**params, "tf": "M1", "session": "07:00–08:30"},
        "discovery": {
            "live_mae_med": -1.8, "live_mfe_med": 7.3,
            "random_mae_med": -4.4, "random_mfe_med": 4.5,
            "live_first_hit_tp_pct": 77, "random_first_hit_tp_pct": 37,
            "volume": "tick imbalance не отделяет live от random; vol_r≥1.2 реже, но PF выше",
            "trailing": "на live-входах trail → WR~86% как Amplify, но avg win падает; на наших входах фикс. TP лучше по $",
            "rule": "prior≤−1 ATR + EMA21 slope≥0.2 + London 07–08:30 + fixed SL2.5/TP4",
        },
        "live_log": live,
        "variants": {
            "fixed": {"return_pct": m.return_pct, "win_rate": m.win_rate,
                      "max_drawdown_pct": m.max_drawdown_pct, "trades": m.trades,
                      "profit_factor": m.profit_factor},
            "vol_r_1_2": {"return_pct": m_vol.return_pct, "win_rate": m_vol.win_rate,
                          "max_drawdown_pct": m_vol.max_drawdown_pct, "trades": m_vol.trades,
                          "profit_factor": m_vol.profit_factor},
            "trail": {"return_pct": m_trail.return_pct, "win_rate": m_trail.win_rate,
                      "max_drawdown_pct": m_trail.max_drawdown_pct, "trades": m_trail.trades,
                      "profit_factor": m_trail.profit_factor},
        },
        "m5_control": {
            "return_pct": m5m.return_pct, "win_rate": m5m.win_rate,
            "max_drawdown_pct": m5m.max_drawdown_pct, "trades": m5m.trades,
            "days": m5_days,
        },
        "metrics": m.as_dict(),
        "avg_month_pct": round(avg_mo, 2),
        "monthly": mo,
        "equity": _equity(res.equity),
        "candles": _candles(chart_df),
        "markers": _markers(res.trades),
        "trades": _trades(res.trades),
        "bar_minutes": 1,
        "setups": strat.setups_seen,
        "period": {"from": str(df_m1.time.iloc[0]), "to": str(df_m1.time.iloc[-1])},
        "history_note": (
            f"v5 fixed {m.return_pct:+.1f}% WR{m.win_rate:.0f}%; "
            f"vol≥1.2 {m_vol.return_pct:+.1f}% WR{m_vol.win_rate:.0f}% DD{m_vol.max_drawdown_pct:.1f}%; "
            f"trail {m_trail.return_pct:+.1f}% WR{m_trail.win_rate:.0f}%. "
            f"До лога +136%/87% не дотягиваем."
        ),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sonik_mt5_221.json").write_text(_json_dump(payload), encoding="utf-8")
    (OUT / "sonik_mt5_221.html").write_text(
        HTML.replace("__PAYLOAD__", _json_dump(payload)), encoding="utf-8",
    )
    con.print(f"[green]Отчёт → {OUT / 'sonik_mt5_221.html'}[/]")


HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SonikPulse v4 · Tag MT5</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{--bg:#0a0c10;--panel:#131820;--line:#243041;--text:#eef2f7;--muted:#8b97a8;
--green:#3dd6c3;--red:#ff7b78;--gold:#e0b84e;--mono:ui-monospace,Consolas,monospace;--sans:system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--sans)}
header{padding:20px 24px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#161c28,#0a0c10)}
header h1{margin:0;font:750 22px/1.2 var(--sans)}header .sub{color:var(--muted);margin-top:8px;font-size:13px;max-width:90ch}
.kpis{display:flex;flex-wrap:wrap;gap:14px;padding:14px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
.kpis div span{display:block;font-size:11px;color:var(--muted)}.kpis div b{font:650 16px var(--mono)}
.pos{color:var(--green)}.neg{color:var(--red)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:0;border-bottom:1px solid var(--line)}
@media(max-width:960px){.grid{grid-template-columns:1fr}}
.panel{padding:14px 18px;border-right:1px solid var(--line)}
.panel h3{margin:0 0 10px;font:600 11px var(--sans);color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
.card{background:#1a2230;border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:10px;font-size:13px}
.card b{color:var(--gold)}ul{margin:6px 0 0 18px;color:var(--muted);padding:0}li{margin:3px 0}
#eqChart{height:240px;background:#07090c}#pxChart{height:420px;background:#07090c}
table{width:100%;border-collapse:collapse;font:12px/1.35 var(--mono)}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}th{color:var(--muted);font:600 11px var(--sans);position:sticky;top:0;background:#151b24}
.wrap{max-height:300px;overflow:auto;padding:0 18px 16px}.note{padding:12px 22px;color:var(--muted);font-size:12px}
</style>
</head>
<body>
<header>
  <h1>SonikPulse v4 · MAE/MFE фильтр · Tag XAUUSD.f</h1>
  <p class="sub">Live-входы отличаются не паттерном свечи, а качеством пути после входа (мелкий MAE, крупный MFE). v4: глубокий откат + разворот EMA21 в ранний Лондон.</p>
</header>
<section class="kpis" id="kpis"></section>
<div class="grid">
  <div class="panel">
    <h3>Лог Amplify</h3><div class="card" id="live"></div>
    <h3>Discovery</h3><div class="card" id="logic"></div>
  </div>
  <div class="panel">
    <h3>Эквити M1</h3><div id="eqChart"></div>
    <div id="months" style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap"></div>
  </div>
</div>
<div class="panel" style="border-bottom:1px solid var(--line)"><h3>Входы (M5 chart)</h3><div id="pxChart"></div></div>
<div class="wrap"><table>
<thead><tr><th>#</th><th>side</th><th>лот</th><th>вход</th><th>выход</th><th>pnl</th><th>мин</th><th></th><th>время</th></tr></thead>
<tbody id="trades"></tbody>
</table></div>
<p class="note" id="note"></p>
<script type="application/json" id="data">__PAYLOAD__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const m=D.metrics, L=D.live_log||{}, P=D.params||{}, Disc=D.discovery||{};
const barMin=D.bar_minutes||1;
const pct=v=>(v>=0?'+':'')+Number(v).toFixed(2)+'%';
const money=v=>(v<0?'−':'+')+'$'+Math.abs(v).toFixed(2);
document.getElementById('kpis').innerHTML=[
  ['Бэктест M1', money(m.net_profit)+' ('+pct(m.return_pct)+')', m.net_profit>=0?'pos':'neg'],
  ['Лог ROI', '+'+L.roi_pct+'%', 'pos'],
  ['WR bt / live', m.win_rate.toFixed(1)+'% / '+L.wr+'%', ''],
  ['DD bt / live', m.max_drawdown_pct.toFixed(2)+'% / '+L.max_dd_pct+'%', 'neg'],
  ['Сделок', m.trades+' / '+L.n, ''],
  ['PF', m.profit_factor.toFixed(2), ''],
].map(([k,v,c])=>`<div><span>${k}</span><b class="${c}">${v}</b></div>`).join('');
document.getElementById('live').innerHTML=`<b>${L.n} сделок · ${L.from} → ${L.to}</b>
<ul><li>WR ${L.wr}% · ROI +${L.roi_pct}% · DD ${L.max_dd_pct}%</li>
<li>hold ${L.hold_med}м · win ${L.win_pts_med} / loss ${L.loss_pts_med} pts</li></ul>`;
document.getElementById('logic').innerHTML=`<b>${Disc.rule||''}</b>
<ul><li>Live MAE/MFE ${Disc.live_mae_med} / +${Disc.live_mfe_med} vs random ${Disc.random_mae_med} / +${Disc.random_mfe_med}</li>
<li>First-hit TP: live ${Disc.live_first_hit_tp_pct}% vs random ${Disc.random_first_hit_tp_pct}%</li>
<li>SL ${P.sl_points} / TP ${P.tp_points} · ${P.session} · TF ${P.tf}</li>
<li>M5 контроль: ${(D.m5_control||{}).return_pct?.toFixed?.(1)}% WR ${(D.m5_control||{}).win_rate?.toFixed?.(0)}%</li></ul>`;
document.getElementById('note').textContent=D.history_note||'';
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
   <td class="${t.pnl>=0?'pos':'neg'}">${money(t.pnl)}</td><td>${(t.bars||0)*barMin}</td><td>${t.label}</td>
   <td>${(t.opened_at||'').slice(5,16)}</td></tr>`).join('');
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
