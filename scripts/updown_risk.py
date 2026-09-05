"""Выбор режима риска: удержать просадку в 10-15% в ХУДШЕМ случае, не в среднем.

Монте-Карло по порядку сделок показал: ставка 0.5% даёт медиану -11.8%,
но в худших 5% случаев уходит за -16.8%. Лимит должен держаться не в среднем,
а в плохом сценарии. Здесь ищем режим, у которого 95-й перцентиль просадки
укладывается в 15%.

Дополнительно проверяем защитные механики:
  * дневной стоп-лосс (пауза до конца суток после -X%);
  * пауза после серии поражений подряд.

    python scripts/updown_risk.py --days 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from scripts.updown_combo import BE, PRICE, build_mask, evaluate, taker_fee
from scripts.updown_wr_hunt import prep
from scripts.updown_data import load

CFG = dict(mom_col="mom10", thr=3.0, vol_min=None, wick_min=None,
           edge=False, atr_min=None)
FEE = taker_fee(PRICE)
PAY = 1.0 / (PRICE + FEE)          # сколько получаем на $1 ставки при выигрыше


def sim(win, days_idx, *, stake_pct=None, stake_abs=None,
        daily_stop=None, loss_streak_stop=None, start=1000.0):
    """Прогон с опциональными защитами. Возвращает (x, max_dd, кривая)."""
    bal, peak, dd = start, start, 0.0
    curve = [bal]
    cur_day, day_start, paused = None, start, False
    streak = 0
    for w, day in zip(win, days_idx):
        if day != cur_day:
            cur_day, day_start, paused = day, bal, False
            streak = 0
        if paused:
            curve.append(bal)
            continue
        if loss_streak_stop and streak >= loss_streak_stop:
            paused = True
            curve.append(bal)
            continue
        b = bal * stake_pct / 100 if stake_pct else min(stake_abs, bal)
        if b <= 0:
            break
        bal += (b * PAY if w else 0) - b
        streak = 0 if w else streak + 1
        if daily_stop and bal <= day_start * (1 - daily_stop / 100):
            paused = True
        peak = max(peak, bal)
        dd = min(dd, (bal - peak) / peak * 100)
        curve.append(bal)
    return bal / start, dd, curve


def mc(win, days_idx, n_sims=2000, **kw):
    """Монте-Карло по порядку сделок: распределение просадки и итога.

    Перемешиваем ИСХОДЫ, оставляя разметку дней на месте — иначе дневные
    счётчики (стоп по дню, серия поражений) обнуляются на каждой сделке
    и защиты перестают что-либо делать.
    """
    rng = np.random.default_rng(11)
    xs, dds = [], []
    for _ in range(n_sims):
        shuffled = rng.permutation(win)
        x, d_, _ = sim(shuffled, days_idx, **kw)
        xs.append(x); dds.append(d_)
    return np.array(xs), np.array(dds)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    d = prep(df)
    m = build_mask(d, **CFG)
    r = evaluate(d, m, CFG["mom_col"])
    win = r["win"]
    idx = r["idx"]
    days_idx = d.time.dt.date.to_numpy()[idx]
    n = len(win)
    wr = win.mean() * 100
    span = (d.time.iloc[-1] - d.time.iloc[0]).days or 1

    print(f"винрейт {wr:.2f}%  ({n} сделок, {n/span:.0f}/сутки)")
    print(f"порог {BE:.2f}%, перевес {wr-BE:+.2f} п.п.")
    print(f"выплата на $1 ставки при выигрыше: ${PAY:.4f}\n")

    print("=" * 76)
    print("1. КАКОЙ РЕЖИМ ДЕРЖИТ 15% В ХУДШЕМ СЛУЧАЕ (Монте-Карло, 2000 прогонов)")
    print("=" * 76)
    print(f"{'режим':22} {'медиана x':>11} {'медиана DD':>11} "
          f"{'DD 95%':>9} {'DD max':>9}  вердикт")
    modes = [
        ("0.25% compound", dict(stake_pct=0.25)),
        ("0.5% compound",  dict(stake_pct=0.5)),
        ("0.75% compound", dict(stake_pct=0.75)),
        ("$10 фикс",       dict(stake_abs=10)),
        ("$15 фикс",       dict(stake_abs=15)),
        ("$20 фикс",       dict(stake_abs=20)),
    ]
    good = []
    for name, kw in modes:
        xs, dds = mc(win, days_idx, **kw)
        p95 = np.percentile(dds, 5)      # 5-й перцентиль = худшие 5%
        ok = p95 >= -15
        if ok:
            good.append((name, kw, np.median(xs), p95))
        print(f"{name:22} {np.median(xs):>10.2f}× {np.median(dds):>10.1f}% "
              f"{p95:>8.1f}% {dds.min():>8.1f}%  {'✓' if ok else '✗'}")

    print("\n" + "=" * 76)
    print("2. ЗАЩИТЫ: дневной стоп и пауза после серии поражений")
    print("=" * 76)
    print(f"{'защита':30} {'медиана x':>11} {'DD 95%':>9} {'DD max':>9}")
    base = dict(stake_pct=0.5)
    for lbl, extra in [
        ("без защиты", {}),
        ("дневной стоп -5%", dict(daily_stop=5)),
        ("дневной стоп -8%", dict(daily_stop=8)),
        ("пауза после 5 лоссов", dict(loss_streak_stop=5)),
        ("пауза после 7 лоссов", dict(loss_streak_stop=7)),
        ("стоп -8% + пауза 7", dict(daily_stop=8, loss_streak_stop=7)),
    ]:
        xs, dds = mc(win, days_idx, **base, **extra)
        print(f"{lbl:30} {np.median(xs):>10.2f}× "
              f"{np.percentile(dds,5):>8.1f}% {dds.min():>8.1f}%")

    print("\n" + "=" * 76)
    print("3. ТО ЖЕ ДЛЯ БОЛЕЕ КРУПНОЙ СТАВКИ (1%) — спасают ли защиты")
    print("=" * 76)
    for lbl, extra in [
        ("без защиты", {}),
        ("дневной стоп -8%", dict(daily_stop=8)),
        ("стоп -8% + пауза 7", dict(daily_stop=8, loss_streak_stop=7)),
        ("стоп -5% + пауза 5", dict(daily_stop=5, loss_streak_stop=5)),
    ]:
        xs, dds = mc(win, days_idx, stake_pct=1.0, **extra)
        ok = np.percentile(dds, 5) >= -15
        print(f"{lbl:30} {np.median(xs):>10.2f}× "
              f"{np.percentile(dds,5):>8.1f}% {dds.min():>8.1f}%  "
              f"{'✓' if ok else '✗'}")

    print("\n" + "=" * 76)
    print("4. ИТОГОВАЯ РЕКОМЕНДАЦИЯ")
    print("=" * 76)
    if good:
        name, kw, med_x, p95 = max(good, key=lambda g: g[2])
        print(f"   режим: {name}")
        print(f"   медиана итога за {span} дней: ×{med_x:.2f}")
        print(f"   просадка в худших 5% случаев: {p95:.1f}% (лимит 15%)")
        x_real, dd_real, curve = sim(win, days_idx, **kw)
        print(f"   на реальном порядке сделок: ×{x_real:.2f}, просадка {dd_real:.1f}%")
        # время до ×2
        bal, k = 1000.0, 0
        for w in win:
            b = bal * kw.get("stake_pct", 0) / 100 if "stake_pct" in kw else kw["stake_abs"]
            bal += (b * PAY if w else 0) - b
            k += 1
            if bal >= 2000:
                break
        if bal >= 2000:
            print(f"   ×2 за {k} сделок ≈ {k/(n/span):.1f} суток")
    else:
        print("   ни один режим не удержал 15% в худших 5% — снижать ставку дальше")


if __name__ == "__main__":
    main()
