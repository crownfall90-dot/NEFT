"""Walk-forward: самая честная проверка. Параметры берём только из прошлого.

Скользящее окно: обучаемся на N днях, торгуем следующий день, сдвигаемся.
Никакого заглядывания вперёд — именно так стратегия работала бы вживую.

    python scripts/updown_walkforward.py --days 60 --train 14
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from scripts.updown_fees import breakeven_wr, taker_fee
from scripts.updown_reversion import prep, trade
from scripts.updown_data import load

GRID = list(itertools.product(
    ["mom5", "mom10", "mom15", "mom30"],
    [2.0, 2.5, 3.0, 3.5],
))


def wr_of(sig: pd.DataFrame) -> tuple[float, int]:
    if sig.empty:
        return 0.0, 0
    w = int((sig.outcome == "win").sum())
    l = int((sig.outcome == "loss").sum())
    return (w / (w + l) * 100 if w + l else 0.0), w + l


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--train", type=int, default=14, help="дней в обучающем окне")
    ap.add_argument("--price", type=float, default=0.46)
    ap.add_argument("--stake", type=float, default=2.0)
    args = ap.parse_args()

    df = load("BTCUSDT", days=args.days)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)
    d = prep(df)
    d["date"] = d.time.dt.date
    dates = sorted(d.date.unique())
    be = breakeven_wr(args.price, taker=True)
    fee = taker_fee(args.price)

    print(f"walk-forward: обучение {args.train} дн. → торговля 1 день, сдвиг")
    print(f"порог безубытка с комиссией: {be:.2f}%")
    print(f"всего торговых дней: {len(dates)}\n")

    bal, start = 1000.0, 1000.0
    all_outs, rows = [], []
    peak, maxdd = bal, 0.0

    for k in range(args.train, len(dates)):
        tr_dates = dates[k - args.train:k]
        te_date = dates[k]
        tr = d[d.date.isin(tr_dates)].reset_index(drop=True)
        te = d[d.date == te_date].reset_index(drop=True)
        if len(te) < 100:
            continue

        # выбираем конфиг ТОЛЬКО по обучающему окну
        best, best_wr = None, -1
        for col, thr in GRID:
            s = trade(tr, mom_col=col, thr=thr, use_pos=False)
            r, n = wr_of(s)
            if n >= 100 and r > best_wr:
                best, best_wr = (col, thr), r
        if best is None:
            continue

        s_te = trade(te, mom_col=best[0], thr=best[1], use_pos=False)
        r_te, n_te = wr_of(s_te)
        if n_te == 0:
            continue

        day_start = bal
        for o in s_te.outcome:
            budget = bal * args.stake / 100
            shares = budget / (args.price + fee)
            bal += (shares if o == "win" else 0.0) - budget
            peak = max(peak, bal)
            maxdd = min(maxdd, (bal - peak) / peak * 100)
        all_outs.extend(s_te.outcome.tolist())

        rows.append({"date": te_date, "cfg": f"{best[0]}/{best[1]}",
                     "train_wr": round(best_wr, 2), "test_wr": round(r_te, 2),
                     "n": n_te, "day_x": round(bal / day_start, 3),
                     "bal": round(bal, 2)})

    res = pd.DataFrame(rows)
    if res.empty:
        print("недостаточно данных")
        return

    print(res.to_string(index=False))

    wr_all, n_all = (np.mean([o == "win" for o in all_outs]) * 100, len(all_outs))
    wins = sum(1 for o in all_outs if o == "win")
    losses = n_all - wins
    se = np.sqrt(0.25 / n_all) * 100

    print(f"\n{'='*70}")
    print(f"ИТОГ WALK-FORWARD (честный, без заглядывания вперёд)")
    print(f"{'='*70}")
    print(f"сделок: {n_all}   винрейт: {wr_all:.2f}%  ({wins}W/{losses}L)")
    print(f"порог с комиссией: {be:.2f}%   перевес: {wr_all - be:+.2f} п.п.")
    print(f"95% ДИ: {wr_all-1.96*se:.2f}% … {wr_all+1.96*se:.2f}%")
    print(f"депозит: ${start} → ${bal:.2f}  = ×{bal/start:.3f}")
    print(f"максимальная просадка: {maxdd:.1f}%")
    print(f"прибыльных дней: {(res.day_x > 1).sum()}/{len(res)}")
    print(f"средняя разница train→test: "
          f"{(res.test_wr - res.train_wr).mean():+.2f} п.п.")
    print(f"стабильность конфига: {res.cfg.nunique()} разных конфигов "
          f"за {len(res)} дней")
    print(f"  самый частый: {res.cfg.mode().iloc[0]} "
          f"({(res.cfg == res.cfg.mode().iloc[0]).sum()} дней)")


if __name__ == "__main__":
    main()
