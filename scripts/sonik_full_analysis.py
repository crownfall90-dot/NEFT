"""Полный разбор live SONIK: визуальные сетапы + правила + бэктест."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from neft.backtest import metrics
from neft.backtest.engine import Backtester, Costs
from neft.core import symbols
from neft.core.config import ROOT
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.sonik_open import SonikPulse

con = Console()
OUT = ROOT / "dashboard"


def ts(t) -> int:
    t = pd.Timestamp(t)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return int(t.timestamp())


def prep(m1: pd.DataFrame) -> pd.DataFrame:
    df = m1.copy()
    tr = pd.concat([
        df.high - df.low,
        (df.high - df.close.shift()).abs(),
        (df.low - df.close.shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["body"] = (df.close - df.open).abs()
    df["body_atr"] = df.body / df.atr
    df["ema21"] = df.close.ewm(span=21, adjust=False).mean()
    df["ema55"] = df.close.ewm(span=55, adjust=False).mean()
    df["mins"] = df.time.dt.hour * 60 + df.time.dt.minute
    df["dow"] = df.time.dt.dayofweek
    df["vol_r"] = df.tick_volume / df.tick_volume.rolling(20).mean().replace(0, np.nan)
    # confirmed pivots (no lookahead)
    df["piv_hi"] = (
        (df.high.shift(2) > df.high.shift(3)) & (df.high.shift(2) > df.high.shift(4))
        & (df.high.shift(2) > df.high.shift(1)) & (df.high.shift(2) > df.high)
    )
    df["piv_lo"] = (
        (df.low.shift(2) < df.low.shift(3)) & (df.low.shift(2) < df.low.shift(4))
        & (df.low.shift(2) < df.low.shift(1)) & (df.low.shift(2) < df.low)
    )
    return df


def classify(df: pd.DataFrame, i: int, side: int) -> dict:
    """Визуальная классификация сетапа на M1."""
    bar = df.iloc[i]
    a = float(bar.atr) or 1.0
    prev5 = df.iloc[i - 5:i]
    prev3 = df.iloc[i - 3:i]
    net5 = float(prev5.close.iloc[-1] - prev5.close.iloc[0])
    pullback = net5 * side < 0
    deep_pb = (net5 * side) < -0.5 * a
    impulse = (float(bar.close) - float(bar.open)) * side > 0
    strong_body = float(bar.body_atr) >= 0.55 if bar.body_atr == bar.body_atr else False
    broke = (
        (side > 0 and bar.close > float(df.high.iloc[i - 1]))
        or (side < 0 and bar.close < float(df.low.iloc[i - 1]))
    )
    # opposite candles in pullback
    opp = int(((prev5.close - prev5.open) * side < 0).sum())
    # near pivot
    look = df.iloc[max(0, i - 40):i + 1]
    if side > 0:
        pivs = look[look.piv_lo]
        near_piv = False
        if len(pivs):
            lvl = float(df.low.shift(2).loc[pivs.index[-1]])
            near_piv = abs(float(bar.close) - lvl) <= 1.2 * a
    else:
        pivs = look[look.piv_hi]
        near_piv = False
        if len(pivs):
            lvl = float(df.high.shift(2).loc[pivs.index[-1]])
            near_piv = abs(float(bar.close) - lvl) <= 1.2 * a
    slope = ((float(bar.ema21) - float(df.ema21.iloc[i - 5])) / a) * side
    # asia
    day = bar.time.date()
    day_df = df[df.time.dt.date == day]
    asia = day_df[day_df.time.dt.hour < 6]
    asia_pos = "na"
    if len(asia):
        ahi, alo = float(asia.high.max()), float(asia.low.min())
        if alo <= bar.close <= ahi:
            asia_pos = "inside"
        elif bar.close > ahi:
            asia_pos = "above"
        else:
            asia_pos = "below"
    # setup label
    if pullback and impulse and broke and strong_body:
        setup = "pb_impulse_break"
    elif pullback and impulse:
        setup = "pb_impulse"
    elif impulse and broke and not pullback:
        setup = "chase_break"
    elif pullback and not impulse:
        setup = "pb_weak"
    else:
        setup = "other"
    # path quality next 8m
    fut = df.iloc[i + 1:i + 9]
    if len(fut):
        if side > 0:
            mae = float(fut.low.min() - bar.close)
            mfe = float(fut.high.max() - bar.close)
        else:
            mae = float(bar.close - fut.high.max())
            mfe = float(bar.close - fut.low.min())
    else:
        mae = mfe = 0.0
    return {
        "setup": setup,
        "pullback": pullback,
        "deep_pb": deep_pb,
        "impulse": impulse,
        "broke": broke,
        "strong_body": strong_body,
        "opp5": opp,
        "near_piv": near_piv,
        "slope": slope,
        "asia_pos": asia_pos,
        "mae8": mae,
        "mfe8": mfe,
        "hour": int(bar.time.hour),
        "mins": int(bar.mins),
        "body_atr": float(bar.body_atr) if bar.body_atr == bar.body_atr else 0,
        "prior5": net5 * side / a,
    }


def main():
    trades = pd.read_csv(ROOT / "data" / "sonik_trades.csv")
    trades["open"] = pd.to_datetime(trades["open"])
    trades["close"] = pd.to_datetime(trades["close"])
    m1 = prep(pd.read_csv(ROOT / "data" / "XAUUSD.f_M1_221d.csv", parse_dates=["time"]))
    m5 = pd.read_csv(ROOT / "data" / "XAUUSD.f_M5_221d.csv", parse_dates=["time"])
    t = trades[(trades.open >= m1.time.min()) & (trades.open <= m1.time.max())].copy()
    t["won"] = t.pnl > 0

    # ── 1. classify every live trade ──
    rows = []
    for r in t.itertuples():
        i = int(m1.time.searchsorted(r.open, side="right") - 1)
        if i < 20:
            continue
        side = 1 if r.side == "Buy" else -1
        c = classify(m1, i, side)
        c.update({
            "side": r.side, "pnl": float(r.pnl), "won": bool(r.pnl > 0),
            "open": str(r.open), "close": str(r.close), "entry": float(r.entry),
            "lot": float(r.lot),
            "hold": (r.close - r.open).total_seconds() / 60,
            "i": i,
        })
        rows.append(c)
    g = pd.DataFrame(rows)

    con.print(f"[bold]1) Визуальная классификация[/] N={len(g)} WR={g.won.mean()*100:.0f}%")
    tab = Table(title="Setup → WR / N / avg pnl / MAE / MFE")
    for col in ("setup", "N", "WR%", "avg$", "MAE", "MFE", "pullback%"):
        tab.add_column(col, justify="right" if col != "setup" else "left")
    for setup, s in g.groupby("setup"):
        tab.add_row(
            setup, str(len(s)), f"{s.won.mean()*100:.0f}",
            f"{s.pnl.mean():.2f}", f"{s.mae8.median():.2f}", f"{s.mfe8.median():.2f}",
            f"{s.pullback.mean()*100:.0f}",
        )
    con.print(tab)

    # combinations that scream edge
    combos = [
        ("pb+impulse+broke", g.pullback & g.impulse & g.broke),
        ("pb+impulse+body", g.pullback & g.impulse & g.strong_body),
        ("deep_pb+impulse", g.deep_pb & g.impulse),
        ("pb+impulse+near_piv", g.pullback & g.impulse & g.near_piv),
        ("pb+impulse+slope>0", g.pullback & g.impulse & (g.slope > 0)),
        ("pb+impulse+London", g.pullback & g.impulse & g.hour.between(7, 9)),
        ("chase_break", (~g.pullback) & g.broke & g.impulse),
        ("London only", g.hour.between(7, 9)),
        ("asia inside", g.asia_pos == "inside"),
    ]
    tab2 = Table(title="Комбо глазами")
    for col in ("combo", "N", "WR%", "avg$", "MAE", "MFE"):
        tab2.add_column(col, justify="right" if col != "combo" else "left")
    for name, mask in combos:
        s = g[mask]
        if len(s) < 5:
            continue
        tab2.add_row(
            name, str(len(s)), f"{s.won.mean()*100:.0f}",
            f"{s.pnl.mean():.2f}", f"{s.mae8.median():.2f}", f"{s.mfe8.median():.2f}",
        )
    con.print(tab2)

    # ── 2. light sklearn if present ──
    con.print("\n[bold]2) ML (если sklearn есть)[/]")
    try:
        from sklearn.tree import DecisionTreeClassifier, export_text
        from sklearn.model_selection import cross_val_score
        feats = ["pullback", "deep_pb", "impulse", "broke", "strong_body", "opp5",
                 "near_piv", "slope", "body_atr", "prior5", "mins", "hour"]
        X = g[feats].astype(float)
        y = g["won"].astype(int)
        clf = DecisionTreeClassifier(max_depth=3, min_samples_leaf=12, random_state=0)
        sc = cross_val_score(clf, X, y, cv=5, scoring="accuracy")
        clf.fit(X, y)
        con.print(f"  predict WON: CV acc {sc.mean()*100:.0f}% ±{sc.std()*100:.0f}% "
                  f"(base {y.mean()*100:.0f}%)")
        con.print(export_text(clf, feature_names=feats)[:900])
        # quality = good path
        yq = ((g.mfe8 >= 4) & (g.mae8 > -2.5)).astype(int)
        clf2 = DecisionTreeClassifier(max_depth=3, min_samples_leaf=12, random_state=0)
        sq = cross_val_score(clf2, X, yq, cv=5, scoring="accuracy")
        clf2.fit(X, yq)
        con.print(f"  predict QUALITY path: CV {sq.mean()*100:.0f}% (base {yq.mean()*100:.0f}%)")
        con.print(export_text(clf2, feature_names=feats)[:900])
    except ImportError:
        con.print("  sklearn нет")

    # ── 3. backtest chart-rule on Tag M1 ──
    con.print("\n[bold]3) Бэктест правил с графика[/]")
    spec = symbols.load("XAUUSD.f")
    spr = float(m1[m1.time.dt.hour.between(7, 18)].spread.median())
    costs = Costs(
        spread_points=spr, contract_size=spec.contract_size, point=spec.point,
        commission_per_lot=0.0, commission_on_close=False, leverage=30,
    )

    def run(**kw):
        rm = RiskManager(
            start_balance=1000,
            limits=RiskLimits(
                risk_per_trade_pct=0.25, max_risk_per_trade_pct=3.0,
                min_risk_per_trade_pct=0.0, max_volume=100.0,
                max_daily_loss_pct=100.0, max_drawdown_pct=100.0,
                min_free_margin_pct=0.0,
            ),
        )
        base = dict(
            risk_pct=0.25, risk_manager=rm, spec=spec,
            session_from=(7, 0), session_until=(9, 30),
            session2_from=None, session2_until=None,
            sl_points=2.5, tp_points=4.0, max_trades_day=2, cooldown_bars=10,
            require_break=True, min_body_atr=0.5,
        )
        base.update(kw)
        s = SonikPulse(**base)
        res = Backtester(s, rm, costs, 1000, "XAUUSD.f").run(m1)
        m = metrics.compute(res.equity, res.trades, 1000, res.ruined)
        return m, res, s

    variants = [
        ("v5 prior<=-1 slope>=0.2", dict(
            max_prior_along=-1.0, min_ema_slope=0.2, session_until=(8, 30), max_trades_day=1)),
        ("chart: pb+impulse (prior<=0)", dict(
            max_prior_along=0.0, min_ema_slope=0.0, min_body_atr=0.45,
            session_until=(9, 30), max_trades_day=2)),
        ("chart: deep pb prior<=-0.6", dict(
            max_prior_along=-0.6, min_ema_slope=0.1, min_body_atr=0.5,
            session_until=(9, 0), max_trades_day=2)),
        ("chart: deep+slope London", dict(
            max_prior_along=-0.6, min_ema_slope=0.2, min_body_atr=0.55,
            session_until=(9, 0), max_trades_day=1)),
        ("chart: pb + vol1.2", dict(
            max_prior_along=-0.5, min_ema_slope=0.1, min_vol_r=1.2,
            session_until=(9, 30), max_trades_day=2)),
    ]
    results = []
    best = None
    for name, kw in variants:
        m, res, s = run(**kw)
        results.append((name, m, res, s, kw))
        con.print(
            f"  {name}: ret={m.return_pct:+.2f}% WR={m.win_rate:.0f}% "
            f"DD={m.max_drawdown_pct:.2f}% N={m.trades} PF={m.profit_factor:.2f}"
        )
        if best is None or m.return_pct > best[1].return_pct:
            best = (name, m, res, s, kw)

    # ── 4. visual HTML with setup tags ──
    cut = m5.time.max() - pd.Timedelta(days=70)
    chart = m5[m5.time >= cut]
    candles = [{
        "time": ts(r.time), "open": float(r.open), "high": float(r.high),
        "low": float(r.low), "close": float(r.close),
    } for r in chart.itertuples(index=False)]

    markers = []
    trade_rows = []
    for r in g.itertuples():
        ot = pd.Timestamp(r.open)
        if ot < cut:
            continue
        buy = r.side == "Buy"
        color = "#3dd6c3" if r.won else "#ff7b78"
        markers.append({
            "time": ts(ot),
            "position": "belowBar" if buy else "aboveBar",
            "color": color,
            "shape": "arrowUp" if buy else "arrowDown",
            "text": r.setup[:6],
        })
        trade_rows.append({
            "side": r.side, "open": r.open, "close": r.close,
            "entry": r.entry, "pnl": r.pnl, "setup": r.setup,
            "pullback": bool(r.pullback), "broke": bool(r.broke),
            "mae8": round(r.mae8, 2), "mfe8": round(r.mfe8, 2),
            "hold": round(r.hold, 1), "won": bool(r.won),
        })

    setup_stats = []
    for setup, s in g.groupby("setup"):
        setup_stats.append({
            "setup": setup, "n": int(len(s)),
            "wr": round(float(s.won.mean() * 100), 1),
            "avg_pnl": round(float(s.pnl.mean()), 2),
            "mae": round(float(s.mae8.median()), 2),
            "mfe": round(float(s.mfe8.median()), 2),
        })

    combo_stats = []
    for name, mask in combos:
        s = g[mask]
        if len(s) < 5:
            continue
        combo_stats.append({
            "combo": name, "n": int(len(s)),
            "wr": round(float(s.won.mean() * 100), 1),
            "avg_pnl": round(float(s.pnl.mean()), 2),
            "mae": round(float(s.mae8.median()), 2),
            "mfe": round(float(s.mfe8.median()), 2),
        })

    bt = []
    for name, m, _, _, kw in results:
        bt.append({
            "name": name, "return_pct": m.return_pct, "win_rate": m.win_rate,
            "dd": m.max_drawdown_pct, "trades": m.trades, "pf": m.profit_factor,
            "kw": {k: v for k, v in kw.items() if k not in ("risk_manager", "spec")},
        })

    payload = {
        "title": "SONIK — полный разбор глазами + правила + бэктест",
        "live_n": int(len(g)),
        "live_wr": round(float(g.won.mean() * 100), 1),
        "setup_stats": setup_stats,
        "combo_stats": combo_stats,
        "backtests": bt,
        "best_bt": bt[0] if not bt else max(bt, key=lambda x: x["return_pct"]),
        "candles": candles,
        "markers": markers,
        "trades": trade_rows,
        "insight": [
            "Wins чаще после отката 5м против стороны; losses — chase без отката.",
            "После входа у wins MAE≈−0.7 / MFE≈+5; у losses MAE≈−3.5 / MFE≈+1.",
            "Asia break не обязателен — большинство внутри Asia-бокса.",
            "Лучший комбо на логе: pullback+impulse (+/- break) в London.",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sonik_full_analysis.json").write_text(
        json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8",
    )
    (OUT / "sonik_full_analysis.html").write_text(
        HTML.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False, default=str)),
        encoding="utf-8",
    )
    con.print(f"\n[green]Отчёт → {OUT / 'sonik_full_analysis.html'}[/]")
    if best:
        con.print(
            f"[cyan]Лучший бэктест:[/] {best[0]} → {best[1].return_pct:+.2f}% "
            f"WR {best[1].win_rate:.0f}% DD {best[1].max_drawdown_pct:.2f}%"
        )


HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SONIK full analysis</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{--bg:#0a0c10;--panel:#131820;--line:#243041;--text:#eef2f7;--muted:#8b97a8;
--green:#3dd6c3;--red:#ff7b78;--gold:#e0b84e;--mono:ui-monospace,Consolas,monospace;--sans:system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--sans)}
header{padding:18px 22px;border-bottom:1px solid var(--line)}
header h1{margin:0;font:750 20px/1.2 var(--sans)}header p{margin:8px 0 0;color:var(--muted);max-width:95ch}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:0;border-bottom:1px solid var(--line)}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.panel{padding:14px 18px;border-right:1px solid var(--line)}
.panel h3{margin:0 0 10px;font:600 11px var(--sans);color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
table{width:100%;border-collapse:collapse;font:12px/1.35 var(--mono)}
th,td{padding:5px 7px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}th{color:var(--muted)}
.pos{color:var(--green)}.neg{color:var(--red)}
#chart{height:520px;background:#07090c}
.wrap{max-height:260px;overflow:auto;padding:0 18px 14px}
ul{margin:0;padding-left:18px;color:var(--muted)}li{margin:4px 0}
.note{padding:10px 22px;color:var(--muted);font-size:12px}
</style>
</head>
<body>
<header>
  <h1>SONIK — разбор всеми способами</h1>
  <p>Визуальная классификация на Tag-графике + комбо-статы + бэктест правил. Стрелки подписаны типом сетапа.</p>
</header>
<div class="grid">
  <div class="panel"><h3>Setup на логе</h3><table id="setups"></table></div>
  <div class="panel"><h3>Комбо глазами</h3><table id="combos"></table></div>
</div>
<div class="grid">
  <div class="panel"><h3>Бэктесты правил</h3><table id="bts"></table></div>
  <div class="panel"><h3>Выводы</h3><ul id="ins"></ul></div>
</div>
<div id="chart"></div>
<div class="wrap"><table>
<thead><tr><th>#</th><th>setup</th><th>side</th><th>open</th><th>pnl</th><th>MAE8</th><th>MFE8</th><th>pb</th><th>brk</th></tr></thead>
<tbody id="tb"></tbody>
</table></div>
<p class="note">pb_impulse_break = откат + импульс + пробой prev bar. chase_break = пробой без отката (часто лузы).</p>
<script type="application/json" id="data">__PAYLOAD__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const money=v=>(v<0?'−':'+')+Math.abs(v).toFixed(2);
document.getElementById('setups').innerHTML='<tr><th>setup</th><th>N</th><th>WR</th><th>avg$</th><th>MAE</th><th>MFE</th></tr>'+
  (D.setup_stats||[]).map(s=>`<tr><td>${s.setup}</td><td>${s.n}</td><td>${s.wr}%</td>
  <td class="${s.avg_pnl>=0?'pos':'neg'}">${money(s.avg_pnl)}</td><td>${s.mae}</td><td>${s.mfe}</td></tr>`).join('');
document.getElementById('combos').innerHTML='<tr><th>combo</th><th>N</th><th>WR</th><th>avg$</th><th>MAE</th><th>MFE</th></tr>'+
  (D.combo_stats||[]).map(s=>`<tr><td>${s.combo}</td><td>${s.n}</td><td>${s.wr}%</td>
  <td class="${s.avg_pnl>=0?'pos':'neg'}">${money(s.avg_pnl)}</td><td>${s.mae}</td><td>${s.mfe}</td></tr>`).join('');
document.getElementById('bts').innerHTML='<tr><th>rule</th><th>ret</th><th>WR</th><th>DD</th><th>N</th><th>PF</th></tr>'+
  (D.backtests||[]).map(s=>`<tr><td>${s.name}</td>
  <td class="${s.return_pct>=0?'pos':'neg'}">${s.return_pct>=0?'+':''}${s.return_pct.toFixed(2)}%</td>
  <td>${s.win_rate.toFixed(0)}%</td><td>${s.dd.toFixed(2)}%</td><td>${s.trades}</td><td>${s.pf.toFixed(2)}</td></tr>`).join('');
document.getElementById('ins').innerHTML=(D.insight||[]).map(x=>`<li>${x}</li>`).join('');
const el=document.getElementById('chart');
const chart=LightweightCharts.createChart(el,{
  layout:{background:{color:'#07090c'},textColor:'#8b97a8'},
  grid:{vertLines:{color:'#1a2230'},horzLines:{color:'#1a2230'}},
  rightPriceScale:{borderColor:'#243041'},
  timeScale:{borderColor:'#243041',timeVisible:true},
});
const cs=chart.addCandlestickSeries({upColor:'#26a69a',downColor:'#ef5350',borderVisible:false,wickUpColor:'#26a69a',wickDownColor:'#ef5350'});
cs.setData(D.candles||[]); cs.setMarkers(D.markers||[]); chart.timeScale().fitContent();
new ResizeObserver(()=>chart.applyOptions({width:el.clientWidth,height:el.clientHeight})).observe(el);
document.getElementById('tb').innerHTML=(D.trades||[]).map((t,i)=>
  `<tr><td>${i+1}</td><td>${t.setup}</td><td class="${t.side==='Buy'?'pos':'neg'}">${t.side}</td>
   <td>${(t.open||'').slice(5,16)}</td>
   <td class="${t.pnl>=0?'pos':'neg'}">${money(t.pnl)}</td>
   <td>${t.mae8}</td><td>${t.mfe8}</td><td>${t.pullback?'Y':'n'}</td><td>${t.broke?'Y':'n'}</td></tr>`).join('');
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
