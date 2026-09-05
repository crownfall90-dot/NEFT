"""Сколько трения выдержит перевес: цена контракта, проскальзывание, лимиты.

Винрейт 52.5% против порога 46% выглядит большим запасом, но порог зависит
от цены входа. Если реально покупать не по 0.46, а дороже — запас тает.

    python scripts/updown_costs.py --days 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from scripts.updown_reversion import prep, trade
from scripts.updown_data import load

CFG = dict(mom_col="mom10", thr=3.0, use_pos=False)


def sim(outcomes, price, stake_pct, start=1000.0, max_trades=None):
    bal, mult = start, 1 / price - 1
    peak, dd = start, 0.0
    for k, o in enumerate(outcomes):
        if max_trades and k >= max_trades:
            break
        st = bal * stake_pct / 100
        bal += st * mult if o == "win" else (-st if o == "loss" else -st * 0.5)
        peak = max(peak, bal)
        dd = min(dd, (bal - peak) / peak * 100)
        if bal < start * 0.01:
            return 0.0, dd, k + 1
    return bal / start, dd, len(outcomes)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    d = prep(df)
    sig = trade(d, **CFG)
    outs = sig.outcome.tolist()
    w = outs.count("win"); l = outs.count("loss")
    wr = w / (w + l) * 100
    print(f"винрейт: {wr:.2f}%  на {len(outs)} сделках\n")

    print("─" * 70)
    print("1. ЧУВСТВИТЕЛЬНОСТЬ К ЦЕНЕ КОНТРАКТА")
    print("   (цена = порог безубытка; 0.46 заявлено, но книга двигается)")
    for p in (0.44, 0.46, 0.48, 0.50, 0.52, 0.525):
        be = p * 100
        x, dd, _ = sim(outs, p, 3.0, max_trades=300)
        edge = wr - be
        print(f"   цена {p:.3f} → порог {be:.1f}%  перевес {edge:+5.2f} п.п.  "
              f"300 сделок: ×{x:.3f}  {'✓' if edge > 0 else '✗ УБЫТОК'}")
    print(f"\n   критическая цена (где перевес исчезает): {wr/100:.4f}")
    print(f"   запас по цене: {wr/100 - 0.46:.4f} ({(wr/100-0.46)/0.46*100:.1f}%)")

    print("\n2. РАЗМЕР СТАВКИ (правило NEFT 0.5–3%)")
    for stake in (0.5, 1.0, 2.0, 3.0):
        for nt in (100, 300):
            x, dd, _ = sim(outs, 0.46, stake, max_trades=nt)
            print(f"   ставка {stake}%, {nt} сделок: ×{x:.3f}  просадка {dd:.1f}%")

    print("\n3. СКОЛЬКО СДЕЛОК НУЖНО ДЛЯ ×1.8–2.0 (ставка 3%, цена 0.46)")
    for target in (1.8, 2.0):
        bal, mult, k = 1000.0, 1 / 0.46 - 1, 0
        for o in outs:
            st = bal * 0.03
            bal += st * mult if o == "win" else (-st if o == "loss" else -st * .5)
            k += 1
            if bal >= 1000 * target:
                break
        days = k / 97.1
        print(f"   ×{target}: {k} сделок ≈ {days:.1f} суток непрерывной торговли")

    print("\n4. ХУДШИЙ СЛУЧАЙ — просадки на реальной последовательности")
    x, dd, _ = sim(outs, 0.46, 3.0)
    losses_run, cur = 0, 0
    for o in outs:
        cur = cur + 1 if o == "loss" else 0
        losses_run = max(losses_run, cur)
    print(f"   максимальная серия поражений подряд: {losses_run}")
    print(f"   при ставке 3% это просадка: "
          f"{(1 - 0.97**losses_run)*100:.1f}% от депозита")
    print(f"   максимальная просадка на всей истории: {dd:.1f}%")

    print("\n5. ПРОВЕРКА НА СЛУЧАЙНОСТЬ (перемешиваем исходы)")
    rng = np.random.default_rng(42)
    arr = np.array([1 if o == "win" else 0 for o in outs])
    real_x, _, _ = sim(outs, 0.46, 3.0, max_trades=300)
    sims = []
    for _ in range(1000):
        sh = rng.permutation(arr)
        o2 = ["win" if v else "loss" for v in sh]
        xx, _, _ = sim(o2, 0.46, 3.0, max_trades=300)
        sims.append(xx)
    sims = np.array(sims)
    print(f"   реальный порядок, 300 сделок: ×{real_x:.3f}")
    print(f"   перемешанный: медиана ×{np.median(sims):.3f}, "
          f"5–95%: ×{np.percentile(sims,5):.3f}…×{np.percentile(sims,95):.3f}")
    print("   (перемешивание сохраняет винрейт — проверяем только влияние порядка)")

    print("\n6. БУТСТРЭП ВИНРЕЙТА (устойчив ли сам перевес)")
    boots = [rng.choice(arr, len(arr), replace=True).mean() * 100
             for _ in range(2000)]
    boots = np.array(boots)
    print(f"   винрейт: {wr:.2f}%")
    print(f"   95% бутстрэп-интервал: {np.percentile(boots,2.5):.2f}% … "
          f"{np.percentile(boots,97.5):.2f}%")
    print(f"   доля выборок ниже порога 46%: {(boots < 46).mean()*100:.2f}%")
    print(f"   доля выборок ниже 50%: {(boots < 50).mean()*100:.2f}%")


if __name__ == "__main__":
    main()
