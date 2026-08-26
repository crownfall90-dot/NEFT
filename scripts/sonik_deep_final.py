"""Deep dig: ticks, pivots, ML labels → Sonik ceiling on Tag MT5."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from rich.console import Console

con = Console()


def load_bars() -> pd.DataFrame:
    df = pd.read_csv("data/XAUUSD.f_M1_221d.csv", parse_dates=["time"])
    tr = pd.concat([
        df.high - df.low,
        (df.high - df.close.shift()).abs(),
        (df.low - df.close.shift()).abs(),
    ], axis=1).max(axis=1)
    df = df.copy()
    df["atr"] = tr.rolling(14).mean()
    df["body_atr"] = (df.close - df.open).abs() / df.atr
    df["dir"] = np.sign(df.close - df.open)
    df["broke"] = (
        ((df.dir > 0) & (df.close > df.high.shift(1)))
        | ((df.dir < 0) & (df.close < df.low.shift(1)))
    )
    df["ema21"] = df.close.ewm(span=21, adjust=False).mean()
    df["ema55"] = df.close.ewm(span=55, adjust=False).mean()
    df["ema200"] = df.close.ewm(span=200, adjust=False).mean()
    df["mins"] = df.time.dt.hour * 60 + df.time.dt.minute
    df["dow"] = df.time.dt.dayofweek
    df["vol_ma"] = df.tick_volume.rolling(20).mean()
    df["vol_r"] = df.tick_volume / df.vol_ma.replace(0, np.nan)
    rng = (df.high - df.low).replace(0, np.nan)
    df["clv"] = ((df.close - df.low) - (df.high - df.close)) / rng
    # fractal pivots confirmed without lookahead (pivot at i-2, confirm on bar i)
    df["pivot_hi"] = (
        (df.high.shift(2) > df.high.shift(3))
        & (df.high.shift(2) > df.high.shift(4))
        & (df.high.shift(2) > df.high.shift(1))
        & (df.high.shift(2) > df.high)
    )
    df["pivot_lo"] = (
        (df.low.shift(2) < df.low.shift(3))
        & (df.low.shift(2) < df.low.shift(4))
        & (df.low.shift(2) < df.low.shift(1))
        & (df.low.shift(2) < df.low)
    )
    return df


def first_hit(df, i, side, sl=2.5, tp=4.0):
    entry = float(df.close.iloc[i])
    for j in range(i + 1, min(i + 90, len(df))):
        if side > 0:
            if df.low.iloc[j] <= entry - sl:
                return 0
            if df.high.iloc[j] >= entry + tp:
                return 1
        else:
            if df.high.iloc[j] >= entry + sl:
                return 0
            if df.low.iloc[j] <= entry - tp:
                return 1
    return -1


def mae_mfe(df, i, side, n=30):
    entry = float(df.close.iloc[i])
    mae = 0.0
    mfe = 0.0
    for j in range(i + 1, min(i + n, len(df))):
        if side > 0:
            mae = min(mae, float(df.low.iloc[j]) - entry)
            mfe = max(mfe, float(df.high.iloc[j]) - entry)
        else:
            mae = min(mae, entry - float(df.high.iloc[j]))
            mfe = max(mfe, entry - float(df.low.iloc[j]))
    return mae, mfe


def dist_to_last_pivot(df, i, side):
    """Distance in ATR to last confirmed opposite pivot."""
    a = float(df.atr.iloc[i]) or 1.0
    look = df.iloc[max(0, i - 80):i + 1]
    if side > 0:
        piv = look[look.pivot_lo]
        if piv.empty:
            return np.nan
        # pivot price is low at pivot bar index = current_index - 2 relative to confirm
        # approximate: use low of the confirm row's shift(2)
        idx = piv.index[-1]
        lvl = float(df.low.shift(2).loc[idx])
        return (float(df.close.iloc[i]) - lvl) / a
    piv = look[look.pivot_hi]
    if piv.empty:
        return np.nan
    idx = piv.index[-1]
    lvl = float(df.high.shift(2).loc[idx])
    return (lvl - float(df.close.iloc[i])) / a


def probe_ticks(trades: pd.DataFrame) -> None:
    con.print("\n[bold cyan]1) MT5 ticks around live entries[/]")
    try:
        import MetaTrader5 as mt5
    except ImportError:
        con.print("  MetaTrader5 not available")
        return
    if not mt5.initialize():
        con.print(f"  MT5 fail: {mt5.last_error()}")
        return
    sample = trades.head(25)
    rows = []
    for r in sample.itertuples():
        t0 = r.open.to_pydatetime() - timedelta(seconds=90)
        t1 = r.open.to_pydatetime() + timedelta(seconds=30)
        ticks = mt5.copy_ticks_range("XAUUSD.f", t0, t1, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) < 5:
            continue
        td = pd.DataFrame(ticks)
        td["time"] = pd.to_datetime(td["time"], unit="s")
        # before entry
        before = td[td.time <= r.open]
        after = td[td.time > r.open]
        if before.empty:
            continue
        side = 1 if r.side == "Buy" else -1
        bid = before["bid"].astype(float)
        ask = before["ask"].astype(float)
        mid = (bid + ask) / 2
        spread = (ask - bid).median()
        # micro momentum last 20 ticks
        if len(mid) >= 20:
            mom = (mid.iloc[-1] - mid.iloc[-20]) * side
        else:
            mom = (mid.iloc[-1] - mid.iloc[0]) * side
        # aggressive: last ticks hitting ask for buy
        last = before.tail(10)
        if "flags" in last.columns:
            # flags bit meaning varies; use bid/ask move
            upticks = (last["bid"].diff() > 0).sum()
            dnticks = (last["bid"].diff() < 0).sum()
        else:
            upticks = dnticks = 0
        rows.append({
            "n_ticks": len(td),
            "spread": float(spread),
            "mom20": float(mom),
            "upticks": int(upticks),
            "dnticks": int(dnticks),
            "won": r.pnl > 0,
            "side": side,
        })
    mt5.shutdown()
    if not rows:
        con.print("  no ticks returned (history empty or symbol denied)")
        return
    f = pd.DataFrame(rows)
    con.print(f"  samples with ticks: {len(f)}")
    con.print(
        f"  spread med={f.spread.median():.3f}  mom20_signed med={f.mom20.median():.3f} "
        f"pct mom>0={(f.mom20 > 0).mean() * 100:.0f}%"
    )
    con.print(
        f"  WR when mom>0: {f[f.mom20 > 0].won.mean() * 100:.0f}%  "
        f"when mom<=0: {f[f.mom20 <= 0].won.mean() * 100:.0f}%"
    )


def build_labeled(df: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    entry_idx = {}
    for r in trades.itertuples():
        i = int(df.time.searchsorted(r.open, side="right") - 1)
        if i >= 60:
            entry_idx[i] = 1 if r.side == "Buy" else -1

    rows = []
    for i in range(80, len(df) - 90):
        row = df.iloc[i]
        if not (400 <= row.mins < 600):
            continue
        if row.atr != row.atr or row.body_atr < 0.4 or not bool(row.broke):
            continue
        side = 1 if row.close > row.open else -1
        prior = ((df.close.iloc[i - 1] - df.close.iloc[i - 6]) / row.atr) * side
        slope = ((df.ema21.iloc[i] - df.ema21.iloc[i - 5]) / row.atr) * side
        hit = first_hit(df, i, side)
        if hit < 0:
            continue
        mae, mfe = mae_mfe(df, i, side)
        piv = dist_to_last_pivot(df, i, side)
        is_live = 1 if i in entry_idx else 0
        rows.append({
            "i": i,
            "side": side,
            "live": is_live,
            "win": hit,
            "mae": mae,
            "mfe": mfe,
            "quality": 1 if (mfe >= 4 and mae > -2.5) else 0,
            "body": float(row.body_atr),
            "prior": float(prior),
            "slope": float(slope),
            "vol_r": float(row.vol_r) if row.vol_r == row.vol_r else 1.0,
            "clv": float(row.clv) * side if row.clv == row.clv else 0.0,
            "dist21": abs(float(row.close) - float(row.ema21)) / float(row.atr),
            "dist200": (float(row.close) - float(row.ema200)) / float(row.atr) * side,
            "trend": int((row.ema21 > row.ema55) == (side > 0)),
            "piv_dist": float(piv) if piv == piv else 9.0,
            "mins": int(row.mins),
            "dow": int(row.dow),
            "spread": float(row.spread),
            "atr": float(row.atr),
        })
    return pd.DataFrame(rows)


def rule_search(f: pd.DataFrame) -> list[dict]:
    """Compact rule search maximizing expectancy with N>=25, 1/day applied later."""
    best = []
    for prior_max in [-0.4, -0.8, -1.2]:
        for slope_min in [0.1, 0.25]:
            for body_min in [0.45, 0.7]:
                for piv_max in [0.8, 1.5, 3.0, 9.0]:
                    for mins_hi in [480, 510, 540]:
                        for dow_set in (None, {0, 1, 2, 3}, {1, 2, 3}):
                            m = (
                                (f.prior <= prior_max)
                                & (f.slope >= slope_min)
                                & (f.body >= body_min)
                                & (f.piv_dist <= piv_max)
                                & (f.mins >= 420)
                                & (f.mins < mins_hi)
                            )
                            if dow_set is not None:
                                m = m & f.dow.isin(dow_set)
                            sub = f.loc[m]
                            if len(sub) < 40:
                                continue
                            wr = sub.win.mean()
                            # crude expectancy in points SL2.5 TP4
                            exp = wr * 4 - (1 - wr) * 2.5
                            if exp > 0.3 and wr >= 0.48:
                                best.append({
                                    "exp": exp, "wr": wr, "n": len(sub),
                                    "q": sub.quality.mean(),
                                    "live_rate": sub.live.mean(),
                                    "prior": prior_max, "slope": slope_min,
                                    "body": body_min, "piv": piv_max,
                                    "until": mins_hi, "dow": dow_set,
                                })
    best.sort(key=lambda x: (x["exp"], x["wr"]), reverse=True)
    return best


def simulate_rule(df: pd.DataFrame, f: pd.DataFrame, rule: dict, spread=0.16) -> dict:
    """1 trade/day, chronological."""
    m = (
        (f.prior <= rule["prior"])
        & (f.slope >= rule["slope"])
        & (f.body >= rule["body"])
        & (f.piv_dist <= rule["piv"])
        & (f.mins >= 420)
        & (f.mins < rule["until"])
    )
    if rule["dow"] is not None:
        m = m & f.dow.isin(rule["dow"])
    sub = f.loc[m].sort_values("i")
    used = {}
    pnls = []
    for row in sub.itertuples():
        day = df.time.iloc[int(row.i)].date()
        if used.get(day, 0) >= 1:
            continue
        # resim with spread
        entry = float(df.close.iloc[int(row.i)]) + int(row.side) * spread
        side = int(row.side)
        sl, tp = entry - side * 2.5, entry + side * 4.0
        hit = None
        for j in range(int(row.i) + 1, min(int(row.i) + 90, len(df))):
            hi, lo = float(df.high.iloc[j]), float(df.low.iloc[j])
            if side > 0:
                if lo <= sl:
                    hit = -2.5 - spread
                    break
                if hi >= tp:
                    hit = 4.0 - spread
                    break
            else:
                if hi >= sl:
                    hit = -2.5 - spread
                    break
                if lo <= tp:
                    hit = 4.0 - spread
                    break
        if hit is None:
            continue
        pnls.append(hit)
        used[day] = 1
    a = np.array(pnls)
    if not len(a):
        return {"n": 0}
    return {
        "n": len(a),
        "wr": float((a > 0).mean()),
        "sum": float(a.sum()),
        "avg": float(a.mean()),
        "ret_usd_001": float(a.sum()),  # points * 0.01 * 100 = points
    }


def try_sklearn(f: pd.DataFrame) -> None:
    con.print("\n[bold cyan]3) sklearn decision tree (if available)[/]")
    try:
        from sklearn.tree import DecisionTreeClassifier, export_text
        from sklearn.model_selection import cross_val_score
    except ImportError:
        con.print("  sklearn missing — skip")
        return
    feats = ["body", "prior", "slope", "vol_r", "clv", "dist21", "dist200",
             "trend", "piv_dist", "mins", "dow", "spread", "atr"]
    X = f[feats].fillna(0)
    y = f["win"]
    clf = DecisionTreeClassifier(max_depth=4, min_samples_leaf=40, random_state=0)
    scores = cross_val_score(clf, X, y, cv=5, scoring="accuracy")
    clf.fit(X, y)
    con.print(f"  CV accuracy: {scores.mean()*100:.1f}% ± {scores.std()*100:.1f}% (base {y.mean()*100:.1f}%)")
    con.print(export_text(clf, feature_names=feats, max_depth=4)[:1200])
    # also predict quality
    yq = f["quality"]
    clf2 = DecisionTreeClassifier(max_depth=4, min_samples_leaf=40, random_state=0)
    s2 = cross_val_score(clf2, X, yq, cv=5, scoring="accuracy")
    clf2.fit(X, yq)
    con.print(f"  quality CV: {s2.mean()*100:.1f}% (base {yq.mean()*100:.1f}%)")


def news_overlap(trades: pd.DataFrame) -> None:
    con.print("\n[bold cyan]4) news proximity[/]")
    try:
        from neft.core.news import load_calendar
        events = load_calendar(refresh=False)
    except Exception as e:
        con.print(f"  calendar: {e}")
        return
    if not events:
        con.print("  no events cached")
        return
    # only overlap week — weak. Check structure
    hi = [e for e in events if str(getattr(e, "impact", "")).lower() == "high"]
    con.print(f"  calendar events={len(events)} high={len(hi)} (usually current week only)")
    # historical news module?
    try:
        from neft.core import news_historical as nh
        con.print(f"  news_historical module: {nh}")
    except Exception as e:
        con.print(f"  no historical news usable: {e}")


def main():
    trades = pd.read_csv("data/sonik_trades.csv")
    trades["open"] = pd.to_datetime(trades["open"])
    trades["close"] = pd.to_datetime(trades["close"])
    df = load_bars()
    t = trades[(trades.open >= df.time.min()) & (trades.open <= df.time.max())].copy()
    con.print(f"bars={len(df)} live_overlap={len(t)}")

    probe_ticks(t)

    con.print("\n[bold cyan]2) labeled candidates + rule search[/]")
    f = build_labeled(df, t)
    con.print(
        f"  candidates={len(f)} live_marked={f.live.sum()} "
        f"base_WR={f.win.mean()*100:.1f}% quality={f.quality.mean()*100:.1f}%"
    )
    # live vs nonlive quality
    if f.live.sum():
        con.print(
            f"  LIVE quality={f[f.live==1].quality.mean()*100:.0f}% "
            f"WR={f[f.live==1].win.mean()*100:.0f}% "
            f"mae={f[f.live==1].mae.median():.2f} mfe={f[f.live==1].mfe.median():.2f}"
        )
        con.print(
            f"  OTHER quality={f[f.live==0].quality.mean()*100:.0f}% "
            f"WR={f[f.live==0].win.mean()*100:.0f}% "
            f"piv_dist L={f[f.live==1].piv_dist.median():.2f} "
            f"O={f[f.live==0].piv_dist.median():.2f}"
        )

    rules = rule_search(f)
    con.print(f"  profitable rules found: {len(rules)}")
    for r in rules[:8]:
        sim = simulate_rule(df, f, r)
        con.print(
            f"  exp={r['exp']:.2f} WR={r['wr']*100:.0f}% Ncand={r['n']} "
            f"prior<={r['prior']} slope>={r['slope']} piv<={r['piv']} "
            f"until={r['until']} dow={r['dow']} → "
            f"1/day N={sim.get('n')} WR={sim.get('wr',0)*100:.0f}% "
            f"sumPts={sim.get('sum',0):.1f}"
        )

    # save best rule
    out = Path("data/sonik_best_rule.json")
    if rules:
        import json
        best = rules[0]
        sim = simulate_rule(df, f, best)
        payload = {"rule": {k: (list(v) if isinstance(v, set) else v) for k, v in best.items()},
                   "sim": sim}
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        con.print(f"  saved {out}")

    try_sklearn(f)
    news_overlap(t)

    # oracle ceiling: if we could pick only quality==1 among v4-like
    con.print("\n[bold cyan]5) oracle ceiling[/]")
    v4 = f[(f.prior <= -0.6) & (f.slope >= 0.15) & (f.mins < 510) & (f.mins >= 420)]
    used = {}
    oracle = []
    randomish = []
    for row in v4.sort_values("i").itertuples():
        day = df.time.iloc[int(row.i)].date()
        if used.get(day, 0) >= 1:
            continue
        # take if quality else skip day? oracle takes only quality
        if row.quality:
            oracle.append(4.0 - 0.16 if row.win else -2.5 - 0.16)
            used[day] = 1
    used = {}
    for row in v4.sort_values("i").itertuples():
        day = df.time.iloc[int(row.i)].date()
        if used.get(day, 0) >= 1:
            continue
        randomish.append(4.0 - 0.16 if row.win else -2.5 - 0.16)
        used[day] = 1
    if oracle:
        oa = np.array(oracle)
        con.print(
            f"  oracle quality 1/day: N={len(oa)} WR={(oa>0).mean()*100:.0f}% "
            f"sum={oa.sum():.1f} (~${oa.sum():.0f} on 0.01)"
        )
    if randomish:
        ra = np.array(randomish)
        con.print(
            f"  v4-like 1/day: N={len(ra)} WR={(ra>0).mean()*100:.0f}% sum={ra.sum():.1f}"
        )
    # what % of days have a quality setup?
    days = v4.copy()
    days["day"] = [df.time.iloc[int(i)].date() for i in days.i]
    has_q = days.groupby("day").quality.max().mean()
    con.print(f"  days with ≥1 quality setup among v4-like: {has_q*100:.0f}%")


if __name__ == "__main__":
    main()
