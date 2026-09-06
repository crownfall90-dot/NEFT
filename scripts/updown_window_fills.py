"""Исполнимость и цены В ОКНЕ 19-22 UTC против остального времени.

Собранные данные (logs/longwatch.jsonl) покрывают день и целевое окно.
Отвечаем на решающий вопрос: можно ли в окне купить контракт достаточно
дёшево, чтобы винрейт 58.04% давал прибыль.

Порог безубытка тейкера при цене P: (P + 2%*min(P,1-P)*0.9) * 100

    python scripts/updown_window_fills.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

SNAP = Path(__file__).resolve().parents[1] / "logs" / "longwatch.jsonl"
WR_WINDOW = 58.04      # винрейт стратегии в окне 19-22 UTC
WR_BASE = 52.33        # круглосуточно


def taker_fee(p: float, disc: float = 0.10) -> float:
    return 0.02 * min(p, 1 - p) * (1 - disc)


def breakeven_taker(p: float) -> float:
    return (p + taker_fee(p)) * 100


def breakeven_maker(p: float) -> float:
    return (p - taker_fee(p) * 0.25) * 100


def load() -> pd.DataFrame:
    rows = []
    with SNAP.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    d = pd.DataFrame(rows)
    d["t"] = pd.to_datetime(d.t)
    return d


def main() -> None:
    if not SNAP.exists():
        print("нет данных")
        return
    d = load()
    print(f"снимков: {len(d)}")
    print(f"период: {d.t.min()} -> {d.t.max()} UTC")
    print(f"часы покрыты: {sorted(d.hour.unique())}\n")

    inw = d[d.in_window]
    out = d[~d.in_window]
    print(f"в окне 19-22 UTC: {len(inw)} снимков")
    print(f"вне окна:         {len(out)} снимков\n")

    if inw.empty:
        print("ОКНО НЕ ПОКРЫТО — сбор не дожил до 19:00 UTC")
        return

    print("=" * 76)
    print("1. ЦЕНЫ: В ОКНЕ ПРОТИВ ОСТАЛЬНОГО ВРЕМЕНИ")
    print("=" * 76)
    print(f"{'':22} {'в окне':>12} {'вне окна':>12}")
    for col, lbl in [("best_ask", "лучший ask"), ("best_bid", "лучший bid")]:
        a = inw[col].dropna()
        b = out[col].dropna()
        print(f"   {lbl+' (медиана)':<20} {a.median():>11.3f} "
              f"{(b.median() if len(b) else float('nan')):>11.3f}")
        print(f"   {lbl+' (мин)':<20} {a.min():>11.3f} "
              f"{(b.min() if len(b) else float('nan')):>11.3f}")
    sp_in = (inw.best_ask - inw.best_bid).dropna()
    sp_out = (out.best_ask - out.best_bid).dropna()
    print(f"   {'спред (медиана)':<20} {sp_in.median():>11.3f} "
          f"{(sp_out.median() if len(sp_out) else float('nan')):>11.3f}")

    print("\n" + "=" * 76)
    print("2. ИСПОЛНИМОСТЬ: ДОЛЯ СНИМКОВ С ДОСТУПНОЙ ЦЕНОЙ")
    print("=" * 76)
    print(f"{'уровень':>10} {'в окне':>22} {'вне окна':>22}")
    for lv, col in [(0.50, "avail_050"), (0.52, "avail_052"), (0.55, "avail_055")]:
        ai = (inw[col] > 0).mean()
        ao = (out[col] > 0).mean() if len(out) else float("nan")
        mi = inw.loc[inw[col] > 0, col].median() if (inw[col] > 0).any() else 0
        mo = out.loc[out[col] > 0, col].median() if len(out) and (out[col] > 0).any() else 0
        print(f"   <= {lv:.2f}  {ai:>9.0%} (медиана ${mi:>6.0f})  "
              f"{ao:>9.0%} (медиана ${mo:>6.0f})")

    print("\n" + "=" * 76)
    print("3. ПО ЧАСАМ (UTC): цена и доступность")
    print("=" * 76)
    print(f"{'час':>5} {'мск':>5} {'снимков':>8} {'ask медиана':>12} "
          f"{'спред':>7} {'есть <=0.52':>12}")
    for h, g in d.groupby("hour"):
        mark = " <-- окно" if 19 <= h < 22 else ""
        print(f"   {h:02d} {((h+3)%24):>4} {len(g):>8} {g.best_ask.median():>11.3f} "
              f"{(g.best_ask-g.best_bid).median():>7.3f} "
              f"{(g.avail_052>0).mean():>11.0%}{mark}")

    print("\n" + "=" * 76)
    print("4. ГЛАВНОЕ: ХВАТАЕТ ЛИ ПЕРЕВЕСА ПРИ НАБЛЮДАЕМЫХ ЦЕНАХ")
    print("=" * 76)
    ask_in = inw.best_ask.dropna()
    for q, lbl in [(0.10, "10% лучших цен"), (0.25, "25% лучших"),
                   (0.50, "медиана")]:
        p = float(ask_in.quantile(q))
        be_t = breakeven_taker(p)
        be_m = breakeven_maker(p)
        print(f"   {lbl:<16} ask={p:.3f}  "
              f"тейкер порог {be_t:.2f}% перевес {WR_WINDOW-be_t:+.2f}  "
              f"мейкер порог {be_m:.2f}% перевес {WR_WINDOW-be_m:+.2f}")

    # максимальная цена, при которой винрейт окна ещё в плюсе
    lo, hi = 0.30, 0.80
    for _ in range(60):
        mid = (lo + hi) / 2
        if breakeven_taker(mid) < WR_WINDOW:
            lo = mid
        else:
            hi = mid
    print(f"\n   критическая цена тейкера при винрейте {WR_WINDOW}%: {lo:.4f}")
    share = (ask_in <= lo).mean()
    print(f"   доля снимков в окне с ask <= {lo:.3f}: {share:.0%}")

    print("\n" + "=" * 76)
    print("5. ВЫВОД")
    print("=" * 76)
    med = float(ask_in.median())
    be = breakeven_taker(med)
    if share > 0.5:
        print(f"   Цена в окне позволяет торговать тейкером в {share:.0%} моментов.")
    elif share > 0.1:
        print(f"   Торговать тейкером можно лишь в {share:.0%} моментов — "
              f"нужен отбор по цене.")
    else:
        print(f"   Тейкером почти никогда: только {share:.0%} снимков.")
    print(f"   Медианный ask в окне {med:.3f} -> порог {be:.2f}%, "
          f"винрейт {WR_WINDOW}% -> перевес {WR_WINDOW-be:+.2f} п.п.")


if __name__ == "__main__":
    main()
