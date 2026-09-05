"""Матрица вариантов бота — с поправкой на adverse selection у мейкера.

ОШИБКА ПЕРВОЙ ВЕРСИИ. Наивный расчёт показывал, что мейкер прибылен даже
с винрейтом 50% («монетка»). Это невозможно, и вот почему модель врала:
лимитная заявка на покупку по bid исполняется НЕ КОГДА УГОДНО, а когда
кто-то соглашается продать вам по этой цене. Он соглашается, когда цена
идёт вниз — то есть против вашей позиции. Это adverse selection: у
исполнившихся заявок винрейт систематически ХУЖЕ среднего.

Правильная модель: если ставим лимитник на d центов ниже рынка, то
исполняются преимущественно те случаи, когда рынок ушёл против нас.
Величину штрафа оцениваем из наблюдаемой волатильности цены контракта.

    python scripts/updown_matrix2.py
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
ACTIVE = ROOT / "logs" / "active_window.jsonl"
OUT_CSV = ROOT / "logs" / "strategy_matrix.csv"
OUT_MD = ROOT / "logs" / "strategy_matrix.md"

REBATE = 0.25


def taker_fee(p: float, disc: float = 0.10) -> float:
    return 0.02 * min(p, 1 - p) * (1 - disc)


def maker_credit(p: float) -> float:
    return taker_fee(p) * REBATE


def cost_of(price: float, role: str) -> float:
    return price + taker_fee(price) if role == "taker" else price - maker_credit(price)


def measure_adverse(path: Path) -> tuple[float, float]:
    """Оценка adverse selection из живого стакана активного окна.

    Смотрим: если цена в момент t была P, то насколько она смещается
    к следующему снимку. Заявка ниже рынка исполняется в тех случаях,
    когда движение пошло вниз — усредняем именно эти случаи.
    """
    if not path.exists():
        return 0.0, 0.0
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    if len(rows) < 5:
        return 0.0, 0.0
    mids = []
    for r in rows:
        asks = sorted(r.get("asks") or [], key=lambda x: x[0])
        bids = sorted(r.get("bids") or [], key=lambda x: -x[0])
        if asks and bids:
            mids.append((asks[0][0] + bids[0][0]) / 2)
    if len(mids) < 5:
        return 0.0, 0.0
    m = np.array(mids)
    steps = np.diff(m)
    down = steps[steps < 0]
    # средний размер движения вниз = насколько «уезжает» цена, когда нас исполняют
    return (float(np.abs(down).mean()) if len(down) else 0.0,
            float(np.std(steps)))


def load_book() -> pd.DataFrame:
    rows = []
    with SNAP.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    d = pd.DataFrame(rows)
    d["t"] = pd.to_datetime(d.t)
    d["asset"] = d.slug.str.extract(r"^(bitcoin|ethereum|bnb)")[0]
    return d


# Винрейты из исследования. in-sample помечен — он не переносится.
SIGNALS = [
    ("fade mom10 (walk-forward)", 52.9, True),
    ("fade mom10 (весь период)", 52.3, True),
    ("fade + фильтры (вне выборки)", 51.7, True),
    ("монетка 50%", 50.0, True),
    ("трендовый (старая версия)", 48.9, True),
    ("fade + фильтры (in-sample, НЕ переносится)", 62.2, False),
]


def main() -> None:
    d = load_book()
    btc = d[d.asset == "bitcoin"]
    up_ask = btc.Up_ask.dropna()
    up_bid = btc.Up_bid.dropna()
    # только «живые» уровни, без уже решённых рынков
    live_ask = up_ask[(up_ask > 0.20) & (up_ask < 0.80)]
    live_bid = up_bid[(up_bid > 0.20) & (up_bid < 0.80)]

    ask_med = float(live_ask.median())
    bid_med = float(live_bid.median())
    ask_min = float(live_ask.quantile(0.05))
    adverse, vol = measure_adverse(ACTIVE)

    print("=" * 104)
    print("ИСХОДНЫЕ ДАННЫЕ (из реальных замеров стакана)")
    print("=" * 104)
    print(f"   снимков BTC: {len(btc)}")
    print(f"   медианный ask (цена покупки): {ask_med:.3f}")
    print(f"   медианный bid (цена продажи): {bid_med:.3f}")
    print(f"   лучший ask (5-й перцентиль):  {ask_min:.3f}")
    print(f"   спред: {ask_med - bid_med:.3f}")
    print(f"\n   adverse selection (среднее движение цены вниз "
          f"между снимками): {adverse:.4f}")
    print(f"   волатильность цены контракта: {vol:.4f}")
    print("   (на столько в среднем уезжает цена, когда лимитник исполняют)")

    rows = []
    for sig, wr, confirmed in SIGNALS:
        variants = [
            ("тейкер, медианный ask", "taker", ask_med, 0.0),
            ("тейкер, лучший ask (p5)", "taker", ask_min, 0.0),
            ("мейкер на bid", "maker", bid_med, ask_med - bid_med),
            ("мейкер bid+1ц", "maker", round(bid_med + 0.01, 2),
             ask_med - bid_med - 0.01),
            ("мейкер 0.50", "maker", 0.50, max(0.0, ask_med - 0.50)),
            ("мейкер 0.52", "maker", 0.52, max(0.0, ask_med - 0.52)),
        ]
        for label, role, price, depth in variants:
            if not (0 < price < 1):
                continue
            cost = cost_of(price, role)
            # Штраф adverse selection: чем глубже под рынком стоит заявка,
            # тем сильнее отбор в пользу невыгодных исполнений.
            # Масштабируем наблюдаемым сносом цены.
            penalty = 0.0
            if role == "maker" and adverse > 0:
                penalty = min(depth / max(adverse, 1e-6), 3.0) * adverse * 100
            wr_eff = wr - penalty
            ev = (wr_eff / 100 - cost) / cost if cost > 0 else 0.0
            rows.append({
                "сигнал": sig, "подтверждён": "да" if confirmed else "НЕТ",
                "винрейт": wr, "вход": label, "роль": role,
                "цена": round(price, 3),
                "штраф_пп": round(penalty, 2),
                "винрейт_эфф": round(wr_eff, 2),
                "порог_бу": round(cost * 100, 2),
                "перевес_пп": round(wr_eff - cost * 100, 2),
                "EV": round(ev, 4),
                "прибыльно": ev > 0,
            })

    res = pd.DataFrame(rows)
    res.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    print("\n" + "=" * 104)
    print("ПОЛНАЯ МАТРИЦА")
    print("=" * 104)
    print(res.to_string(index=False))

    print("\n" + "=" * 104)
    print("ПРОВЕРКА МОДЕЛИ: «монетка 50%» должна быть убыточна везде")
    print("=" * 104)
    coin = res[res.сигнал.str.contains("монетка")]
    bad = coin[coin.прибыльно]
    if len(bad):
        print("   МОДЕЛЬ ВРЁТ — монетка прибыльна:")
        print(bad.to_string(index=False))
    else:
        print("   OK: монетка убыточна во всех вариантах, модель состоятельна")

    real = res[(res.подтверждён == "да") & res.прибыльно]
    print("\n" + "=" * 104)
    print("ПРИБЫЛЬНЫЕ ВАРИАНТЫ С ПОДТВЕРЖДЁННЫМ ВИНРЕЙТОМ")
    print("=" * 104)
    if len(real):
        print(real.sort_values("EV", ascending=False).to_string(index=False))
    else:
        print("   НЕТ НИ ОДНОГО.")

    # Markdown-отчёт
    with OUT_MD.open("w", encoding="utf-8") as f:
        f.write("# Матрица вариантов бота BTC up/down 5m\n\n")
        f.write("Оценка на реальных ценах стакана predict.fun "
                f"({len(btc)} снимков).\n\n")
        f.write(f"- медианный ask (покупка): **{ask_med:.3f}**\n")
        f.write(f"- медианный bid (продажа): **{bid_med:.3f}**\n")
        f.write(f"- спред: **{ask_med-bid_med:.3f}**\n")
        f.write(f"- adverse selection: **{adverse:.4f}** на исполнение\n\n")
        f.write(res.to_markdown(index=False))
        f.write("\n\n## Вывод\n\n")
        if len(real):
            f.write(f"Прибыльных вариантов: {len(real)}\n")
        else:
            f.write("Прибыльных вариантов с подтверждённым винрейтом нет.\n")
    print(f"\nCSV: {OUT_CSV}")
    print(f"MD:  {OUT_MD}")


if __name__ == "__main__":
    main()
