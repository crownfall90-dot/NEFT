"""Итоговый HTML-отчёт: все протестированные варианты бота в одной таблице.

Сводит воедино всё исследование:
  * какие сигналы проверялись и что показали вне выборки,
  * реальные цены рынка из замеров API,
  * матрицу «сигнал x роль x цена» с EV каждого варианта,
  * проверку устойчивости и ограничение по исполнимости.

    python scripts/updown_final_report.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "logs" / "book_snapshots.jsonl"
MATRIX = ROOT / "logs" / "strategy_matrix.csv"
OUT = ROOT / "logs" / "updown_variants_report.html"

# Все проверенные подходы к предсказанию направления.
SIGNAL_RESEARCH = [
    ("Трендовый (momentum по тренду)", "48.9%", "42.7% на последней неделе",
     "Отклонён", "Ставил по движению; сканирование 62 688 баров показало, "
     "что верхний квинтиль momentum даёт 47.1% Up — логика была обратной"),
    ("Mean-reversion fade mom10", "52.3%", "52.9% walk-forward",
     "Принят", "Ставка против импульса. 0 убыточных недель из 9, "
     "бутстрэп 95% ДИ 51.4–54.4%"),
    ("Fade + объёмный всплеск", "62.2% (подбор)", "51.7%",
     "Отклонён", "Разница −10.6 п.п. вне выборки — подгонка"),
    ("Fade + фитиль отбоя", "64.1% (подбор)", "56.6%",
     "Отклонён", "Разница −7.5 п.п."),
    ("Fade + край диапазона", "63.3% (подбор)", "50.9%",
     "Отклонён", "Разница −12.4 п.п."),
    ("Фильтр по часам (02:00 UTC)", "62.0%", "не проверялся",
     "Отклонён", "502 наблюдения, механизма нет — заведомая подгонка"),
    ("Арбитраж Up+Down < 1.00", "—", "0 находок",
     "Невозможен", "Минимальная сумма ask 1.01 на 1073 снимках, "
     "5919 живых проверок — ноль"),
]

# Что даёт каждый вариант исполнения.
EXEC_NOTES = {
    "тейкер, медианный ask": "Покупка по рынку. Цена 0.55 — то, что реально "
                             "стоит в стакане",
    "тейкер, лучший ask (p5)": "Лучшая наблюдавшаяся цена покупки",
    "мейкер на bid": "Лимитник на 0.46. Прибылен, но за 35 снимков "
                     "активного окна не исполнился ни разу",
    "мейкер bid+1ц": "Чуть выше bid — исполняется чаще, перевес меньше",
    "мейкер 0.50": "Ближе к рынку; перевес уходит в минус после штрафа",
    "мейкер 0.52": "Предел прибыльности без учёта adverse selection",
}


def load_prices() -> dict:
    rows = []
    with SNAP.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    d = pd.DataFrame(rows)
    d["asset"] = d.slug.str.extract(r"^(bitcoin|ethereum|bnb)")[0]
    btc = d[d.asset == "bitcoin"]
    ask = btc.Up_ask.dropna()
    bid = btc.Up_bid.dropna()
    live_ask = ask[(ask > 0.20) & (ask < 0.80)]
    live_bid = bid[(bid > 0.20) & (bid < 0.80)]
    return {
        "snapshots": len(btc),
        "markets": int(btc.id.nunique()),
        "ask_med": float(live_ask.median()),
        "bid_med": float(live_bid.median()),
        "spread": float(live_ask.median() - live_bid.median()),
        "ask_min": float(live_ask.min()),
    }


def main() -> None:
    px = load_prices()
    mx = pd.read_csv(MATRIX, encoding="utf-8-sig")

    rows_signals = "".join(
        f"<tr><td>{n}</td><td class='num'>{ins}</td><td class='num'>{oos}</td>"
        f"<td class='{'ok' if v == 'Принят' else 'no'}'>{v}</td>"
        f"<td class='note'>{c}</td></tr>"
        for n, ins, oos, v, c in SIGNAL_RESEARCH)

    mx_conf = mx[mx["подтверждён"] == "да"].copy()
    rows_matrix = ""
    for r in mx_conf.itertuples():
        cls = "ok" if r.прибыльно else "no"
        note = EXEC_NOTES.get(r.вход, "")
        rows_matrix += (
            f"<tr><td>{r.сигнал}</td><td class='num'>{r.винрейт}%</td>"
            f"<td>{r.вход}</td><td class='num'>{r.цена}</td>"
            f"<td class='num'>{r.штраф_пп}</td>"
            f"<td class='num'>{r.порог_бу}%</td>"
            f"<td class='num {cls}'>{r.перевес_пп:+.2f}</td>"
            f"<td class='num {cls}'>{r.EV:+.2%}</td>"
            f"<td class='note'>{note}</td></tr>")

    good = mx_conf[mx_conf.прибыльно]
    html = _TPL
    for k, v in {
        "__SNAP__": f"{px['snapshots']:,}".replace(",", " "),
        "__MARKETS__": str(px["markets"]),
        "__ASK__": f"{px['ask_med']:.2f}",
        "__BID__": f"{px['bid_med']:.2f}",
        "__SPREAD__": f"{px['spread']:.2f}",
        "__SIGNALS__": rows_signals,
        "__MATRIX__": rows_matrix,
        "__NGOOD__": str(len(good)),
        "__NTOTAL__": str(len(mx_conf)),
    }.items():
        html = html.replace(k, v)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"снимков BTC: {px['snapshots']}, рынков: {px['markets']}")
    print(f"ask {px['ask_med']:.2f} / bid {px['bid_med']:.2f}, "
          f"спред {px['spread']:.2f}")
    print(f"вариантов с подтверждённым винрейтом: {len(mx_conf)}, "
          f"прибыльных: {len(good)}")
    print(f"отчёт: {OUT}")


_TPL = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>BTC up/down 5m — все протестированные варианты</title><style>
body{margin:0;background:#0d1117;color:#e6edf3;font:14px system-ui,Segoe UI,sans-serif}
.wrap{max-width:1500px;margin:0 auto;padding:26px}
h1{font-size:21px;margin:0 0 6px}h2{font-size:16px;margin:30px 0 12px;color:#8b949e}
.sub{color:#8b949e;margin-bottom:22px}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:8px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 16px;min-width:118px}
.card .k{color:#8b949e;font-size:12px}.card .v{font-size:19px;font-weight:600;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px;background:#0d1117}
th,td{padding:8px 10px;border-bottom:1px solid #21262d;text-align:left;vertical-align:top}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
th{color:#8b949e;font-weight:500;background:#161b22;position:sticky;top:0}
.ok{color:#3fb950}.no{color:#f85149}.warn{color:#d29922}
.note{color:#8b949e;font-size:12px;max-width:420px}
.box{background:#161b22;border-left:3px solid #d29922;padding:14px 18px;border-radius:6px;margin:18px 0;line-height:1.6}
.box.red{border-left-color:#f85149}.box.green{border-left-color:#3fb950}
.tw{border:1px solid #30363d;border-radius:10px;overflow:auto;max-height:640px}
</style></head><body><div class="wrap">
<h1>BTC «вверх или вниз» 5 минут — все протестированные варианты</h1>
<div class="sub">Оценка на реальных ценах predict.fun: __SNAP__ снимков стакана по __MARKETS__ рынкам</div>

<div class="cards">
<div class="card"><div class="k">Цена покупки (ask)</div><div class="v no">__ASK__</div></div>
<div class="card"><div class="k">Цена продажи (bid)</div><div class="v">__BID__</div></div>
<div class="card"><div class="k">Спред</div><div class="v warn">__SPREAD__</div></div>
<div class="card"><div class="k">Прибыльных вариантов</div><div class="v">__NGOOD__ из __NTOTAL__</div></div>
</div>

<div class="box red"><b>Ключевая поправка.</b> Весь ранний бэктест считался по цене 0.46,
взятой со страницы рынка. Замер API показал: 0.46 — это <b>bid</b>, цена продажи.
Покупатель платит <b>ask = __ASK__</b>. Это сдвигает порог безубытка с 46.8% до 55.8%
и делает покупку по рынку убыточной при любом из достигнутых винрейтов.</div>

<h2>1. Проверенные способы предсказать направление</h2>
<div class="tw"><table><thead><tr>
<th>Подход</th><th>На подборе</th><th>Вне выборки</th><th>Вердикт</th><th>Комментарий</th>
</tr></thead><tbody>__SIGNALS__</tbody></table></div>

<h2>2. Матрица: сигнал × способ входа × цена</h2>
<div class="sub">Штраф — поправка на adverse selection: лимитная заявка ниже рынка
исполняется преимущественно тогда, когда цена идёт против позиции.
Без этой поправки модель показывала прибыль даже для «монетки» с винрейтом 50%.</div>
<div class="tw"><table><thead><tr>
<th>Сигнал</th><th>Винрейт</th><th>Вход</th><th>Цена</th><th>Штраф п.п.</th>
<th>Порог б/у</th><th>Перевес</th><th>EV/сделка</th><th>Комментарий</th>
</tr></thead><tbody>__MATRIX__</tbody></table></div>

<h2>3. Устойчивость прибыльных вариантов</h2>
<div class="box"><b>Монте-Карло по двум неопределённостям сразу</b>
(винрейт 52.9% ±0.79, adverse selection ±60% от измеренного):<br>
медианный перевес <b>+2.43 п.п.</b>, 5-й перцентиль <b>−0.48 п.п.</b>,
доля прибыльных сценариев <b>90.1%</b>.<br><br>
Перевес устойчив к погрешности винрейта (положителен даже при 51%),
но чувствителен к adverse selection: при значении 0.03 вместо измеренных
0.0156 уходит в минус. Само значение измерено всего по 35 снимкам одного окна —
это самая слабая часть оценки.</div>

<h2>4. Ограничение, которое перевешивает всё</h2>
<div class="box red"><b>Исполнимость.</b> Прибыльны только мейкерские заявки
по 0.46–0.47. За 35 снимков активного пятиминутного окна ликвидность дешевле 0.52
отсутствовала полностью — доступный объём был <b>$0 во всех снимках</b>,
рыночный ask держался в диапазоне 0.57–0.68.<br><br>
Заявка на 0.46 при таком рынке не исполнится. Положительный EV на бумаге
не превращается в сделки: при доле исполнения 5% месячный рост составит 1.08×,
при 2% — 1.03×, а наблюдалось <b>0 исполнений из 35</b>.</div>

<div class="box green"><b>Что можно сделать дальше.</b>
Единственный непроверенный путь — более длинные горизонты (15 минут, сутки).
Там предсказуемость выше, чем на пяти минутах, где базовая доля роста 49.1%
близка к случайности, а спред относительно потенциальной прибыли меньше.
Инструменты замера в репозитории работают с любым рынком площадки.</div>

</div></body></html>"""


if __name__ == "__main__":
    main()
