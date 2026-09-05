"""Сверка: даёт ли модуль updown_fade те же цифры, что исследовательские скрипты.

    python scripts/updown_verify.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from neft.strategies.updown_fade import UpDownFade
from scripts.updown_fees import breakeven_wr
from scripts.updown_reversion import prep, trade
from scripts.updown_data import load


def main() -> None:
    df = load("BTCUSDT", days=60)
    df = df[df.time.dt.dayofweek < 5].reset_index(drop=True)

    # эталон из исследовательского скрипта
    d_ref = prep(df)
    ref = trade(d_ref, mom_col="mom10", thr=3.0, use_pos=False)
    rw = int((ref.outcome == "win").sum()); rl = int((ref.outcome == "loss").sum())
    ref_wr = rw / (rw + rl) * 100

    # модуль
    s = UpDownFade(mom_bars=10, mom_threshold=3.0)
    s.prepare(df)
    sig = s.signals()
    mw = int((sig.outcome == "win").sum()); ml = int((sig.outcome == "loss").sum())
    mod_wr = mw / (mw + ml) * 100

    print(f"эталон (updown_reversion): {len(ref):5} сделок, винрейт {ref_wr:.2f}%")
    print(f"модуль (UpDownFade):       {len(sig):5} сделок, винрейт {mod_wr:.2f}%")
    print(f"расхождение: {abs(len(ref)-len(sig))} сделок, "
          f"{abs(ref_wr-mod_wr):.2f} п.п.")
    # ±5 сделок — разный прогрев (эталон жёстко warm=65), винрейт должен совпасть
    ok = abs(len(ref) - len(sig)) <= 5 and abs(ref_wr - mod_wr) < 0.5
    print("СВЕРКА ПРОЙДЕНА" if ok else "РАСХОЖДЕНИЕ — проверить логику")

    n = mw + ml
    se = np.sqrt(0.25 / n) * 100
    be = breakeven_wr(0.46, taker=True)
    print(f"\nвинрейт {mod_wr:.2f}% ± {se:.2f}  порог с комиссией {be:.2f}%")
    print(f"перевес {mod_wr-be:+.2f} п.п. = {(mod_wr-be)/se:.1f}σ")
    print(f"отсев: weak={s.skipped_weak} extreme={s.skipped_extreme} "
          f"pos={s.skipped_pos}")


if __name__ == "__main__":
    main()
