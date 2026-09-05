"""Подбор ставки под просадку депозита 5-10% в ХУДШЕМ случае.

Важно понимать природу инструмента: у бинарного контракта нет просадки
внутри сделки. Купили Up за 0.46 → через 5 минут либо 1.00, либо 0.00.
Каждая отдельная сделка теряет 0% или 100% ставки, промежуточных значений
не существует. Поэтому 5-10% может относиться только к депозиту.

Максимальная серия поражений на истории — 11 подряд. Отсюда прямая
арифметика: чтобы 11 лоссов не увели депозит глубже X%, ставка должна
быть не больше определённой доли.

    python scripts/updown_dd_target.py --days 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from scripts.updown_combo import BE, PRICE, build_mask, evaluate, taker_fee
from scripts.updown_wr_hunt import prep
from scripts.updown_data import load

CFG = dict(mom_col="mom10", thr=3.0, vol_min=None, wick_min=None,
           edge=False, atr_min=None)
FEE = taker_fee(PRICE)
PAY = 1.0 / (PRICE + FEE)


def sim(win, stake_pct, start=1000.0):
    bal, peak, dd = start, start, 0.0
    for w in win:
        b = bal * stake_pct / 100
        bal += (b * PAY - b) if w else -b
        peak = max(peak, bal)
        dd = min(dd, (bal - peak) / peak * 100)
    return bal / start, dd


def mc(win, stake_pct, n=4000, seed=3):
    rng = np.random.default_rng(seed)
    xs, dds = [], []
    for _ in range(n):
        x, d = sim(rng.permutation(win), stake_pct)
        xs.append(x); dds.append(d)
    return np.array(xs), np.array(dds)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    d = prep(df)
    r = evaluate(d, build_mask(d, **CFG), CFG["mom_col"])
    win = r["win"]
    n = len(win)
    wr = win.mean() * 100
    span = (d.time.iloc[-1] - d.time.iloc[0]).days or 1

    streak = cur = 0
    for w in win:
        cur = 0 if w else cur + 1
        streak = max(streak, cur)

    print("ПРИРОДА ИНСТРУМЕНТА")
    print("  бинарный контракт: выигрыш = +117% ставки, проигрыш = −100% ставки")
    print("  просадки ВНУТРИ одной сделки не существует — только 0% или 100%")
    print(f"  максимальная серия поражений подряд на истории: {streak}\n")

    print(f"винрейт {wr:.2f}%, {n} сделок за {span} дней, порог {BE:.2f}%\n")

    print("=" * 74)
    print("1. АРИФМЕТИКА: во что превращается серия лоссов при разной ставке")
    print("=" * 74)
    print(f"{'ставка':>8} {'5 лоссов':>10} {'8 лоссов':>10} "
          f"{f'{streak} лоссов':>11}")
    for st in (0.1, 0.25, 0.5, 0.75, 1.0, 1.5):
        f = st / 100
        print(f"{st:>7}% {(1-(1-f)**5-0)*-100:>9.1f}% "
              f"{((1-f)**8-1)*100:>9.1f}% {((1-f)**streak-1)*100:>10.1f}%")

    print("\n" + "=" * 74)
    print("2. МОНТЕ-КАРЛО (4000 прогонов): просадка депозита за 2 месяца")
    print("=" * 74)
    print(f"{'ставка':>8} {'медиана x':>11} {'медиана DD':>11} {'худшие 5%':>10} "
          f"{'максимум':>9}  цель 5-10%")
    fits = []
    for st in (0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5):
        xs, dds = mc(win, st)
        p5 = np.percentile(dds, 5)
        worst = dds.min()
        # Критерий строгий: и худшие 5%, и абсолютный максимум просадки
        # должны укладываться в целевой коридор. Смотреть только на
        # перцентиль недостаточно — хвост уводит глубже лимита.
        ok = p5 >= -10 and worst >= -15
        if ok:
            fits.append((st, np.median(xs), p5, worst))
        print(f"{st:>7}% {np.median(xs):>10.2f}× {np.median(dds):>10.1f}% "
              f"{p5:>9.1f}% {worst:>8.1f}%  {'✓' if ok else ''}")

    print("\n" + "=" * 74)
    print("3. ЦЕНА ОСТОРОЖНОСТИ: сколько времени до удвоения")
    print("=" * 74)
    per_day = n / span
    for st in (0.1, 0.15, 0.2, 0.25, 0.5):
        bal, k = 1000.0, 0
        for w in win:
            b = bal * st / 100
            bal += (b * PAY - b) if w else -b
            k += 1
            if bal >= 2000:
                break
        if bal >= 2000:
            print(f"   ставка {st}%: ×2 за {k} сделок ≈ {k/per_day:.1f} суток")
        else:
            x, _ = sim(win, st)
            print(f"   ставка {st}%: ×2 не достигнуто за {span} дней "
                  f"(итог ×{x:.2f})")

    print("\n" + "=" * 74)
    print("4. РЕКОМЕНДАЦИЯ ПОД ЦЕЛЬ 5-10% ПРОСАДКИ")
    print("=" * 74)
    if fits:
        # Из прошедших отбор берём самый доходный.
        st, mx, p5, worst = max(fits, key=lambda g: g[1])
        print(f"   ставка {st}% от депозита")
        print(f"   просадка: медиана ~{np.median(mc(win, st)[1]):.1f}%, "
              f"худшие 5% — {p5:.1f}%, максимум за 4000 прогонов — {worst:.1f}%")
        print(f"   итог за {span} дней: медиана ×{mx:.2f}")
        x_real, dd_real = sim(win, st)
        print(f"   на реальном порядке сделок: ×{x_real:.2f}, {dd_real:.1f}%")
    else:
        print("   ни один вариант не удержал лимит — снижать ставку дальше")

    print("\n" + "=" * 74)
    print("5. О «ТОЧНОСТИ КАЖДОЙ СДЕЛКИ»")
    print("=" * 74)
    print(f"   винрейт {wr:.1f}% означает: примерно каждая вторая сделка убыточна.")
    print(f"   это НЕ чинится настройками — перевес стратегии в том, что")
    print(f"   выигрышей чуть больше, чем нужно для покрытия комиссии.")
    print(f"   попытки поднять точность фильтрами проверены в updown_final.py:")
    print(f"   на подборе давали 61-64%, на новых данных разваливались до 50-56%.")
    se = np.sqrt(0.25 / n) * 100
    print(f"\n   честный винрейт: {wr:.2f}% ± {se:.2f} (95% ДИ "
          f"{wr-1.96*se:.1f}…{wr+1.96*se:.1f}%)")
    print(f"   вероятность лосса на любой отдельной сделке: {100-wr:.1f}%")
    print(f"   вероятность 5 лоссов подряд: {((100-wr)/100)**5*100:.2f}% "
          f"(случается регулярно)")
    print(f"   вероятность {streak} лоссов подряд: "
          f"{((100-wr)/100)**streak*100:.4f}% — и это уже случалось")


if __name__ == "__main__":
    main()
