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
                    require_structure=False, block_after_small_doji=True,
                    min_sl_ratio=0.0, max_sl_ratio=1000.0, vol_window=3)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    ema_v = float(d.ema.iloc[i])
    d.loc[d.index[i], ["is_doji", "big_doji", "ha_close"]] = True, True, ema_v + 1
    d.loc[d.index[i], ["high", "low", "close"]] = 110.0, 104.0, 105.0
    d.loc[d.index[i], ["ha_low", "ha_high"]] = ema_v + 0.05, ema_v + 1.5
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


def test_market_tp_rr_from_fill_with_spread():
    df = _bars(40)
    strat = ScalpHA(ema_period=5, pullback_bars=2, rr=1.5, session=None,
                    require_structure=False, min_sl_ratio=0.0, max_sl_ratio=1000.0,
                    entry_mode="market")
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    ema_v = float(d.ema.iloc[i])
    d.loc[d.index[i], ["is_doji", "big_doji", "ha_close"]] = True, True, ema_v + 1
    d.loc[d.index[i], ["high", "low", "close", "spread"]] = 110.0, 104.0, 105.0, 20
    d.loc[d.index[i], ["ha_low", "ha_high"]] = ema_v + 0.05, ema_v + 1.5
    for j in (i - 2, i - 1):
        d.loc[d.index[j], ["clean_bear", "is_doji", "small_doji"]] = True, False, False
    sig = strat.on_bar(_bar(d, i), False)
    assert sig is not None
    fill = strat._market_fill(d.iloc[i], Side.BUY)
    risk = fill - sig.sl
    assert abs((sig.tp - fill) - risk * 1.5) < 1e-9


def test_ema_touch_blocks_entry():
    df = _bars(40)
    strat = ScalpHA(ema_period=5, pullback_bars=2, rr=1.0, session=None,
                    require_structure=False, min_sl_ratio=0.0, max_sl_ratio=1000.0)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    ema_v = float(d.ema.iloc[i])
    # BUY по ha_close, но HA-свеча касается EMA.
    d.loc[d.index[i], ["is_doji", "big_doji", "ha_close"]] = True, True, ema_v + 0.5
    d.loc[d.index[i], ["ha_low", "ha_high"]] = ema_v - 0.1, ema_v + 1.0
    d.loc[d.index[i], ["high", "low", "close"]] = 110.0, 100.0, 105.0
    for j in (i - 2, i - 1):
        d.loc[d.index[j], ["clean_bear", "is_doji", "small_doji"]] = True, False, False
    assert strat.on_bar(_bar(d, i), False) is None
    assert strat.skipped_ema_touch == 1


def test_one_clean_candle_is_not_a_pullback():
    df = _bars(40)
    strat = ScalpHA(ema_period=5, session=None, require_structure=False,
                    min_sl_ratio=0.0, max_sl_ratio=1000.0)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    d.loc[d.index[i], ["is_doji", "big_doji", "ha_close"]] = True, True, float(d.ema.iloc[i]) + 1
    d.loc[d.index[i], ["high", "low"]] = 110.0, 100.0
    d.loc[d.index[i - 1], ["clean_bear", "is_doji", "small_doji"]] = True, False, False
    d.loc[d.index[i - 2], ["clean_bear", "is_doji", "small_doji"]] = False, False, False
    assert strat.on_bar(_bar(d, i), False) is None


def test_first_pullback_after_ema_is_structure_not_entry():
    df = _bars(40)
    strat = ScalpHA(ema_period=5, session=None, require_structure=True,
                    min_sl_ratio=0.0, max_sl_ratio=1000.0)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    ema_v = float(d.ema.iloc[i])
    d.loc[d.index[i], ["is_doji", "big_doji", "ha_close", "structure_ready"]] = (
        True, True, ema_v + 1, False)
    d.loc[d.index[i], ["high", "low", "ha_low", "ha_high"]] = (
        110.0, 104.0, ema_v + 0.05, ema_v + 1.5)
    for j in (i - 2, i - 1):
        d.loc[d.index[j], ["clean_bear", "is_doji", "small_doji"]] = True, False, False
    assert strat.on_bar(_bar(d, i), False) is None
    assert strat.skipped_structure == 1


def test_small_doji_inside_pullback_kills_setup():
    df = _bars(40)
    strat = ScalpHA(ema_period=5, session=None, require_structure=False,
                    min_sl_ratio=0.0, max_sl_ratio=1000.0)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    ema_v = float(d.ema.iloc[i])
    d.loc[d.index[i], ["is_doji", "big_doji", "ha_close"]] = True, True, ema_v + 1
    d.loc[d.index[i], ["high", "low", "ha_low", "ha_high"]] = (
        110.0, 104.0, ema_v + 0.05, ema_v + 1.5)
    d.loc[d.index[i - 1], ["clean_bear", "is_doji", "small_doji"]] = True, False, False
    d.loc[d.index[i - 2], ["clean_bear", "is_doji", "small_doji"]] = True, True, True
    assert strat.on_bar(_bar(d, i), False) is None
    assert strat.skipped_small_doji == 1


def test_structure_ready_after_first_pullback_resumes():
    df = _bars(50, step=0.5)
    strat = ScalpHA(ema_period=5, session=None)
    feat = strat.prepare(df)
    # Пока ha_close выше EMA и нет отката — структура ещё не готова.
    above = feat.ha_close > feat.ema
    if above.iloc[-1]:
        # На чистом аптренде без смены цвета structure_ready должно быть False
        # до первого against-свечи и возврата. Если последняя свеча бычья
        # и ready — значит откат уже был; инвариант: ready ⇒ был откат.
        if bool(feat.structure_ready.iloc[-1]):
            assert bool(feat.ha_bull.eq(False).any())


def test_sr_filter_blocks_when_far_from_level():
    df = _bars(40)
    strat = ScalpHA(ema_period=5, pullback_bars=2, rr=1.0, session=None,
                    require_structure=False, min_sl_ratio=0.0, max_sl_ratio=1000.0,
                    ta_filter="sr", sr_atr=0.85)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    ema_v = float(d.ema.iloc[i])
    d.loc[d.index[i], ["is_doji", "big_doji", "ha_close"]] = True, True, ema_v + 1
    d.loc[d.index[i], ["high", "low", "close"]] = 110.0, 104.0, 105.0
    d.loc[d.index[i], ["ha_low", "ha_high"]] = ema_v + 0.05, ema_v + 1.5
    d.loc[d.index[i], ["last_low", "last_high", "atr"]] = 50.0, 200.0, 1.0
    for j in (i - 2, i - 1):
        d.loc[d.index[j], ["clean_bear", "is_doji", "small_doji"]] = True, False, False
    assert strat.on_bar(_bar(d, i), False) is None
    assert strat.skipped_ta == 1


def test_sr_filter_allows_buy_at_support():
    df = _bars(40)
    strat = ScalpHA(ema_period=5, pullback_bars=2, rr=1.0, session=None,
                    require_structure=False, min_sl_ratio=0.0, max_sl_ratio=1000.0,
                    ta_filter="sr", sr_atr=0.85)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    ema_v = float(d.ema.iloc[i])
    d.loc[d.index[i], ["is_doji", "big_doji", "ha_close"]] = True, True, ema_v + 1
    d.loc[d.index[i], ["high", "low", "close"]] = 110.0, 104.0, 105.0
    d.loc[d.index[i], ["ha_low", "ha_high"]] = ema_v + 0.05, ema_v + 1.5
    d.loc[d.index[i], ["last_low", "last_high", "atr"]] = 104.0, 130.0, 2.0
    for j in (i - 2, i - 1):
        d.loc[d.index[j], ["clean_bear", "is_doji", "small_doji"]] = True, False, False
    sig = strat.on_bar(_bar(d, i), False)
    assert sig is not None
    assert "поддержк" in (sig.reason or "")


def test_crypto_sr_doji_buy_at_support_without_hss_pullback():
    df = _bars(50)
    strat = ScalpHA(ema_period=5, rr=1.5, session=None,
                    setup_mode="sr_doji", ta_filter="off", entry_mode="market",
                    min_sl_ratio=0.0, max_sl_ratio=1000.0)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    d.loc[d.index[i], ["is_doji", "big_doji"]] = True, False
    d.loc[d.index[i], ["high", "low", "close"]] = 110.0, 104.0, 105.0
    d.loc[d.index[i], ["last_low", "last_high", "atr"]] = 104.2, 140.0, 2.0
    sig = strat.on_bar(_bar(d, i), False)
    assert sig is not None
    assert sig.side is Side.BUY
    assert "поддержк" in (sig.reason or "")


def test_crypto_sr_doji_sell_at_resistance():
    df = _bars(50)
    strat = ScalpHA(ema_period=5, rr=2.0, session=None,
                    setup_mode="sr_doji", ta_filter="off", entry_mode="market",
                    min_sl_ratio=0.0, max_sl_ratio=1000.0)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    d.loc[d.index[i], ["is_doji", "big_doji"]] = True, False
    d.loc[d.index[i], ["high", "low", "close"]] = 110.0, 104.0, 109.0
    d.loc[d.index[i], ["last_low", "last_high", "atr"]] = 80.0, 109.5, 2.0
    sig = strat.on_bar(_bar(d, i), False)
    assert sig is not None
    assert sig.side is Side.SELL
    assert "сопротивлен" in (sig.reason or "")


def test_crypto_sr_doji_skips_without_level():
    df = _bars(50)
    strat = ScalpHA(ema_period=5, rr=1.5, session=None,
                    setup_mode="sr_doji", ta_filter="off", entry_mode="market",
                    min_sl_ratio=0.0, max_sl_ratio=1000.0)
    strat.prepare(df)
    d = strat.df
    i = len(d) - 1
    d.loc[d.index[i], "is_doji"] = True
    d.loc[d.index[i], ["high", "low", "close"]] = 110.0, 104.0, 107.0
    d.loc[d.index[i], ["last_low", "last_high", "atr"]] = 50.0, 200.0, 1.0
    assert strat.on_bar(_bar(d, i), False) is None
    assert strat.skipped_ta >= 1


def test_cfd_hss_default_is_not_sr_doji():
    strat = ScalpHA()
    assert strat.setup_mode == "hss"
    assert strat.entry_mode == "stop"


if __name__ == "__main__":
    tests = [
        test_volume_window_three_and_same_size_counts,
        test_doji_needs_both_wicks,
        test_buy_setup_stop_at_doji_extreme,
        test_market_tp_rr_from_fill_with_spread,
        test_ema_touch_blocks_entry,
        test_one_clean_candle_is_not_a_pullback,
        test_first_pullback_after_ema_is_structure_not_entry,
        test_small_doji_inside_pullback_kills_setup,
        test_structure_ready_after_first_pullback_resumes,
        test_sr_filter_blocks_when_far_from_level,
        test_sr_filter_allows_buy_at_support,
        test_crypto_sr_doji_buy_at_support_without_hss_pullback,
        test_crypto_sr_doji_sell_at_resistance,
        test_crypto_sr_doji_skips_without_level,
        test_cfd_hss_default_is_not_sr_doji,
    ]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all", len(tests))
