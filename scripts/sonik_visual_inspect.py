"""Визуальный разбор сделок Amplify на Tag XAUUSD.f + HTML-чарт."""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from rich.console import Console
from rich.panel import Panel

con = Console()
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dashboard"


def ts(t) -> int:
    t = pd.Timestamp(t)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return int(t.timestamp())


def describe_window(df: pd.DataFrame, i: int, side: int, n_before=12, n_after=8) -> str:
    """Текстовый «взгляд на график» вокруг входа."""
    w = df.iloc[max(0, i - n_before):i + n_after + 1]
    entry_bar = df.iloc[i]
    lines = []
    lines.append(
        f"вход {entry_bar.time} close={entry_bar.close:.2f} "
        f"{'BUY' if side > 0 else 'SELL'} "
        f"свеча {'зелёная' if entry_bar.close > entry_bar.open else 'красная'} "
        f"body={abs(entry_bar.close - entry_bar.open):.2f} "
        f"range={entry_bar.high - entry_bar.low:.2f}"
    )
    # last 5 bars narrative
    prev = df.iloc[i - 5:i]
    downs = (prev.close < prev.open).sum()
    ups = (prev.close > prev.open).sum()
    net = float(prev.close.iloc[-1] - prev.close.iloc[0])
    lines.append(
        f"  до входа 5м: {downs}↓/{ups}↑ net={net:+.2f} "
        f"{'откат против' if net * side < 0 else 'уже по направлению'}"
    )
    # structure: higher highs / lower lows
    hh = float(prev.high.max())
    ll = float(prev.low.min())
    lines.append(
        f"  5м high/low={hh:.2f}/{ll:.2f}; "
        f"close vs range={((entry_bar.close - ll) / (hh - ll + 1e-9)):.2f}"
    )
    # after
    fut = df.iloc[i + 1:i + n_after + 1]
    if len(fut):
        if side > 0:
            mae = float((fut.low.min() - entry_bar.close))
            mfe = float((fut.high.max() - entry_bar.close))
        else:
            mae = float((entry_bar.close - fut.high.max()))
            mfe = float((entry_bar.close - fut.low.min()))
        lines.append(f"  после {n_after}м: MAE={mae:+.2f} MFE={mfe:+.2f}")
    # Asia box that day
    day = entry_bar.time.date()
    day_df = df[df.time.dt.date == day]
    asia = day_df[(day_df.time.dt.hour >= 0) & (day_df.time.dt.hour < 6)]
    if len(asia):
        ahi, alo = float(asia.high.max()), float(asia.low.min())
        pos = "внутри Asia" if alo <= entry_bar.close <= ahi else (
            "выше Asia" if entry_bar.close > ahi else "ниже Asia"
        )
        lines.append(f"  Asia box {alo:.2f}–{ahi:.2f} → вход {pos}")
    return "\n".join(lines)


def main():
    trades = pd.read_csv(ROOT / "data" / "sonik_trades.csv")
    trades["open"] = pd.to_datetime(trades["open"])
    trades["close"] = pd.to_datetime(trades["close"])
    m1 = pd.read_csv(ROOT / "data" / "XAUUSD.f_M1_221d.csv", parse_dates=["time"])
    m5 = pd.read_csv(ROOT / "data" / "XAUUSD.f_M5_221d.csv", parse_dates=["time"])

    t = trades[(trades.open >= m1.time.min()) & (trades.open <= m1.time.max())].copy()
    t["won"] = t.pnl > 0
    con.print(f"[cyan]overlap live на Tag M1:[/] {len(t)} сделок "
              f"(WR {t.won.mean()*100:.0f}%)")

    # --- глазами: последние 8 вин и 6 лузов ---
    wins = t[t.won].sort_values("open").tail(8)
    losses = t[~t.won].sort_values("open").tail(6)

    con.print("\n[bold green]=== WINS — что на графике ===[/]")
    for r in wins.itertuples():
        i = int(m1.time.searchsorted(r.open, side="right") - 1)
        side = 1 if r.side == "Buy" else -1
        txt = describe_window(m1, i, side)
        con.print(Panel(
            txt + f"\n  live pnl=${r.pnl:.2f} hold="
                  f"{(r.close - r.open).total_seconds()/60:.0f}м entry={r.entry}",
            title=f"{r.side} {str(r.open)[5:16]}",
            border_style="green",
        ))

    con.print("\n[bold red]=== LOSSES — что на графике ===[/]")
    for r in losses.itertuples():
        i = int(m1.time.searchsorted(r.open, side="right") - 1)
        side = 1 if r.side == "Buy" else -1
        txt = describe_window(m1, i, side)
        con.print(Panel(
            txt + f"\n  live pnl=${r.pnl:.2f} hold="
                  f"{(r.close - r.open).total_seconds()/60:.0f}м entry={r.entry}",
            title=f"{r.side} {str(r.open)[5:16]}",
            border_style="red",
        ))

    # --- агрегаты «как глаз» ---
    rows = []
    for r in t.itertuples():
        i = int(m1.time.searchsorted(r.open, side="right") - 1)
        if i < 10:
            continue
        side = 1 if r.side == "Buy" else -1
        prev = m1.iloc[i - 5:i]
        net = float(prev.close.iloc[-1] - prev.close.iloc[0])
        bar = m1.iloc[i]
        fut = m1.iloc[i + 1:i + 9]
        if side > 0:
            mae = float(fut.low.min() - bar.close) if len(fut) else 0
            mfe = float(fut.high.max() - bar.close) if len(fut) else 0
        else:
            mae = float(bar.close - fut.high.max()) if len(fut) else 0
            mfe = float(bar.close - fut.low.min()) if len(fut) else 0
        day = bar.time.date()
        day_df = m1[m1.time.dt.date == day]
        asia = day_df[(day_df.time.dt.hour >= 0) & (day_df.time.dt.hour < 6)]
        asia_pos = "na"
        if len(asia):
            ahi, alo = float(asia.high.max()), float(asia.low.min())
            if alo <= bar.close <= ahi:
                asia_pos = "inside"
            elif bar.close > ahi:
                asia_pos = "above"
            else:
                asia_pos = "below"
        rows.append({
            "won": r.pnl > 0,
            "pullback": net * side < 0,
            "impulse_bar": (bar.close - bar.open) * side > 0,
            "broke_prev": (
                (side > 0 and bar.close > float(m1.high.iloc[i - 1]))
                or (side < 0 and bar.close < float(m1.low.iloc[i - 1]))
            ),
            "asia_pos": asia_pos,
            "hour": r.open.hour,
            "mae": mae,
            "mfe": mfe,
            "hold": (r.close - r.open).total_seconds() / 60,
        })
    g = pd.DataFrame(rows)
    con.print("\n[bold]Паттерн глазами (доля сделок)[/]")
    con.print(f"  откат 5м против стороны: {g.pullback.mean()*100:.0f}% "
              f"(wins {g[g.won].pullback.mean()*100:.0f}% / "
              f"loss {g[~g.won].pullback.mean()*100:.0f}%)")
    con.print(f"  импульсная свеча входа: {g.impulse_bar.mean()*100:.0f}%")
    con.print(f"  пробой prev high/low: {g.broke_prev.mean()*100:.0f}%")
    con.print(f"  Asia: {g.asia_pos.value_counts(normalize=True).mul(100).round(0).to_dict()}")
    con.print(f"  час входа med={g.hour.median():.0f} "
              f"07–09: {(g.hour.between(7,9)).mean()*100:.0f}%")
    con.print(f"  после 8м MAE med win={g[g.won].mae.median():.2f} "
              f"loss={g[~g.won].mae.median():.2f} | "
              f"MFE win={g[g.won].mfe.median():.2f} loss={g[~g.won].mfe.median():.2f}")

    # --- HTML chart: M5 + all live markers ---
    # use last 60 days of M5 for file size, all overlap trades as markers
    cut = m5.time.max() - pd.Timedelta(days=70)
    chart = m5[m5.time >= cut]
    candles = [{
        "time": ts(r.time), "open": float(r.open), "high": float(r.high),
        "low": float(r.low), "close": float(r.close),
    } for r in chart.itertuples(index=False)]

    markers = []
    trade_rows = []
    for r in t.itertuples():
        if r.open < cut:
            continue
        buy = r.side == "Buy"
        markers.append({
            "time": ts(r.open),
            "position": "belowBar" if buy else "aboveBar",
            "color": "#3dd6c3" if r.pnl > 0 else "#ff7b78",
            "shape": "arrowUp" if buy else "arrowDown",
            "text": f"{'+' if r.pnl > 0 else ''}{r.pnl:.1f}",
        })
        markers.append({
            "time": ts(r.close),
            "position": "aboveBar" if buy else "belowBar",
            "color": "#ffc107",
            "shape": "circle",
            "text": "x",
        })
        trade_rows.append({
            "side": r.side, "open": str(r.open), "close": str(r.close),
            "entry": float(r.entry), "pnl": float(r.pnl), "lot": float(r.lot),
            "hold_min": round((r.close - r.open).total_seconds() / 60, 1),
        })

    payload = {
        "symbol": "XAUUSD.f",
        "note": "Живые сделки Amplify на истории Tag MT5. Зелёные/красные стрелки = вход (цвет по pnl), круг = выход.",
        "candles": candles,
        "markers": markers,
        "trades": trade_rows,
        "stats": {
            "n": int(len(t)),
            "wr": round(float(t.won.mean() * 100), 1),
            "pullback_pct": round(float(g.pullback.mean() * 100), 1),
            "broke_pct": round(float(g.broke_prev.mean() * 100), 1),
            "london_pct": round(float(g.hour.between(7, 9).mean() * 100), 1),
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sonik_visual.json").write_text(
        json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8",
    )
    (OUT / "sonik_visual.html").write_text(
        HTML.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False, default=str)),
        encoding="utf-8",
    )
    con.print(f"\n[green]График со сделками → {OUT / 'sonik_visual.html'}[/]")


HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SONIK live trades on Tag chart</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{--bg:#0a0c10;--panel:#131820;--line:#243041;--text:#eef2f7;--muted:#8b97a8;
--green:#3dd6c3;--red:#ff7b78;--mono:ui-monospace,Consolas,monospace;--sans:system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--sans)}
header{padding:16px 22px;border-bottom:1px solid var(--line)}
header h1{margin:0;font:750 20px/1.2 var(--sans)}header p{margin:6px 0 0;color:var(--muted);font-size:13px}
.kpis{display:flex;gap:16px;flex-wrap:wrap;padding:12px 22px;background:var(--panel);border-bottom:1px solid var(--line)}
.kpis div span{display:block;font-size:11px;color:var(--muted)}.kpis b{font:650 16px var(--mono)}
#chart{height:560px;background:#07090c}
.wrap{max-height:280px;overflow:auto;padding:0 18px 16px}
table{width:100%;border-collapse:collapse;font:12px var(--mono)}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}th{color:var(--muted);position:sticky;top:0;background:#151b24}
.pos{color:var(--green)}.neg{color:var(--red)}.note{padding:10px 22px;color:var(--muted);font-size:12px}
</style>
</head>
<body>
<header>
  <h1>Amplify / #SONIK — сделки на графике Tag XAUUSD.f</h1>
  <p id="sub"></p>
</header>
<section class="kpis" id="kpis"></section>
<div id="chart"></div>
<div class="wrap"><table>
<thead><tr><th>#</th><th>side</th><th>open</th><th>close</th><th>entry</th><th>pnl</th><th>мин</th></tr></thead>
<tbody id="tb"></tbody>
</table></div>
<p class="note">Стрелка = вход (зелёная если сделка в плюс, красная если минус). Круг = выход. Смотри кластеры 07–09 и поведение до/после стрелки.</p>
<script type="application/json" id="data">__PAYLOAD__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
document.getElementById('sub').textContent=D.note;
const S=D.stats||{};
document.getElementById('kpis').innerHTML=[
  ['Сделок', S.n],['WR', S.wr+'%'],['Откат 5м', S.pullback_pct+'%'],
  ['Пробой бара', S.broke_pct+'%'],['London 07–09', S.london_pct+'%'],
].map(([k,v])=>`<div><span>${k}</span><b>${v}</b></div>`).join('');
const el=document.getElementById('chart');
const chart=LightweightCharts.createChart(el,{
  layout:{background:{color:'#07090c'},textColor:'#8b97a8'},
  grid:{vertLines:{color:'#1a2230'},horzLines:{color:'#1a2230'}},
  rightPriceScale:{borderColor:'#243041'},
  timeScale:{borderColor:'#243041',timeVisible:true,secondsVisible:false},
});
const cs=chart.addCandlestickSeries({
  upColor:'#26a69a',downColor:'#ef5350',borderVisible:false,
  wickUpColor:'#26a69a',wickDownColor:'#ef5350',
});
cs.setData(D.candles||[]);
cs.setMarkers(D.markers||[]);
chart.timeScale().fitContent();
new ResizeObserver(()=>chart.applyOptions({width:el.clientWidth,height:el.clientHeight})).observe(el);
document.getElementById('tb').innerHTML=(D.trades||[]).map((t,i)=>
  `<tr><td>${i+1}</td><td class="${t.side==='Buy'?'pos':'neg'}">${t.side}</td>
   <td>${(t.open||'').slice(5,16)}</td><td>${(t.close||'').slice(5,16)}</td>
   <td>${t.entry.toFixed(2)}</td>
   <td class="${t.pnl>=0?'pos':'neg'}">${t.pnl>=0?'+':''}${t.pnl.toFixed(2)}</td>
   <td>${t.hold_min}</td></tr>`).join('');
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
