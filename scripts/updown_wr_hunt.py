"""Поиск условий с максимальным винрейтом. Количество сделок вторично.

Новые правила (заданы под эту стратегию, правила NEFT не применяются):
  * приоритет — винрейт, а не размер иксов;
  * допустимая просадка 10–15%;
  * сделок может быть мало, если они качественные.

Метод: измеряем винрейт в разрезе одиночных условий на ПЕРВЫХ 70% данных,
последние 30% не трогаем (они для финальной проверки в другом скрипте).

    python scripts/updown_wr_hunt.py --days 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from neft.core.indicators import atr, ema
from scripts.updown_data import load

H = 5


def prep(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy().reset_index(drop=True)
    d["atr"] = atr(d, 14)
    a = d.atr.replace(0, np.nan)
    for n in (3, 5, 10, 15, 30, 60):
        d[f"mom{n}"] = (d.close - d.close.shift(n)) / a
    d["ema20"] = ema(d.close, 20)
    d["ema60"] = ema(d.close, 60)
    d["px_vs_ema20"] = (d.close - d.ema20) / a
    d["ema_gap"] = (d.ema20 - d.ema60) / a

    rng = (d.high - d.low).replace(0, np.nan)
    d["bar_pos"] = ((d.close - d.low) / rng).fillna(.5)
    d["body"] = ((d.close - d.open) / rng).fillna(0)
    d["upper_wick"] = ((d.high - d[["open", "close"]].max(axis=1)) / rng).fillna(0)
    d["lower_wick"] = ((d[["open", "close"]].min(axis=1) - d.low) / rng).fillna(0)

    d["atr_pct"] = d.atr / d.close * 100
    d["atr_z"] = (d.atr - d.atr.rolling(240).mean()) / d.atr.rolling(240).std()
    d["vol_z"] = (d.volume - d.volume.rolling(240).mean()) / d.volume.rolling(240).std()

    # расстояние до недавних экстремумов (в ATR) — «край» диапазона
    d["hi60"] = d.high.rolling(60).max()
    d["lo60"] = d.low.rolling(60).min()
    d["to_hi"] = (d.hi60 - d.close) / a
    d["to_lo"] = (d.close - d.lo60) / a
    d["range_pos"] = ((d.close - d.lo60) / (d.hi60 - d.lo60).replace(0, np.nan)).fillna(.5)

    up = (d.close > d.open).astype(int)
    d["run"] = (up.groupby((up != up.shift()).cumsum()).cumcount() + 1) * np.where(up == 1, 1, -1)

    d["hour"] = d.time.dt.hour
    d["fwd"] = d.close.shift(-H) - d.close
    return d


def fade_wr(d: pd.DataFrame, mask: pd.Series, mom_col: str) -> tuple[float, int]:
    """Винрейт ставки против импульса при заданном фильтре."""
    sub = d[mask & d.fwd.notna() & d[mom_col].notna()]
    if len(sub) < 60:
        return np.nan, len(sub)
    side_up = sub[mom_col] < 0          # падение → ставим Up
    win = np.where(side_up, sub.fwd > 0, sub.fwd < 0)
    return win.mean() * 100, len(sub)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    d_all = prep(df)
    cut = int(len(d_all) * 0.7)
    d = d_all.iloc[:cut].reset_index(drop=True)

    print(f"ПОИСК на {d.time.iloc[0]} → {d.time.iloc[-1]} ({len(d)} баров)")
    print(f"(последние 30% отложены на финальную проверку)\n")
    base, n0 = fade_wr(d, d.mom10.abs() >= 3.0, "mom10")
    print(f"базовая версия (|mom10|>=3.0): {base:.2f}%  n={n0}\n")

    print("─" * 74)
    print("1. ПОРОГ ИМПУЛЬСА — чем сильнее движение, тем надёжнее возврат?")
    for col in ("mom5", "mom10", "mom15", "mom30"):
        line = []
        for thr in (2.0, 3.0, 4.0, 5.0, 6.0):
            wr, n = fade_wr(d, d[col].abs() >= thr, col)
            line.append(f"{thr}:{wr:.1f}%(n={n})" if n >= 60 else f"{thr}:—")
        print(f"   {col:7} " + "  ".join(line))

    print("\n2. ПОЛОЖЕНИЕ В ДИАПАЗОНЕ (fade у края работает лучше?)")
    m = d.mom10.abs() >= 3.0
    for lo, hi, lbl in [(0, .2, "у нижнего края"), (.2, .4, "низ"),
                        (.4, .6, "середина"), (.6, .8, "верх"),
                        (.8, 1.01, "у верхнего края")]:
        wr, n = fade_wr(d, m & d.range_pos.between(lo, hi), "mom10")
        print(f"   {lbl:16} {wr:5.2f}%  n={n}")

    print("\n3. СОГЛАСОВАНИЕ: импульс против положения в диапазоне")
    # ставим Up (падение) когда цена внизу диапазона = двойной сигнал
    good = m & (((d.mom10 < 0) & (d.range_pos < .35)) |
                ((d.mom10 > 0) & (d.range_pos > .65)))
    bad = m & (((d.mom10 < 0) & (d.range_pos > .65)) |
               ((d.mom10 > 0) & (d.range_pos < .35)))
    for lbl, msk in [("согласовано (край диапазона)", good),
                     ("против (не край)", bad)]:
        wr, n = fade_wr(d, msk, "mom10")
        print(f"   {lbl:30} {wr:5.2f}%  n={n}")

    print("\n4. ВОЛАТИЛЬНОСТЬ (atr_z: всплеск или затишье)")
    for lo, hi, lbl in [(-9, -1, "низкая"), (-1, 0, "ниже средней"),
                        (0, 1, "выше средней"), (1, 2, "высокая"),
                        (2, 99, "экстремальная")]:
        wr, n = fade_wr(d, m & d.atr_z.between(lo, hi), "mom10")
        print(f"   {lbl:16} {wr:5.2f}%  n={n}")

    print("\n5. ОБЪЁМНЫЙ ВСПЛЕСК (vol_z)")
    for lo, hi, lbl in [(-9, 0, "ниже среднего"), (0, 1, "обычный"),
                        (1, 2, "повышенный"), (2, 99, "всплеск")]:
        wr, n = fade_wr(d, m & d.vol_z.between(lo, hi), "mom10")
        print(f"   {lbl:16} {wr:5.2f}%  n={n}")

    print("\n6. ФИТИЛЬ ПРОТИВ ДВИЖЕНИЯ (отбой уже виден на сигнальном баре)")
    rej_up = m & (d.mom10 > 0) & (d.upper_wick > .4)   # рост + верхний фитиль
    rej_dn = m & (d.mom10 < 0) & (d.lower_wick > .4)
    wr, n = fade_wr(d, rej_up | rej_dn, "mom10")
    print(f"   есть фитиль отбоя  {wr:5.2f}%  n={n}")
    wr, n = fade_wr(d, m & ~(rej_up | rej_dn), "mom10")
    print(f"   нет фитиля         {wr:5.2f}%  n={n}")

    print("\n7. ЧАСЫ (UTC) — где fade работает стабильнее")
    rows = []
    for h in range(24):
        wr, n = fade_wr(d, m & (d.hour == h), "mom10")
        if n >= 40:
            rows.append((h, wr, n))
    rows.sort(key=lambda r: -r[1])
    print("   лучшие:  " + "  ".join(f"{h:02d}:{w:.1f}%(n={n})" for h, w, n in rows[:6]))
    print("   худшие:  " + "  ".join(f"{h:02d}:{w:.1f}%(n={n})" for h, w, n in rows[-6:]))

    print("\n8. ДЛИНА СЕРИИ ОДНОНАПРАВЛЕННЫХ БАРОВ")
    for lo, hi, lbl in [(3, 4, "3 бара"), (4, 5, "4 бара"),
                        (5, 6, "5 баров"), (6, 99, "6+ баров")]:
        wr, n = fade_wr(d, m & (d.run.abs().between(lo, hi - 1)), "mom10")
        print(f"   {lbl:10} {wr:5.2f}%  n={n}")


if __name__ == "__main__":
    main()
