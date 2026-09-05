"""Устойчив ли найденный перевес мейкера, или он в пределах погрешности.

Матрица показала: мейкер на bid 0.46 даёт перевес +2.44 п.п. при
винрейте 52.9%. Но обе величины — оценки с погрешностью:
  * винрейт 52.9% +/- 0.79 (walk-forward, 4025 сделок);
  * adverse selection 0.0156 измерен всего по 35 снимкам одного окна.

Прогоняем перевес по обеим неопределённостям сразу и смотрим,
какая доля сценариев остаётся прибыльной.

    python scripts/updown_robustness.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

REBATE = 0.25
PRICE = 0.46          # мейкерский лимитник на bid
WR = 52.9             # walk-forward винрейт
WR_SE = 0.79          # его стандартная ошибка
ADVERSE = 0.0156      # измеренный снос цены
DEPTH = 0.09          # насколько заявка ниже ask (спред)


def taker_fee(p: float, disc: float = 0.10) -> float:
    return 0.02 * min(p, 1 - p) * (1 - disc)


def maker_cost(p: float) -> float:
    return p - taker_fee(p) * REBATE


def edge(wr: float, adverse: float, price: float = PRICE,
        depth: float = DEPTH) -> float:
    penalty = min(depth / max(adverse, 1e-6), 3.0) * adverse * 100
    return (wr - penalty) - maker_cost(price) * 100


def main() -> None:
    base = edge(WR, ADVERSE)
    print(f"базовый расчёт: перевес {base:+.2f} п.п. "
          f"(винрейт {WR}%, adverse {ADVERSE:.4f})\n")

    print("=" * 74)
    print("1. ЧУВСТВИТЕЛЬНОСТЬ К ADVERSE SELECTION")
    print("=" * 74)
    print("   (измерен по 35 снимкам одного окна - оценка очень грубая)")
    print(f"{'adverse':>10} {'штраф п.п.':>12} {'перевес':>10}  вердикт")
    for a in (0.005, 0.010, 0.0156, 0.020, 0.030, 0.045):
        e = edge(WR, a)
        pen = min(DEPTH / max(a, 1e-6), 3.0) * a * 100
        print(f"{a:>10.4f} {pen:>11.2f} {e:>+9.2f}  "
              f"{'прибыль' if e > 0 else 'УБЫТОК'}")

    print("\n" + "=" * 74)
    print("2. ЧУВСТВИТЕЛЬНОСТЬ К ВИНРЕЙТУ")
    print("=" * 74)
    print(f"{'винрейт':>9} {'перевес':>10}  вердикт")
    for w in (51.0, 52.0, 52.3, 52.9, 53.5, 54.4):
        e = edge(w, ADVERSE)
        print(f"{w:>9.1f} {e:>+9.2f}  {'прибыль' if e > 0 else 'УБЫТОК'}")

    print("\n" + "=" * 74)
    print("3. ОБЕ НЕОПРЕДЕЛЁННОСТИ ВМЕСТЕ (Монте-Карло, 20000 сценариев)")
    print("=" * 74)
    rng = np.random.default_rng(42)
    wr_s = rng.normal(WR, WR_SE, 20000)
    # adverse оценен грубо: разброс +/-60% от измеренного
    adv_s = rng.uniform(ADVERSE * 0.4, ADVERSE * 1.6, 20000)
    edges = np.array([edge(w, a) for w, a in zip(wr_s, adv_s)])
    print(f"   медианный перевес: {np.median(edges):+.2f} п.п.")
    print(f"   5-й перцентиль:    {np.percentile(edges, 5):+.2f} п.п.")
    print(f"   95-й перцентиль:   {np.percentile(edges, 95):+.2f} п.п.")
    print(f"   доля прибыльных сценариев: {(edges > 0).mean():.1%}")
    print(f"   доля убыточных:            {(edges <= 0).mean():.1%}")

    print("\n" + "=" * 74)
    print("4. ГЛАВНОЕ ОГРАНИЧЕНИЕ: ИСПОЛНИМОСТЬ")
    print("=" * 74)
    print("   Замер активного окна (35 снимков): ликвидность дешевле 0.52")
    print("   отсутствовала во ВСЕХ снимках. Заявка на 0.46 при рынке")
    print("   0.55-0.68 не исполнялась НИ РАЗУ.")
    print()
    print("   Даже если перевес положителен, сделок не будет.")
    print("   Оценим доходность при разной доле исполнения:")
    ev = edge(WR, ADVERSE) / 100 / maker_cost(PRICE)
    print(f"\n   EV на состоявшуюся сделку: {ev:+.2%}")
    print(f"{'исполнение':>12} {'сделок/сутки':>14} {'за месяц (ставка 1%)':>22}")
    for fr in (0.30, 0.10, 0.05, 0.02, 0.005):
        n = 97 * fr
        growth = (1 + 0.01 * ev) ** (n * 30)
        print(f"{fr:>11.1%} {n:>13.1f} {growth:>21.3f}x")
    print("\n   наблюдалось исполнений: 0 из 35 снимков (<2.9%)")


if __name__ == "__main__":
    main()
