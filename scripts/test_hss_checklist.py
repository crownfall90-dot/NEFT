"""Проверка чеклиста HSS на синтетике — без живого рынка.

    python scripts/test_hss_checklist.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from neft.core.indicators import add_features
from neft.core.models import Side
from neft.core.strategy import Bar
from neft.strategies.scalp_ha import ScalpHA


def _bars(n: int, start: float = 100.0, step: float = 0.2) -> pd.DataFrame:
    rows = []
    px = start
    t0 = pd.Timestamp("2026-01-05 16:00")
    for i in range(n):
        o, c = px, px + step
        rows.append(dict(
            time=t0 + pd.Timedelta(minutes=i),
            open=o, high=max(o, c) + 0.05, low=min(o, c) - 0.05, close=c,
            tick_volume=100, spread=1,
        ))
        px = c
    return pd.DataFrame(rows)


def _bar(df: pd.DataFrame, i: int) -> Bar:
    r = df.iloc[i]
    return Bar(time=r.time, open=float(r.open), high=float(r.high),
               low=float(r.low), close=float(r.close), spread_points=1, index=i)


def test_volume_window_three_and_same_size_counts():
    df = _bars(20)
    feat = add_features(df, ema_period=5, vol_window=3, vol_mode="min")
    size = feat["size"]
    for i in range(4, len(feat)):
        ref = float(min(size.iloc[i - 3], size.iloc[i - 2], size.iloc[i - 1]))
        got = feat.size_ref.iloc[i]
        assert abs(float(got) - ref) < 1e-12, (i, got, ref)
        want = float(size.iloc[i]) >= ref
        assert bool(feat.big_doji.iloc[i]) is want


def test_doji_needs_both_wicks():
    feat = add_features(_bars(30), ema_period=5)
    dojis = feat[feat.is_doji]
    if len(dojis):
        assert (dojis.top_wick_ratio > 0.02).all()
        assert (dojis.bot_wick_ratio > 0.02).all()
        assert (dojis.body_ratio <= 0.10).all()
    # Чистая медвежья без верхнего фитиля — не doji.
    clean = feat[feat.clean_bear & (feat.top_wick_ratio <= 0.02)]
    assert not bool(clean.is_doji.any())


def test_buy_setup_stop_at_doji_extreme():
    df = _bars(40)
    strat = ScalpHA(ema_period=5, pullback_bars=2, rr=1.0, session=None,
                    require_structure=False, block_after_small_doji=False,
                    min_sl_ratio=0.0, max_sl_ratio=1000.0, vol_window=3)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    ema_v = float(d.ema.iloc[i])
    d.loc[d.index[i], ["is_doji", "big_doji"]] = True, True
    d.loc[d.index[i], ["high", "low", "close"]] = 110.0, 104.0, ema_v + 1
    for j in (i - 2, i - 1):
        d.loc[d.index[j], ["clean_bear", "is_doji", "small_doji"]] = True, False, False
    sig = strat.on_bar(_bar(d, i), False)
    assert sig is not None
    assert sig.side is Side.BUY
    assert sig.entry_type == "stop"
    assert sig.entry == 110.0
    assert sig.sl is not None
    assert sig.tp is not None
    risk = float(sig.entry) - float(sig.sl)
    assert risk > 0
    assert abs((sig.tp - sig.entry) - risk * 1.0) < 1e-9
    assert sig.expire_bars == 3


def test_market_entry_uses_close_not_extreme():
    """entry_mode=market: объём считается от close, а не от разметки doji."""
    df = _bars(40)
    strat = ScalpHA(ema_period=5, pullback_bars=2, rr=1.5, session=None,
                    require_structure=False, block_after_small_doji=False,
                    min_sl_ratio=0.0, max_sl_ratio=1000.0,
                    entry_mode="market", tp_from_extreme=False)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    ema_v = float(d.ema.iloc[i])
    d.loc[d.index[i], ["is_doji", "big_doji"]] = True, True
    d.loc[d.index[i], ["high", "low", "close"]] = 110.0, 104.0, ema_v + 1
    for j in (i - 2, i - 1):
        d.loc[d.index[j], ["clean_bear", "is_doji", "small_doji"]] = True, False, False
    sig = strat.on_bar(_bar(d, i), False)
    assert sig is not None
    assert sig.entry is None          # market — цена входа не фиксируется
    real_entry = float(d.close.iloc[i])
    risk = real_entry - float(sig.sl)
    assert abs((sig.tp - real_entry) - risk * 1.5) < 1e-9


def test_one_clean_candle_is_not_a_pullback():
    df = _bars(40)
    strat = ScalpHA(ema_period=5, session=None, require_structure=False,
                    block_after_small_doji=False,
                    min_sl_ratio=0.0, max_sl_ratio=1000.0)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    ema_v = float(d.ema.iloc[i])
    d.loc[d.index[i], ["is_doji", "big_doji"]] = True, True
    d.loc[d.index[i], ["high", "low", "close"]] = 110.0, 100.0, ema_v + 1
    d.loc[d.index[i - 1], ["clean_bear", "is_doji", "small_doji"]] = True, False, False
    d.loc[d.index[i - 2], ["clean_bear", "is_doji", "small_doji"]] = False, False, False
    assert strat.on_bar(_bar(d, i), False) is None


def test_first_pullback_after_ema_is_structure_not_entry():
    df = _bars(40)
    strat = ScalpHA(ema_period=5, session=None, require_structure=True,
                    block_after_small_doji=False,
                    min_sl_ratio=0.0, max_sl_ratio=1000.0)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    ema_v = float(d.ema.iloc[i])
    d.loc[d.index[i], ["is_doji", "big_doji"]] = True, True
    d.loc[d.index[i], ["high", "low", "close"]] = 110.0, 104.0, ema_v + 1
    # Структура ещё не сложилась: ни одного завершённого отката после EMA.
    d.loc[d.index[i], ["bars_since_cross", "pullbacks_done"]] = 100, 0
    for j in (i - 2, i - 1):
        d.loc[d.index[j], ["clean_bear", "is_doji", "small_doji"]] = True, False, False
    assert strat.on_bar(_bar(d, i), False) is None
    assert strat.skipped_structure == 1


def test_small_doji_before_signal_kills_setup():
    df = _bars(40)
    strat = ScalpHA(ema_period=5, session=None, require_structure=False,
                    block_after_small_doji=True,
                    min_sl_ratio=0.0, max_sl_ratio=1000.0)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    ema_v = float(d.ema.iloc[i])
    d.loc[d.index[i], ["is_doji", "big_doji"]] = True, True
    d.loc[d.index[i], ["high", "low", "close"]] = 110.0, 104.0, ema_v + 1
    for j in range(i - 5, i):
        d.loc[d.index[j], ["clean_bear", "is_doji", "small_doji"]] = True, False, False
    d.loc[d.index[i - 3], "small_doji"] = True
    assert strat.on_bar(_bar(d, i), False) is None
    assert strat.skipped_small_doji == 1


def test_session_filter_skips_outside_kill_zone():
    df = _bars(40)
    strat = ScalpHA(ema_period=5, session=(16, 19), require_structure=False,
                    block_after_small_doji=False,
                    min_sl_ratio=0.0, max_sl_ratio=1000.0)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    d.loc[d.index[i], ["is_doji", "big_doji"]] = True, True
    r = d.iloc[i]
    outside = Bar(time=pd.Timestamp("2026-01-05 21:00"), open=float(r.open),
                  high=float(r.high), low=float(r.low), close=float(r.close),
                  spread_points=1, index=i)
    assert strat.on_bar(outside, False) is None
    assert strat.skipped_session == 1


def test_sl_range_filter_rejects_too_wide_stop():
    df = _bars(40)
    strat = ScalpHA(ema_period=5, pullback_bars=2, session=None,
                    require_structure=False, block_after_small_doji=False,
                    min_sl_ratio=0.0, max_sl_ratio=1.0)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    ema_v = float(d.ema.iloc[i])
    d.loc[d.index[i], ["is_doji", "big_doji"]] = True, True
    # Стоп заведомо шире max_sl (медианный размер бара здесь ~0.3).
    d.loc[d.index[i], ["high", "low", "close"]] = 200.0, 100.0, ema_v + 1
    for j in (i - 2, i - 1):
        d.loc[d.index[j], ["clean_bear", "is_doji", "small_doji"]] = True, False, False
    assert strat.on_bar(_bar(d, i), False) is None
    assert strat.skipped_sl_range == 1


def test_defaults_are_stop_entry():
    strat = ScalpHA()
    assert strat.entry_mode == "stop"
    assert strat.expire_bars == 3


if __name__ == "__main__":
    tests = [
        test_volume_window_three_and_same_size_counts,
        test_doji_needs_both_wicks,
        test_buy_setup_stop_at_doji_extreme,
        test_market_entry_uses_close_not_extreme,
        test_one_clean_candle_is_not_a_pullback,
        test_first_pullback_after_ema_is_structure_not_entry,
        test_small_doji_before_signal_kills_setup,
        test_session_filter_skips_outside_kill_zone,
        test_sl_range_filter_rejects_too_wide_stop,
        test_defaults_are_stop_entry,
    ]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
