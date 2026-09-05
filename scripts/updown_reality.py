"""Реалити-чек: устойчив ли зазор между винрейтом ~51% и порогом 46%.

Ключевые вопросы:
 1. Не артефакт ли ×N из-за сложного процента на тысячах сделок.
 2. Держится ли винрейт помесячно, а не только в среднем.
 3. Что делает с результатом реалистичная задержка входа.
 4. Сколько сделок физически влезает в сутки при экспирации 5 минут.

    python scripts/updown_reality.py --days 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from scripts.updown_reversion import BE, PRICE, prep, trade
from scripts.updown_data import load

CFG = dict(mom_col="mom10", thr=3.0, use_pos=False)


def bankroll(outcomes, stake_pct=3.0, start=1000.0, cap_trades=None):
    bal, mult = start, 1 / PRICE - 1
    curve = [bal]
    for k, o in enumerate(outcomes):
        if cap_trades and k >= cap_trades:
            break
        st = bal * stake_pct / 100
        bal += st * mult if o == "win" else (-st if o == "loss" else -st * 0.5)
        curve.append(bal)
    return bal / start, curve


def wr_of(sig):
    w = int((sig.outcome == "win").sum()); l = int((sig.outcome == "loss").sum())
    return (w / (w + l) * 100 if w + l else 0.0), w, l


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    d = prep(df)
    sig = trade(d, **CFG)
    wr, w, l = wr_of(sig)
    n = w + l
    se = np.sqrt(0.25 / n) * 100

    print(f"конфиг: {CFG}")
    print(f"период: {d.time.iloc[0]} → {d.time.iloc[-1]}\n")
    print(f"сделок: {len(sig)}   винрейт: {wr:.2f}%  ({w}W/{l}L)")
    print(f"порог б/у: {BE}%   перевес: {wr - BE:+.2f} п.п.")
    print(f"95% ДИ винрейта: {wr - 1.96*se:.2f}% … {wr + 1.96*se:.2f}%")
    print(f"{'ДИ целиком выше порога' if wr - 1.96*se > BE else 'ДИ ЗАДЕВАЕТ порог'}\n")

    print("─" * 66)
    print("1. ЧАСТОТА СДЕЛОК — влезают ли они в реальное время")
    days_span = (d.time.iloc[-1] - d.time.iloc[0]).days or 1
    print(f"   {len(sig)} сделок за {days_span} дней = {len(sig)/days_span:.1f} в сутки")
    gaps = sig.i.diff().dropna()
    print(f"   медианный интервал между входами: {gaps.median():.0f} мин")
    overlap = (gaps < 5).sum()
    print(f"   входов раньше, чем закрылся предыдущий контракт: {overlap}")

    print("\n2. СЛОЖНЫЙ ПРОЦЕНТ — откуда берутся огромные иксы")
    x_all, curve = bankroll(sig.outcome.tolist())
    print(f"   все {len(sig)} сделок подряд, ставка 3%: ×{x_all:,.1f}")
    for cap in (50, 100, 200, 500):
        x, _ = bankroll(sig.outcome.tolist(), cap_trades=cap)
        print(f"   первые {cap:4} сделок: ×{x:.3f}")
    print("   → большие иксы = сложный процент на тысячах сделок, не качество сигнала")

    print("\n3. ПОМЕСЯЧНО (винрейт должен держаться, а не скакать)")
    s = sig.copy()
    s["month"] = pd.to_datetime(s.time).dt.to_period("M")
    for m, g in s.groupby("month"):
        r, gw, gl = wr_of(g)
        gse = np.sqrt(0.25 / max(gw + gl, 1)) * 100
        x, _ = bankroll(g.outcome.tolist())
        print(f"   {m}  {r:5.2f}% ±{gse:.2f}  ({gw}W/{gl}L)  ×{x:.2f}  "
              f"{'✓' if r > BE else '✗'}")

    print("\n4. ПОНЕДЕЛЬНО")
    s["week"] = pd.to_datetime(s.time).dt.isocalendar().week
    bad = 0
    for wk, g in s.groupby("week"):
        r, gw, gl = wr_of(g)
        if gw + gl < 20:
            continue
        x, _ = bankroll(g.outcome.tolist())
        if x < 1:
            bad += 1
        print(f"   неделя {wk}  {r:5.2f}%  ({gw}W/{gl}L)  ×{x:.2f}  "
              f"{'✓' if x > 1 else '✗'}")
    print(f"   убыточных недель: {bad}")

    print("\n5. ЗАДЕРЖКА ВХОДА (сигнал на close, покупка не мгновенна)")
    px = d.close.to_numpy()
    for lag in (0, 1, 2, 3):
        idx = sig.i.to_numpy() + lag
        ok = idx + 5 < len(px)
        e, x_ = px[idx[ok]], px[idx[ok] + 5]
        up = (sig.side.to_numpy()[ok] == "Up")
        win = np.where(up, x_ > e, x_ < e)
        r = win.mean() * 100
        outs = ["win" if v else "loss" for v in win]
        xx, _ = bankroll(outs)
        print(f"   +{lag} мин: {r:5.2f}%  ×{xx:,.1f}  "
              f"{'✓' if r > BE else '✗'}")

    print("\n6. ЧУВСТВИТЕЛЬНОСТЬ ПОРОГА")
    for thr in (2.0, 2.5, 3.0, 3.5, 4.0):
        s2 = trade(d, mom_col=CFG["mom_col"], thr=thr, use_pos=False)
        r, gw, gl = wr_of(s2)
        print(f"   thr={thr}: {r:5.2f}%  (n={len(s2)})")


if __name__ == "__main__":
    main()
