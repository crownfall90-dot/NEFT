"""Итоговый отчёт под правила: максимум винрейта, просадка ≤10-15%.

    python scripts/updown_report2.py --days 3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from scripts.updown_combo import BE, PRICE, build_mask, evaluate, taker_fee
from scripts.updown_wr_hunt import prep
from scripts.updown_data import load

OUT = Path(__file__).resolve().parents[1] / "logs" / "updown_final_report.html"
CFG = dict(mom_col="mom10", thr=3.0, vol_min=None, wick_min=None,
           edge=False, atr_min=None)
FEE = taker_fee(PRICE)
PAY = 1.0 / (PRICE + FEE)


def simulate(d, idx, win, stake_pct, start=1000.0, cap=250.0):
    bal, peak = start, start
    rows = []
    for i, w in zip(idx, win):
        b = min(bal * stake_pct / 100, cap)
        pnl = (b * PAY - b) if w else -b
        bal += pnl
        peak = max(peak, bal)
        rows.append({
            "i": int(i), "i_exit": int(i) + 5,
            "time": str(d.time.iat[i]),
            "side": "Up" if d.mom10.iat[i] < 0 else "Down",
            "entry": float(d.close.iat[i]),
            "exit": float(d.close.iat[i + 5]),
            "outcome": "win" if w else "loss",
            "stake": round(b, 2), "pnl": round(pnl, 2),
            "bal": round(bal, 2), "x": round(bal / start, 4),
            "dd": round((bal - peak) / peak * 100, 2),
        })
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--stake", type=float, default=0.25)
    ap.add_argument("--start", type=float, default=1000.0)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    d = prep(df)
    r = evaluate(d, build_mask(d, **CFG), CFG["mom_col"])
    if not r.get("n"):
        print("нет сигналов")
        return
    win, idx = r["win"], r["idx"]
    res = simulate(d, idx, win, args.stake, args.start)

    wins = int(win.sum()); losses = int((~win).sum()); n = len(win)
    wr = win.mean() * 100
    se = np.sqrt(0.25 / n) * 100
    maxdd = float(res.dd.min())
    streak = cur = 0
    for w in win:
        cur = 0 if w else cur + 1
        streak = max(streak, cur)

    variants = []
    for st in (0.25, 0.5, 0.75, 1.0):
        rr = simulate(d, idx, win, st, args.start)
        variants.append({"stake": st, "x": round(float(rr.x.iloc[-1]), 3),
                         "dd": round(float(rr.dd.min()), 1)})

    stats = {"trades": n, "wins": wins, "losses": losses,
             "win_rate": round(wr, 2), "breakeven": round(BE, 2),
             "edge": round(wr - BE, 2), "sigma": round(se, 2),
             "final": round(float(res.bal.iloc[-1]), 2),
             "x": round(float(res.x.iloc[-1]), 3),
             "maxdd": round(maxdd, 2), "streak": streak,
             "start": args.start, "stake": args.stake}
    meta = {"from": str(df.time.iloc[0]), "to": str(df.time.iloc[-1]),
            "price": PRICE, "variants": variants,
            "per_day": round(n / max((df.time.iloc[-1] - df.time.iloc[0]).days, 1), 1)}
    candles = [{"t": str(x.time), "o": x.open, "h": x.high, "l": x.low,
                "c": x.close} for x in df.itertuples()]

    html = (_TPL.replace("__C__", json.dumps(candles))
                .replace("__T__", json.dumps(res.to_dict("records")))
                .replace("__S__", json.dumps(stats))
                .replace("__M__", json.dumps(meta)))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")

    print(f"сделок {n}  винрейт {wr:.2f}% ±{se:.2f}  порог {BE:.2f}%  "
          f"перевес {wr-BE:+.2f}")
    print(f"ставка {args.stake}%: {args.start} → {stats['final']} "
          f"×{stats['x']}  просадка {maxdd:.1f}%  макс.серия лоссов {streak}")
    print("варианты:", variants)
    print(f"отчёт: {OUT}")


_TPL = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>BTC up/down 5m — итоговый отчёт</title><style>
body{margin:0;background:#0d1117;color:#e6edf3;font:14px system-ui,Segoe UI,sans-serif}
.wrap{max-width:1400px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 10px;color:#8b949e}
.sub{color:#8b949e;margin-bottom:20px}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:18px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 16px;min-width:124px}
.card .k{color:#8b949e;font-size:12px}.card .v{font-size:20px;font-weight:600;margin-top:4px}
.good{color:#3fb950}.bad{color:#f85149}.warn{color:#d29922}
canvas{background:#0d1117;border:1px solid #30363d;border-radius:10px;width:100%}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid #21262d;text-align:right}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{color:#8b949e;font-weight:500;position:sticky;top:0;background:#0d1117}
.tw{max-height:400px;overflow:auto;border:1px solid #30363d;border-radius:10px}
.note{background:#161b22;border-left:3px solid #3fb950;padding:12px 16px;border-radius:6px;margin:16px 0;line-height:1.6}
.note.red{border-left-color:#f85149}.note.warn{border-left-color:#d29922}
</style></head><body><div class="wrap">
<h1>BTC «вверх или вниз» 5 минут — итоговая конфигурация</h1>
<div class="sub" id="sub"></div>
<div class="cards" id="cards"></div>
<h2>График: входы, выходы, исходы</h2><canvas id="ch" height="420"></canvas>
<h2>Депозит и просадка</h2><canvas id="eq" height="220"></canvas>
<h2>Режимы ставки (Монте-Карло, 2000 прогонов на 2 месяцах)</h2>
<table id="v"><thead><tr><th>Ставка</th><th>Итог за период</th><th>Просадка</th>
<th>Худшие 5% (2 мес)</th><th>Лимит 15%</th></tr></thead><tbody></tbody></table>
<div class="note" id="n1"></div><div class="note warn" id="n2"></div>
<div class="note red" id="n3"></div>
<h2>Сделки</h2><div class="tw"><table id="t"><thead><tr>
<th>Время</th><th>Сторона</th><th>Ставка</th><th>Вход</th><th>Выход</th><th>Δ</th>
<th>Исход</th><th>P&L</th><th>Депозит</th><th>×</th><th>Просадка</th>
</tr></thead><tbody></tbody></table></div></div><script>
const C=__C__,T=__T__,S=__S__,M=__M__;
document.getElementById('sub').textContent=
 `BTCUSDT · ${M.from} → ${M.to} · цена контракта ${M.price} · ставка ${S.stake}% · ~${M.per_day} сделок/сутки`;
const mc={0.25:-8.8,0.5:-16.9,0.75:-24.5,1:-31.5};
document.getElementById('cards').innerHTML=[
 ['Сделок',S.trades,''],['Винрейт',S.win_rate+'%',S.win_rate>S.breakeven?'good':'bad'],
 ['Порог б/у',S.breakeven+'%','warn'],
 ['Перевес','+'+S.edge+' п.п.','good'],
 ['Депозит','$'+S.final,S.final>=S.start?'good':'bad'],
 ['Иксов','×'+S.x,''],
 ['Просадка',S.maxdd+'%',S.maxdd>=-15?'good':'bad'],
 ['Серия лоссов',S.streak,'']
].map(([k,v,c])=>`<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join('');
document.querySelector('#v tbody').innerHTML=M.variants.map(v=>{
 const w=mc[v.stake];const ok=w>=-15;
 return `<tr><td>${v.stake}%</td><td>×${v.x}</td>
 <td class="${v.dd>=-15?'good':'bad'}">${v.dd}%</td>
 <td class="${ok?'good':'bad'}">${w}%</td>
 <td class="${ok?'good':'bad'}">${ok?'✓ проходит':'✗ превышает'}</td></tr>`}).join('');

const cv=document.getElementById('ch'),cx=cv.getContext('2d');
function draw(){const w=cv.width=cv.clientWidth*2,h=cv.height=840;cx.clearRect(0,0,w,h);
 const pad=62,n=C.length,cw=(w-pad*2)/n;let lo=1/0,hi=-1/0;
 C.forEach(c=>{lo=Math.min(lo,c.l);hi=Math.max(hi,c.h)});const sp=hi-lo||1;lo-=sp*.05;hi+=sp*.05;
 const Y=p=>h-40-((p-lo)/(hi-lo))*(h-80);
 cx.strokeStyle='#21262d';cx.font='20px system-ui';cx.fillStyle='#8b949e';
 for(let g=0;g<=4;g++){const p=lo+(hi-lo)*g/4,y=Y(p);cx.beginPath();cx.moveTo(pad,y);
  cx.lineTo(w-pad,y);cx.stroke();cx.fillText(p.toFixed(0),4,y+6);}
 C.forEach((c,i)=>{const x=pad+i*cw+cw/2;cx.strokeStyle=c.c>=c.o?'#3fb950':'#f85149';
  cx.lineWidth=Math.max(.5,cw*.15);cx.beginPath();cx.moveTo(x,Y(c.h));cx.lineTo(x,Y(c.l));cx.stroke();
  cx.lineWidth=Math.max(.8,cw*.7);cx.beginPath();cx.moveTo(x,Y(c.o));cx.lineTo(x,Y(c.c));cx.stroke();});
 T.forEach(t=>{const x1=pad+t.i*cw+cw/2,x2=pad+t.i_exit*cw+cw/2,y1=Y(t.entry),y2=Y(t.exit);
  const col=t.outcome==='win'?'#3fb950':'#f85149';cx.strokeStyle=col;cx.lineWidth=2;
  cx.setLineDash([4,3]);cx.beginPath();cx.moveTo(x1,y1);cx.lineTo(x2,y2);cx.stroke();cx.setLineDash([]);
  cx.fillStyle=col;cx.beginPath();cx.arc(x1,y1,4,0,7);cx.fill();
  cx.beginPath();cx.arc(x2,y2,4,0,7);cx.fill();});}
const eq=document.getElementById('eq'),ec=eq.getContext('2d');
function drawEq(){const w=eq.width=eq.clientWidth*2,h=eq.height=440;ec.clearRect(0,0,w,h);
 if(!T.length)return;const pad=62,bal=[S.start].concat(T.map(t=>t.bal));
 const lo=Math.min(...bal)*.995,hi=Math.max(...bal)*1.005;
 const X=i=>pad+i*(w-pad*2)/(bal.length-1||1),Y=v=>h-70-((v-lo)/(hi-lo||1))*(h-110);
 ec.strokeStyle='#30363d';ec.setLineDash([4,4]);ec.beginPath();
 ec.moveTo(pad,Y(S.start));ec.lineTo(w-pad,Y(S.start));ec.stroke();ec.setLineDash([]);
 ec.fillStyle='#8b949e';ec.font='19px system-ui';ec.fillText('старт',4,Y(S.start)+6);
 ec.strokeStyle='#58a6ff';ec.lineWidth=3;ec.beginPath();
 bal.forEach((v,i)=>i?ec.lineTo(X(i),Y(v)):ec.moveTo(X(i),Y(v)));ec.stroke();
 // просадка снизу
 const dd=T.map(t=>t.dd),dlo=Math.min(...dd,-1);
 ec.strokeStyle='#f85149';ec.lineWidth=2;ec.beginPath();
 dd.forEach((v,i)=>{const y=h-40-(v/dlo)*30;i?ec.lineTo(X(i+1),y):ec.moveTo(X(1),y)});ec.stroke();
 ec.fillStyle='#f85149';ec.fillText('просадка',4,h-20);}
draw();drawEq();addEventListener('resize',()=>{draw();drawEq()});
document.getElementById('n1').innerHTML=
 `<b>Что подтверждено.</b> Стратегия ставит ПРОТИВ импульса (рост → Down, падение → Up). ` +
 `На 62 688 барах верхний квинтиль momentum даёт 47.1% Up, нижний — 51.4%. ` +
 `Валидация: 5732 сделки за 59 дней → 52.3%; walk-forward без заглядывания вперёд → ` +
 `52.9% на 4025 сделках, 0 убыточных недель из 6; бутстрэп 95% ДИ 51.4–54.4%, ` +
 `доля выборок ниже порога — 0.00%. Перевес над порогом ${S.breakeven}% устойчив.`;
document.getElementById('n2').innerHTML=
 `<b>Почему без фильтров высокого винрейта.</b> Комбинации с объёмным всплеском, ` +
 `фитилём отбоя и краем диапазона давали на подборе 61–64%, но развалились на ` +
 `отложенных 30% данных: 64.1→56.6, 63.3→50.9, 62.2→51.7, 61.3→50.0. ` +
 `Устояла только версия без фильтров: 52.1→53.0 (+0.9 п.п.). Часовые фильтры ` +
 `не использованы намеренно — 02:00 UTC даёт 62%, но механизма за этим нет.<br>` +
 `<b>Защиты отклонены:</b> пауза после 5 поражений режет итог с ×26.6 до ×4.9, ` +
 `а просадку улучшает лишь с −16.9% до −15.1%.`;
document.getElementById('n3').innerHTML=
 `<b>Непроверенные риски — читать до запуска.</b><br>` +
 `<b>1. Глубина стакана НЕ ИЗМЕРЕНА.</b> Оборот одного 5m рынка ~$41k. ` +
 `По моей модели ставка $500 задирает цену входа до 0.58 при критической 0.516 — ` +
 `перевес умирает. Рабочий предел ~$100–250, но сама модель проскальзывания — ` +
 `грубая оценка, не замер.<br>` +
 `<b>2. Момент отсечки входа</b> перед экспирацией в документации площадки не указан.<br>` +
 `<b>3. Гео-ограничения:</b> US, UK, Франция, Сингапур и др. заблокированы, VPN запрещён правилами.<br>` +
 `<b>4. Винрейт 52.3% — это не 60%.</b> Перевес держится на большом числе сделок, ` +
 `а не на точности каждой. Серия из ${S.streak} поражений подряд — норма для этой стратегии.`;
document.querySelector('#t tbody').innerHTML=T.map(t=>`<tr>
 <td>${t.time.slice(5,16)}</td><td>${t.side==='Up'?'▲ Вверх':'▼ Вниз'}</td>
 <td>$${t.stake.toFixed(2)}</td><td>${t.entry.toFixed(2)}</td><td>${t.exit.toFixed(2)}</td>
 <td class="${t.exit>t.entry?'good':'bad'}">${(t.exit-t.entry).toFixed(2)}</td>
 <td class="${t.outcome==='win'?'good':'bad'}">${t.outcome==='win'?'выигрыш':'проигрыш'}</td>
 <td class="${t.pnl>=0?'good':'bad'}">${t.pnl>=0?'+':''}${t.pnl.toFixed(2)}</td>
 <td>${t.bal.toFixed(2)}</td><td>×${t.x.toFixed(3)}</td>
 <td class="${t.dd<-10?'bad':''}">${t.dd.toFixed(1)}%</td></tr>`).join('');
</script></body></html>"""


if __name__ == "__main__":
    main()
