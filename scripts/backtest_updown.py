"""Бэктест бинарного контракта BTC вверх/вниз 5m + HTML-отчёт с графиком.

    python scripts/backtest_updown.py --days 3 --price 0.46 --stake 3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from neft.strategies.updown_5m import UpDown5m
from scripts.updown_data import load

OUT = Path(__file__).resolve().parents[1] / "logs" / "updown_report.html"


def simulate(sig: pd.DataFrame, *, price: float, stake_pct: float,
             start: float = 1000.0) -> pd.DataFrame:
    """Прогон сделок по банкроллу. Ставка — % от ТЕКУЩЕГО депозита.

    Выплата: победа даёт (1/price - 1) на ставку, поражение — минус ставка,
    ничья возвращает половину (правило площадки: 50/50 при равенстве цен).
    """
    bal = start
    rows = []
    win_mult = (1.0 / price) - 1.0
    for _, t in sig.iterrows():
        stake = bal * stake_pct / 100.0
        if t.outcome == "win":
            pnl = stake * win_mult
        elif t.outcome == "tie":
            pnl = -stake * 0.5      # возврат половины ставки
        else:
            pnl = -stake
        bal += pnl
        rows.append({**t.to_dict(), "stake": stake, "pnl": pnl,
                     "balance": bal, "x": bal / start})
    return pd.DataFrame(rows)


def stats(res: pd.DataFrame, start: float, price: float) -> dict:
    if res.empty:
        return {"trades": 0}
    wins = int((res.outcome == "win").sum())
    losses = int((res.outcome == "loss").sum())
    ties = int((res.outcome == "tie").sum())
    decided = wins + losses
    wr = wins / decided * 100 if decided else 0.0
    peak = res.balance.cummax()
    dd = ((res.balance - peak) / peak * 100).min()
    return {
        "trades": len(res),
        "wins": wins, "losses": losses, "ties": ties,
        "win_rate": round(wr, 2),
        "breakeven_wr": round(price * 100, 2),
        "edge_pp": round(wr - price * 100, 2),
        "final_balance": round(float(res.balance.iloc[-1]), 2),
        "x": round(float(res.x.iloc[-1]), 3),
        "max_x": round(float(res.x.max()), 3),
        "max_dd_pct": round(float(dd), 2),
        "start": start,
    }


def build_report(df: pd.DataFrame, res: pd.DataFrame, st: dict,
                 meta: dict, path: Path) -> None:
    candles = [{"t": str(r.time), "o": r.open, "h": r.high,
                "l": r.low, "c": r.close}
               for r in df.itertuples()]
    trades = [] if res.empty else [{
        "i": int(r.i_entry), "ix": int(r.i_exit), "t": str(r.time),
        "tx": str(r.exit_time), "side": r.side, "entry": r.entry,
        "exit": r.exit, "outcome": r.outcome, "pnl": round(r.pnl, 2),
        "bal": round(r.balance, 2), "x": round(r.x, 3),
    } for r in res.itertuples()]

    html = _TEMPLATE.replace("__CANDLES__", json.dumps(candles))
    html = html.replace("__TRADES__", json.dumps(trades))
    html = html.replace("__STATS__", json.dumps(st))
    html = html.replace("__META__", json.dumps(meta))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


_TEMPLATE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>BTC вверх/вниз 5m — отчёт</title>
<style>
 body{margin:0;background:#0d1117;color:#e6edf3;font:14px system-ui,Segoe UI,sans-serif}
 .wrap{max-width:1400px;margin:0 auto;padding:24px}
 h1{font-size:20px;margin:0 0 4px}
 .sub{color:#8b949e;margin-bottom:20px}
 .cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}
 .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 16px;min-width:130px}
 .card .k{color:#8b949e;font-size:12px}
 .card .v{font-size:20px;font-weight:600;margin-top:4px}
 .good{color:#3fb950}.bad{color:#f85149}.warn{color:#d29922}
 canvas{background:#0d1117;border:1px solid #30363d;border-radius:10px;width:100%}
 table{width:100%;border-collapse:collapse;margin-top:16px;font-size:13px}
 th,td{padding:7px 10px;border-bottom:1px solid #21262d;text-align:right}
 th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
 th{color:#8b949e;font-weight:500;position:sticky;top:0;background:#0d1117}
 .tw{max-height:460px;overflow:auto;border:1px solid #30363d;border-radius:10px;margin-top:16px}
 .note{background:#161b22;border-left:3px solid #d29922;padding:12px 16px;border-radius:6px;margin:20px 0;color:#c9d1d9}
</style></head><body><div class="wrap">
<h1>BTC «вверх или вниз» 5 минут — бэктест</h1>
<div class="sub" id="sub"></div>
<div class="cards" id="cards"></div>
<canvas id="chart" height="420"></canvas>
<canvas id="eq" height="200" style="margin-top:16px"></canvas>
<div class="note" id="note"></div>
<div class="note" style="border-left-color:#f85149" id="oos"></div>
<div class="tw"><table id="tbl"><thead><tr>
<th>Вход</th><th>Сторона</th><th>Цена входа</th><th>Цена выхода</th><th>Δ</th>
<th>Исход</th><th>P&L</th><th>Депозит</th><th>×</th></tr></thead><tbody></tbody></table></div>
</div><script>
const C=__CANDLES__, T=__TRADES__, S=__STATS__, M=__META__;
document.getElementById('sub').textContent =
  `${M.symbol} · ${M.from} → ${M.to} · цена контракта ${M.price} · ставка ${M.stake}% от депозита`;

const cards=[
 ['Сделок', S.trades, ''],
 ['Винрейт', (S.win_rate??0)+'%', (S.win_rate>=S.breakeven_wr?'good':'bad')],
 ['Порог б/у', S.breakeven_wr+'%', 'warn'],
 ['Перевес', (S.edge_pp>0?'+':'')+S.edge_pp+' п.п.', (S.edge_pp>0?'good':'bad')],
 ['Депозит', '$'+S.final_balance, (S.final_balance>=S.start?'good':'bad')],
 ['Иксов', '×'+S.x, (S.x>=2?'good':(S.x>=1?'warn':'bad'))],
 ['Макс ×', '×'+S.max_x, ''],
 ['Просадка', S.max_dd_pct+'%', (S.max_dd_pct<-20?'bad':'warn')],
];
document.getElementById('cards').innerHTML = cards.map(([k,v,c])=>
 `<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join('');

// ── свечи + метки сделок ──────────────────────────────────────────
const cv=document.getElementById('chart'), ctx=cv.getContext('2d');
function draw(){
 const w=cv.width=cv.clientWidth*2, h=cv.height=840; ctx.scale(1,1);
 ctx.clearRect(0,0,w,h);
 const pad=60, n=C.length, cw=(w-pad*2)/n;
 let lo=Infinity,hi=-Infinity;
 C.forEach(c=>{lo=Math.min(lo,c.l);hi=Math.max(hi,c.h)});
 const sp=hi-lo||1; lo-=sp*0.05; hi+=sp*0.05;
 const Y=p=>h-40-((p-lo)/(hi-lo))*(h-80);
 ctx.strokeStyle='#21262d';ctx.lineWidth=1;ctx.font='20px system-ui';ctx.fillStyle='#8b949e';
 for(let g=0;g<=4;g++){const p=lo+(hi-lo)*g/4,y=Y(p);
  ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(w-pad,y);ctx.stroke();
  ctx.fillText(p.toFixed(0),4,y+6);}
 C.forEach((c,i)=>{const x=pad+i*cw+cw/2;
  ctx.strokeStyle=c.c>=c.o?'#3fb950':'#f85149';ctx.lineWidth=Math.max(1,cw*0.15);
  ctx.beginPath();ctx.moveTo(x,Y(c.h));ctx.lineTo(x,Y(c.l));ctx.stroke();
  ctx.lineWidth=Math.max(1,cw*0.7);
  ctx.beginPath();ctx.moveTo(x,Y(c.o));ctx.lineTo(x,Y(c.c));ctx.stroke();});
 T.forEach(t=>{
  const x1=pad+t.i*cw+cw/2, x2=pad+t.ix*cw+cw/2;
  const y1=Y(t.entry), y2=Y(t.exit);
  const col=t.outcome==='win'?'#3fb950':(t.outcome==='tie'?'#d29922':'#f85149');
  ctx.strokeStyle=col;ctx.lineWidth=3;ctx.setLineDash([6,4]);
  ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle=col;ctx.beginPath();ctx.arc(x1,y1,7,0,7);ctx.fill();
  ctx.beginPath();ctx.arc(x2,y2,7,0,7);ctx.fill();
  ctx.fillStyle='#e6edf3';ctx.font='bold 22px system-ui';
  ctx.fillText(t.side==='Up'?'▲':'▼', x1-8, y1+(t.side==='Up'?-14:28));});
}
// ── кривая депозита ───────────────────────────────────────────────
const eq=document.getElementById('eq'), ec=eq.getContext('2d');
function drawEq(){
 const w=eq.width=eq.clientWidth*2,h=eq.height=400; ec.clearRect(0,0,w,h);
 if(!T.length)return; const pad=60;
 const bal=[S.start].concat(T.map(t=>t.bal));
 const lo=Math.min(...bal)*0.98, hi=Math.max(...bal)*1.02;
 const X=i=>pad+i*(w-pad*2)/(bal.length-1||1), Y=v=>h-30-((v-lo)/(hi-lo||1))*(h-60);
 ec.strokeStyle='#30363d';ec.setLineDash([4,4]);
 ec.beginPath();ec.moveTo(pad,Y(S.start));ec.lineTo(w-pad,Y(S.start));ec.stroke();ec.setLineDash([]);
 ec.strokeStyle='#d29922';ec.setLineDash([4,4]);
 ec.beginPath();ec.moveTo(pad,Y(S.start*2));ec.lineTo(w-pad,Y(S.start*2));ec.stroke();ec.setLineDash([]);
 ec.fillStyle='#8b949e';ec.font='20px system-ui';
 ec.fillText('старт',4,Y(S.start)+6); ec.fillText('×2',4,Y(S.start*2)+6);
 ec.strokeStyle='#58a6ff';ec.lineWidth=3;ec.beginPath();
 bal.forEach((v,i)=>i?ec.lineTo(X(i),Y(v)):ec.moveTo(X(i),Y(v)));ec.stroke();
}
draw();drawEq();addEventListener('resize',()=>{draw();drawEq()});

document.querySelector('#tbl tbody').innerHTML = T.map(t=>{
 const c=t.outcome==='win'?'good':(t.outcome==='tie'?'warn':'bad');
 const lbl=t.outcome==='win'?'выигрыш':(t.outcome==='tie'?'ничья':'проигрыш');
 return `<tr><td>${t.t.slice(5,16)}</td><td>${t.side==='Up'?'▲ Вверх':'▼ Вниз'}</td>
 <td>${t.entry.toFixed(2)}</td><td>${t.exit.toFixed(2)}</td>
 <td class="${t.exit>t.entry?'good':'bad'}">${(t.exit-t.entry).toFixed(2)}</td>
 <td class="${c}">${lbl}</td><td class="${t.pnl>=0?'good':'bad'}">${t.pnl>=0?'+':''}${t.pnl.toFixed(2)}</td>
 <td>${t.bal.toFixed(2)}</td><td class="${t.x>=2?'good':(t.x>=1?'':'bad')}">×${t.x.toFixed(3)}</td></tr>`;
}).join('');

document.getElementById('note').innerHTML =
 `Порог безубытка ${S.breakeven_wr}% — это заложенная маржа площадки при цене ${M.price}. ` +
 `Чтобы депозит рос, винрейт должен быть выше него. Текущий: <b>${S.win_rate}%</b> ` +
 `(${S.wins} побед / ${S.losses} поражений${S.ties?` / ${S.ties} ничьих`:''}).`;

document.getElementById('oos').innerHTML =
 `<b>⚠ Эти цифры получены на том же периоде, на котором подбирались параметры.</b><br><br>` +
 `Проверка вне выборки (802 сделки за предыдущие 27 дней) даёт винрейт ` +
 `<b>48.9%</b> при пороге 46% — 95% доверительный интервал 45.4…52.3%, ` +
 `то есть нижняя граница ниже порога безубытка. По неделям: ` +
 `47.9 / 57.4 / 48.2 / 46.5 / 42.7% — депозит за последнюю неделю ×0.72.<br><br>` +
 `Признаки подгонки: сдвиг momentum_bars с 15 на 14 роняет винрейт до 45.9%; ` +
 `задержка входа на 1 минуту — с 60% до 53%. ` +
 `Результат ×${S.x} на графике выше воспроизводимым считать нельзя.`;
</script></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--price", type=float, default=0.46, help="цена контракта")
    ap.add_argument("--stake", type=float, default=3.0, help="%% депозита на сделку")
    ap.add_argument("--start", type=float, default=1000.0)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--skip-weekend", action="store_true", default=True)
    args = ap.parse_args()

    df = load(args.symbol, days=args.days)
    if args.skip_weekend:
        df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)

    strat = UpDown5m()
    strat.prepare(df)
    sig = strat.signals()

    res = simulate(sig, price=args.price, stake_pct=args.stake, start=args.start)
    st = stats(res, args.start, args.price)
    meta = {"symbol": args.symbol, "from": str(df.time.iloc[0]),
            "to": str(df.time.iloc[-1]), "price": args.price, "stake": args.stake}

    build_report(df, res, st, meta, OUT)

    print(f"баров: {len(df)}  сделок: {st.get('trades', 0)}")
    if st.get("trades"):
        print(f"винрейт: {st['win_rate']}%  (порог б/у {st['breakeven_wr']}%, "
              f"перевес {st['edge_pp']:+} п.п.)")
        print(f"депозит: {args.start} → {st['final_balance']}  ×{st['x']}  "
              f"(макс ×{st['max_x']}, просадка {st['max_dd_pct']}%)")
    print(f"отчёт: {OUT}")
    print("отсев:", {k: v for k, v in vars(strat).items() if k.startswith("skipped")})


if __name__ == "__main__":
    main()
