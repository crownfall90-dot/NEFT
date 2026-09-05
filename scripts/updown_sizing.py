"""Подбор сайзинга под лимит просадки 10–15% и максимальный винрейт.

Установлено (updown_final.py): фильтры высокого винрейта не переносятся
на новые данные, работает базовый fade без них — 52.9% на walk-forward.
Осталось подобрать размер ставки: просадка при 2% доходит до −38%, это
втрое выше лимита.

Проверяем три подхода:
  1. фиксированный процент от депозита (compounding);
  2. фиксированная ставка в $ (без compounding) — просадка не разгоняется;
  3. ограничение серии убытков (стоп после N поражений подряд).

    python scripts/updown_sizing.py --days 60
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


def sim_pct(win, stake_pct, start=1000.0):
    """Процент от текущего депозита — compounding."""
    bal, peak, dd = start, start, 0.0
    curve = [bal]
    for w in win:
        b = bal * stake_pct / 100
        bal += (b / (PRICE + FEE) if w else 0) - b
        peak = max(peak, bal); dd = min(dd, (bal - peak) / peak * 100)
        curve.append(bal)
    return bal / start, dd, curve


def sim_flat(win, stake_abs, start=1000.0):
    """Фиксированная ставка в $ — просадка растёт линейно, не экспоненциально."""
    bal, peak, dd = start, start, 0.0
    curve = [bal]
    for w in win:
        b = min(stake_abs, bal)
        if b <= 0:
            break
        bal += (b / (PRICE + FEE) if w else 0) - b
        peak = max(peak, bal); dd = min(dd, (bal - peak) / peak * 100)
        curve.append(bal)
    return bal / start, dd, curve


def sim_capped(win, stake_pct, cap_abs, start=1000.0):
    """Процент, но не выше потолка в $ (потолок = ликвидность рынка)."""
    bal, peak, dd = start, start, 0.0
    for w in win:
        b = min(bal * stake_pct / 100, cap_abs)
        if b <= 0:
            break
        bal += (b / (PRICE + FEE) if w else 0) - b
        peak = max(peak, bal); dd = min(dd, (bal - peak) / peak * 100)
    return bal / start, dd


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
    n = len(win)
    wr = win.mean() * 100
    days = (d.time.iloc[-1] - d.time.iloc[0]).days or 1

    print(f"базовый fade: винрейт {wr:.2f}%, {n} сделок за {days} дней "
          f"({n/days:.0f}/сутки)")
    print(f"порог безубытка {BE:.2f}%, перевес {wr-BE:+.2f} п.п.")
    run_loss, cur = 0, 0
    for w in win:
        cur = 0 if w else cur + 1
        run_loss = max(run_loss, cur)
    print(f"макс. серия поражений: {run_loss}\n")

    print("─" * 74)
    print("1. ПРОЦЕНТ ОТ ДЕПОЗИТА (compounding) — просадка разгоняется")
    print(f"{'ставка':>8} {'итог':>14} {'просадка':>10}  лимит 10-15%")
    for st in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0):
        x, dd, _ = sim_pct(win, st)
        ok = "✓" if dd >= -15 else "✗"
        print(f"{st:>7}% {x:>13,.2f}× {dd:>9.1f}%  {ok}")

    print("\n2. ФИКСИРОВАННАЯ СТАВКА В $ (депозит $1000)")
    print(f"{'ставка':>8} {'итог':>14} {'просадка':>10}  лимит 10-15%")
    for st in (5, 10, 15, 20, 30, 50):
        x, dd, _ = sim_flat(win, st)
        ok = "✓" if dd >= -15 else "✗"
        print(f"{'$'+str(st):>8} {x:>13,.2f}× {dd:>9.1f}%  {ok}")

    print("\n3. ПРОЦЕНТ С ПОТОЛКОМ (реалистично: ликвидность рынка ~$250)")
    print(f"{'ставка':>8} {'потолок':>9} {'итог':>12} {'просадка':>10}  лимит")
    for st in (1.0, 2.0, 3.0):
        for cap in (100, 250):
            x, dd = sim_capped(win, st, cap)
            ok = "✓" if dd >= -15 else "✗"
            print(f"{st:>7}% {'$'+str(cap):>9} {x:>11,.2f}× {dd:>9.1f}%  {ok}")

    print("\n4. РЕКОМЕНДУЕМЫЙ РЕЖИМ: ставка 0.5%, потолок $250")
    x, dd, curve = sim_pct(win, 0.5)
    xc, ddc = sim_capped(win, 0.5, 250)
    print(f"   без потолка: ×{x:,.2f}  просадка {dd:.1f}%")
    print(f"   с потолком:  ×{xc:,.2f}  просадка {ddc:.1f}%")
    per_month = n / days * 30
    print(f"   сделок в месяц: ~{per_month:.0f}")

    print("\n5. СКОЛЬКО ВРЕМЕНИ ДО УДВОЕНИЯ (ставка 0.5%, compounding)")
    bal, k = 1000.0, 0
    for w in win:
        b = bal * 0.005
        bal += (b / (PRICE + FEE) if w else 0) - b
        k += 1
        if bal >= 2000:
            break
    if bal >= 2000:
        print(f"   ×2 достигается за {k} сделок ≈ {k/(n/days):.1f} суток")
    else:
        print(f"   ×2 не достигнуто за {n} сделок (итог ×{bal/1000:.2f})")

    print("\n6. РАСПРЕДЕЛЕНИЕ ПРОСАДКИ (1000 перемешиваний порядка сделок)")
    rng = np.random.default_rng(7)
    for st in (0.5, 1.0, 2.0):
        dds = []
        for _ in range(1000):
            sh = rng.permutation(win)
            _, dd_i, _ = sim_pct(sh, st)
            dds.append(dd_i)
        dds = np.array(dds)
        print(f"   ставка {st}%: медиана {np.median(dds):.1f}%, "
              f"худшие 5% хуже {np.percentile(dds,5):.1f}%, "
              f"максимум {dds.min():.1f}%")


if __name__ == "__main__":
    main()
