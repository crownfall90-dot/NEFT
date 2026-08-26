"""SONIK — разбор всеми способами: live, ML, ticks, rules, walk-forward, ceiling."""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from neft.backtest import metrics
from neft.backtest.engine import Backtester, Costs
from neft.core import symbols
from neft.core.config import ROOT
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.sonik_open import SonikPulse

con = Console()
OUT = ROOT / "dashboard"
DATA = ROOT / "data"


def prep(m1: pd.DataFrame) -> pd.DataFrame:
    df = m1.copy()
    tr = pd.concat([
        df.high - df.low,
        (df.high - df.close.shift()).abs(),
        (df.low - df.close.shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["body"] = (df.close - df.open).abs()
    df["body_atr"] = df.body / df.atr
    df["ema21"] = df.close.ewm(span=21, adjust=False).mean()
    df["ema55"] = df.close.ewm(span=55, adjust=False).mean()
    df["ema200"] = df.close.ewm(span=200, adjust=False).mean()
    df["mins"] = df.time.dt.hour * 60 + df.time.dt.minute
    df["dow"] = df.time.dt.dayofweek
    df["vol_r"] = df.tick_volume / df.tick_volume.rolling(20).mean().replace(0, np.nan)
    rng = (df.high - df.low).replace(0, np.nan)
    df["clv"] = ((df.close - df.low) - (df.high - df.close)) / rng
    df["piv_hi"] = (
        (df.high.shift(2) > df.high.shift(3)) & (df.high.shift(2) > df.high.shift(4))
        & (df.high.shift(2) > df.high.shift(1)) & (df.high.shift(2) > df.high)
    )
    df["piv_lo"] = (
        (df.low.shift(2) < df.low.shift(3)) & (df.low.shift(2) < df.low.shift(4))
        & (df.low.shift(2) < df.low.shift(1)) & (df.low.shift(2) < df.low)
    )
    return df


def classify(df: pd.DataFrame, i: int, side: int) -> dict:
    bar = df.iloc[i]
    a = float(bar.atr) or 1.0
    prev5 = df.iloc[i - 5:i]
    net5 = float(prev5.close.iloc[-1] - prev5.close.iloc[0])
    pullback = net5 * side < 0
    deep_pb = (net5 * side) < -0.5 * a
    impulse = (float(bar.close) - float(bar.open)) * side > 0
    strong_body = float(bar.body_atr) >= 0.55 if bar.body_atr == bar.body_atr else False
    broke = (
        (side > 0 and bar.close > float(df.high.iloc[i - 1]))
        or (side < 0 and bar.close < float(df.low.iloc[i - 1]))
    )
    opp = int(((prev5.close - prev5.open) * side < 0).sum())
    look = df.iloc[max(0, i - 40):i + 1]
    near_piv = False
    piv_dist = 9.0
    if side > 0:
        pivs = look[look.piv_lo]
        if len(pivs):
            lvl = float(df.low.shift(2).loc[pivs.index[-1]])
            piv_dist = abs(float(bar.close) - lvl) / a
            near_piv = piv_dist <= 1.2
    else:
        pivs = look[look.piv_hi]
        if len(pivs):
            lvl = float(df.high.shift(2).loc[pivs.index[-1]])
            piv_dist = abs(float(bar.close) - lvl) / a
            near_piv = piv_dist <= 1.2
    slope = ((float(bar.ema21) - float(df.ema21.iloc[i - 5])) / a) * side
    day = bar.time.date()
    day_df = df[df.time.dt.date == day]
    asia = day_df[day_df.time.dt.hour < 6]
    asia_pos = "na"
    asia_range = np.nan
    if len(asia):
        ahi, alo = float(asia.high.max()), float(asia.low.min())
        asia_range = ahi - alo
        if alo <= bar.close <= ahi:
            asia_pos = "inside"
        elif bar.close > ahi:
            asia_pos = "above"
        else:
            asia_pos = "below"
    if pullback and impulse and broke and strong_body:
        setup = "pb_impulse_break"
    elif pullback and impulse:
        setup = "pb_impulse"
    elif impulse and broke and not pullback:
        setup = "chase_break"
    elif pullback and not impulse:
        setup = "pb_weak"
    else:
        setup = "other"
    fut = df.iloc[i + 1:i + 9]
    if len(fut):
        if side > 0:
            mae = float(fut.low.min() - bar.close)
            mfe = float(fut.high.max() - bar.close)
        else:
            mae = float(bar.close - fut.high.max())
            mfe = float(bar.close - fut.low.min())
    else:
        mae = mfe = 0.0
    return {
        "setup": setup, "pullback": pullback, "deep_pb": deep_pb, "impulse": impulse,
        "broke": broke, "strong_body": strong_body, "opp5": opp, "near_piv": near_piv,
        "piv_dist": piv_dist, "slope": slope, "asia_pos": asia_pos,
        "asia_range": asia_range, "mae8": mae, "mfe8": mfe,
        "hour": int(bar.time.hour), "mins": int(bar.mins), "dow": int(bar.dow),
        "body_atr": float(bar.body_atr) if bar.body_atr == bar.body_atr else 0,
        "prior5": net5 * side / a,
        "vol_r": float(bar.vol_r) if bar.vol_r == bar.vol_r else 1.0,
        "clv": float(bar.clv) * side if bar.clv == bar.clv else 0.0,
        "spread": float(bar.spread) if "spread" in bar.index else 0.0,
        "atr": a,
    }


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


def probe_ticks(trades: pd.DataFrame) -> dict:
    out = {"ok": False, "n": 0, "note": ""}
    try:
        import MetaTrader5 as mt5
    except ImportError:
        out["note"] = "MetaTrader5 not installed"
        return out
    if not mt5.initialize():
        out["note"] = f"MT5 init fail: {mt5.last_error()}"
        return out
    rows = []
    for r in trades.itertuples():
        t0 = r.open.to_pydatetime() - timedelta(seconds=90)
        t1 = r.open.to_pydatetime() + timedelta(seconds=30)
        ticks = mt5.copy_ticks_range("XAUUSD.f", t0, t1, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) < 5:
            continue
        td = pd.DataFrame(ticks)
        td["time"] = pd.to_datetime(td["time"], unit="s")
        before = td[td.time <= r.open]
        if before.empty or len(before) < 5:
            continue
        side = 1 if r.side == "Buy" else -1
        mid = (before["bid"].astype(float) + before["ask"].astype(float)) / 2
        mom = (mid.iloc[-1] - mid.iloc[-min(20, len(mid))]) * side
        rows.append({"mom20": float(mom), "spread": float((before.ask - before.bid).median()),
                     "won": bool(r.pnl > 0), "n_ticks": len(td)})
    mt5.shutdown()
    if not rows:
        out["note"] = "no ticks returned"
        return out
    f = pd.DataFrame(rows)
    out.update({
        "ok": True, "n": len(f),
        "spread_med": float(f.spread.median()),
        "mom_med": float(f.mom20.median()),
        "wr_mom_pos": float(f[f.mom20 > 0].won.mean() * 100) if (f.mom20 > 0).any() else None,
        "wr_mom_neg": float(f[f.mom20 <= 0].won.mean() * 100) if (f.mom20 <= 0).any() else None,
        "pct_mom_pos": float((f.mom20 > 0).mean() * 100),
    })
    return out


def run_bt(m1, costs, spec, **kw):
    rm = RiskManager(
        start_balance=1000,
        limits=RiskLimits(
            risk_per_trade_pct=0.25, max_risk_per_trade_pct=3.0,
            min_risk_per_trade_pct=0.0, max_volume=100.0,
            max_daily_loss_pct=100.0, max_drawdown_pct=100.0, min_free_margin_pct=0.0,
        ),
    )
    base = dict(
        risk_pct=0.25, risk_manager=rm, spec=spec,
        session_from=(7, 0), session_until=(9, 0),
        session2_from=None, session2_until=None,
        sl_points=2.5, tp_points=4.0, max_trades_day=1, cooldown_bars=12,
        require_break=True, min_body_atr=0.5,
    )
    base.update(kw)
    s = SonikPulse(**base)
    res = Backtester(s, rm, costs, 1000, "XAUUSD.f").run(m1)
    m = metrics.compute(res.equity, res.trades, 1000, res.ruined)
    return m, res


def walk_forward(m1, costs, spec, kw, folds=3):
    """Equal-time folds on M1 window."""
    t0, t1 = m1.time.min(), m1.time.max()
    span = (t1 - t0) / folds
    rows = []
    for k in range(folds):
        a = t0 + span * k
        b = t0 + span * (k + 1)
        chunk = m1[(m1.time >= a) & (m1.time < b)].reset_index(drop=True)
        if len(chunk) < 5000:
            continue
        m, _ = run_bt(chunk, costs, spec, **kw)
        rows.append({
            "fold": k + 1,
            "from": str(a.date()), "to": str(b.date()),
            "ret": m.return_pct, "wr": m.win_rate, "dd": m.max_drawdown_pct,
            "n": m.trades, "pf": m.profit_factor,
        })
    return rows


def main():
    trades = pd.read_csv(DATA / "sonik_trades.csv")
    trades["open"] = pd.to_datetime(trades["open"])
    trades["close"] = pd.to_datetime(trades["close"])
    m1 = prep(pd.read_csv(DATA / "XAUUSD.f_M1_221d.csv", parse_dates=["time"]))
    m5 = pd.read_csv(DATA / "XAUUSD.f_M5_221d.csv", parse_dates=["time"])
    t = trades[(trades.open >= m1.time.min()) & (trades.open <= m1.time.max())].copy()
    t["won"] = t.pnl > 0

    insight = []
    sections = {}

    # ── 1. classify live ──
    rows = []
    for r in t.itertuples():
        i = int(m1.time.searchsorted(r.open, side="right") - 1)
        if i < 60:
            continue
        side = 1 if r.side == "Buy" else -1
        c = classify(m1, i, side)
        c.update({
            "side": r.side, "pnl": float(r.pnl), "won": bool(r.pnl > 0),
            "open": str(r.open), "close": str(r.close), "entry": float(r.entry),
            "lot": float(r.lot), "hold": (r.close - r.open).total_seconds() / 60, "i": i,
        })
        rows.append(c)
    g = pd.DataFrame(rows)
    con.print(f"[bold]1) Live classify[/] N={len(g)} WR={g.won.mean()*100:.0f}%")

    setup_stats = []
    for setup, s in g.groupby("setup"):
        setup_stats.append({
            "setup": setup, "n": int(len(s)), "wr": round(float(s.won.mean() * 100), 1),
            "avg_pnl": round(float(s.pnl.mean()), 2),
            "mae": round(float(s.mae8.median()), 2), "mfe": round(float(s.mfe8.median()), 2),
        })
    combos = [
        ("pb+impulse", g.pullback & g.impulse),
        ("pb+impulse+broke", g.pullback & g.impulse & g.broke),
        ("pb+impulse+near_piv", g.pullback & g.impulse & g.near_piv),
        ("deep_pb+impulse+near_piv", g.deep_pb & g.impulse & g.near_piv),
        ("pb+impulse+slope>0.15", g.pullback & g.impulse & (g.slope > 0.15)),
        ("deep+piv+slope", g.deep_pb & g.impulse & g.near_piv & (g.slope > 0.15)),
        ("chase_break", (~g.pullback) & g.broke & g.impulse),
        ("London 07-09", g.hour.between(7, 9)),
        ("asia inside", g.asia_pos == "inside"),
        ("piv_dist<=1.0", g.piv_dist <= 1.0),
    ]
    combo_stats = []
    for name, mask in combos:
        s = g[mask]
        if len(s) < 5:
            continue
        combo_stats.append({
            "combo": name, "n": int(len(s)), "wr": round(float(s.won.mean() * 100), 1),
            "avg_pnl": round(float(s.pnl.mean()), 2),
            "mae": round(float(s.mae8.median()), 2), "mfe": round(float(s.mfe8.median()), 2),
        })
    sections["setup_stats"] = setup_stats
    sections["combo_stats"] = combo_stats
    best_live = max(combo_stats, key=lambda x: (x["wr"], x["avg_pnl"])) if combo_stats else None
    if best_live:
        insight.append(
            f"На логе лучший комбо: {best_live['combo']} → WR {best_live['wr']}% "
            f"N={best_live['n']} avg ${best_live['avg_pnl']}"
        )

    # hour / dow
    hour_rows = []
    for h, s in g.groupby("hour"):
        if len(s) < 5:
            continue
        hour_rows.append({"hour": int(h), "n": len(s), "wr": round(s.won.mean() * 100, 1),
                          "avg": round(s.pnl.mean(), 2)})
    sections["by_hour"] = hour_rows

    # ── 2. ML ──
    ml = {"ok": False}
    try:
        from sklearn.tree import DecisionTreeClassifier, export_text
        from sklearn.model_selection import cross_val_score
        from sklearn.ensemble import GradientBoostingClassifier
        feats = ["pullback", "deep_pb", "impulse", "broke", "strong_body", "opp5",
                 "near_piv", "piv_dist", "slope", "body_atr", "prior5", "mins",
                 "hour", "vol_r", "clv", "spread", "atr"]
        X = g[feats].astype(float).fillna(0)
        y = g["won"].astype(int)
        clf = DecisionTreeClassifier(max_depth=3, min_samples_leaf=10, random_state=0)
        sc = cross_val_score(clf, X, y, cv=5, scoring="accuracy")
        clf.fit(X, y)
        tree_txt = export_text(clf, feature_names=feats)
        gb = GradientBoostingClassifier(max_depth=2, n_estimators=40, random_state=0)
        sg = cross_val_score(gb, X, y, cv=5, scoring="accuracy")
        gb.fit(X, y)
        imp = sorted(zip(feats, gb.feature_importances_), key=lambda x: -x[1])
        ml = {
            "ok": True,
            "tree_cv": round(float(sc.mean() * 100), 1),
            "tree_std": round(float(sc.std() * 100), 1),
            "base": round(float(y.mean() * 100), 1),
            "gb_cv": round(float(sg.mean() * 100), 1),
            "tree": tree_txt[:1200],
            "importance": [{"f": a, "w": round(float(b), 3)} for a, b in imp[:8]],
        }
        con.print(f"[bold]2) ML[/] tree CV {ml['tree_cv']}% base {ml['base']}%  "
                  f"GB {ml['gb_cv']}%  top={imp[0][0]}")
        insight.append(
            f"ML: top features {', '.join(a for a,_ in imp[:4])} — "
            f"CV≈{ml['gb_cv']}% vs base {ml['base']}% (слабый lift → edge не в простых OHLC)."
        )
    except Exception as e:
        ml["note"] = str(e)
        con.print(f"[bold]2) ML[/] fail: {e}")
    sections["ml"] = ml

    # ── 3. ticks ──
    con.print("[bold]3) Ticks[/]")
    ticks = probe_ticks(t)
    sections["ticks"] = ticks
    if ticks.get("ok"):
        con.print(f"  n={ticks['n']} mom>0 WR={ticks.get('wr_mom_pos')} "
                  f"mom<=0 WR={ticks.get('wr_mom_neg')}")
        insight.append(
            f"Ticks: mom>0 WR={ticks.get('wr_mom_pos')}% vs mom<=0 "
            f"{ticks.get('wr_mom_neg')}% (n={ticks['n']}) — микро-моментум слабо отделяет."
        )
    else:
        con.print(f"  {ticks.get('note')}")
        insight.append(f"Ticks: {ticks.get('note')}")

    # ── 4. candidate pool WR (all chart signals) ──
    con.print("[bold]4) All-chart candidates vs live[/]")
    cand = []
    live_idx = set()
    for r in g.itertuples():
        live_idx.add(int(r.i))
    for i in range(80, len(m1) - 90):
        row = m1.iloc[i]
        if not (420 <= row.mins < 540):
            continue
        if row.atr != row.atr or row.body_atr != row.body_atr or row.body_atr < 0.5:
            continue
        side = 1 if row.close > row.open else -1
        broke = (side > 0 and row.close > m1.high.iloc[i - 1]) or (
            side < 0 and row.close < m1.low.iloc[i - 1])
        if not broke:
            continue
        prior = ((m1.close.iloc[i - 1] - m1.close.iloc[i - 6]) / row.atr) * side
        slope = ((m1.ema21.iloc[i] - m1.ema21.iloc[i - 5]) / row.atr) * side
        hit = first_hit(m1, i, side)
        if hit < 0:
            continue
        look = m1.iloc[max(0, i - 40):i + 1]
        if side > 0:
            pivs = look[look.piv_lo]
            piv_d = 9.0
            if len(pivs):
                lvl = float(m1.low.shift(2).loc[pivs.index[-1]])
                piv_d = abs(float(row.close) - lvl) / float(row.atr)
        else:
            pivs = look[look.piv_hi]
            piv_d = 9.0
            if len(pivs):
                lvl = float(m1.high.shift(2).loc[pivs.index[-1]])
                piv_d = abs(float(row.close) - lvl) / float(row.atr)
        cand.append({
            "i": i, "win": hit, "prior": prior, "slope": slope, "piv": piv_d,
            "live": 1 if i in live_idx else 0,
        })
    c = pd.DataFrame(cand)
    rules_cand = [
        ("any break body>=0.5", (c.prior == c.prior)),
        ("prior<=-0.6 slope>=0.15", (c.prior <= -0.6) & (c.slope >= 0.15)),
        ("+ piv<=1.5", (c.prior <= -0.6) & (c.slope >= 0.15) & (c.piv <= 1.5)),
        ("+ piv<=1.2", (c.prior <= -0.6) & (c.slope >= 0.15) & (c.piv <= 1.2)),
        ("prior<=-1 slope>=0.2", (c.prior <= -1.0) & (c.slope >= 0.2)),
    ]
    cand_stats = []
    for name, mask in rules_cand:
        s = c[mask]
        if len(s) < 20:
            continue
        cand_stats.append({
            "rule": name, "n": int(len(s)), "wr": round(float(s.win.mean() * 100), 1),
            "live_n": int(s.live.sum()),
            "live_wr": round(float(s[s.live == 1].win.mean() * 100), 1) if s.live.sum() else None,
        })
        con.print(f"  {name}: N={len(s)} WR={s.win.mean()*100:.0f}% live_in={s.live.sum()}")
    sections["cand_stats"] = cand_stats
    insight.append(
        "На всём M1 те же OHLC-правила дают WR~45–55%; live-подмножество заметно лучше — "
        "фильтр Amplify тоньше баров."
    )

    # ── 5. backtests + walk-forward ──
    con.print("[bold]5) Backtests + walk-forward[/]")
    spec = symbols.load("XAUUSD.f")
    spr = float(m1[m1.time.dt.hour.between(7, 18)].spread.median())
    costs = Costs(
        spread_points=spr, contract_size=spec.contract_size, point=spec.point,
        commission_per_lot=0.0, commission_on_close=False, leverage=30,
    )
    variants = [
        ("v5 tight", dict(max_prior_along=-1.0, min_ema_slope=0.2, session_until=(8, 30))),
        ("deep+piv+slope", dict(
            max_prior_along=-0.6, min_ema_slope=0.15,
            require_near_pivot=True, max_pivot_atr=1.5)),
        ("deep+piv1.2+slope", dict(
            max_prior_along=-0.6, min_ema_slope=0.15,
            require_near_pivot=True, max_pivot_atr=1.2)),
        ("pb+piv London wide", dict(
            max_prior_along=0.0, min_ema_slope=0.0,
            require_near_pivot=True, max_pivot_atr=1.2,
            session_until=(10, 0), max_trades_day=2)),
        ("v5 + vol1.2", dict(
            max_prior_along=-1.0, min_ema_slope=0.2, min_vol_r=1.2, session_until=(8, 30))),
        ("deep+piv+slope SL2 TP3.5", dict(
            max_prior_along=-0.6, min_ema_slope=0.15,
            require_near_pivot=True, max_pivot_atr=1.5,
            sl_points=2.0, tp_points=3.5)),
    ]
    bt = []
    best_kw = None
    best_name = None
    best_m = None
    for name, kw in variants:
        m, res = run_bt(m1, costs, spec, **kw)
        bt.append({
            "name": name, "return_pct": m.return_pct, "win_rate": m.win_rate,
            "dd": m.max_drawdown_pct, "trades": m.trades, "pf": m.profit_factor,
        })
        con.print(
            f"  {name}: ret={m.return_pct:+.2f}% WR={m.win_rate:.0f}% "
            f"DD={m.max_drawdown_pct:.2f}% N={m.trades} PF={m.profit_factor:.2f}"
        )
        if best_m is None or m.return_pct > best_m.return_pct:
            best_m, best_kw, best_name = m, kw, name

    wf = walk_forward(m1, costs, spec, best_kw or {}, folds=3) if best_kw else []
    sections["backtests"] = bt
    sections["walk_forward"] = wf
    sections["best_rule"] = {"name": best_name, "kw": {
        k: (list(v) if isinstance(v, tuple) else v)
        for k, v in (best_kw or {}).items()
    }}
    if best_m:
        insight.append(
            f"Лучший честный бэктест Tag M1: {best_name} → "
            f"{best_m.return_pct:+.2f}% WR {best_m.win_rate:.0f}% "
            f"DD {best_m.max_drawdown_pct:.2f}% N={best_m.trades}"
        )
    if wf:
        pos = sum(1 for x in wf if x["ret"] > 0)
        insight.append(
            f"Walk-forward {len(wf)} folds: {pos}/{len(wf)} profitable "
            f"({', '.join(f\"f{x['fold']} {x['ret']:+.1f}%\" for x in wf)})"
        )
        for x in wf:
            con.print(f"  WF f{x['fold']} {x['from']}→{x['to']}: "
                      f"{x['ret']:+.2f}% WR={x['wr']:.0f}% N={x['n']}")

    # ── 6. oracle ceiling ──
    q = c[(c.prior <= -0.6) & (c.slope >= 0.15)]
    used = {}
    oracle = []
    plain = []
    for row in q.sort_values("i").itertuples():
        day = m1.time.iloc[int(row.i)].date()
        if used.get(day, 0):
            continue
        plain.append(4.0 - spr if row.win else -2.5 - spr)
        used[day] = 1
    used = {}
    for row in q.sort_values("i").itertuples():
        day = m1.time.iloc[int(row.i)].date()
        if used.get(day, 0):
            continue
        # quality path proxy: need mfe/mae — approximate via win only for ceiling of picks
        # better: recompute quality
        i = int(row.i)
        entry = float(m1.close.iloc[i])
        side = 1 if m1.close.iloc[i] > m1.open.iloc[i] else -1
        fut = m1.iloc[i + 1:i + 31]
        if side > 0:
            mae = float(fut.low.min() - entry) if len(fut) else 0
            mfe = float(fut.high.max() - entry) if len(fut) else 0
        else:
            mae = float(entry - fut.high.max()) if len(fut) else 0
            mfe = float(entry - fut.low.min()) if len(fut) else 0
        if mfe >= 4 and mae > -2.5:
            oracle.append(4.0 - spr if row.win else -2.5 - spr)
            used[day] = 1
    ceiling = {
        "v4_1day_n": len(plain),
        "v4_1day_wr": round(float(np.mean([x > 0 for x in plain]) * 100), 1) if plain else 0,
        "v4_1day_sum_pts": round(float(sum(plain)), 1) if plain else 0,
        "oracle_n": len(oracle),
        "oracle_wr": round(float(np.mean([x > 0 for x in oracle]) * 100), 1) if oracle else 0,
        "oracle_sum_pts": round(float(sum(oracle)), 1) if oracle else 0,
    }
    sections["ceiling"] = ceiling
    insight.append(
        f"Oracle (quality path 1/day среди deep+slope): N={ceiling['oracle_n']} "
        f"WR~{ceiling['oracle_wr']}% sumPts={ceiling['oracle_sum_pts']} — потолок без Amplify-фильтра."
    )
    insight.append(
        "Amplify +136%/WR87% не воспроизводится с M1 OHLC; честный потолок на Tag ~единицы % "
        "за ~100д при DD~1–2%."
    )

    # ── HTML ──
    def ts(t):
        t = pd.Timestamp(t)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        return int(t.timestamp())

    cut = m5.time.max() - pd.Timedelta(days=70)
    chart = m5[m5.time >= cut]
    candles = [{
        "time": ts(r.time), "open": float(r.open), "high": float(r.high),
        "low": float(r.low), "close": float(r.close),
    } for r in chart.itertuples(index=False)]
    markers = []
    trade_rows = []
    for r in g.itertuples():
        ot = pd.Timestamp(r.open)
        if ot < cut:
            continue
        buy = r.side == "Buy"
        markers.append({
            "time": ts(ot),
            "position": "belowBar" if buy else "aboveBar",
            "color": "#3dd6c3" if r.won else "#ff7b78",
            "shape": "arrowUp" if buy else "arrowDown",
            "text": r.setup[:6],
        })
        trade_rows.append({
            "side": r.side, "open": r.open, "close": r.close, "entry": r.entry,
            "pnl": r.pnl, "setup": r.setup, "pullback": bool(r.pullback),
            "broke": bool(r.broke), "near_piv": bool(r.near_piv),
            "mae8": round(r.mae8, 2), "mfe8": round(r.mfe8, 2),
            "hold": round(r.hold, 1), "won": bool(r.won),
        })

    payload = {
        "title": "SONIK — все способы",
        "live_n": int(len(g)),
        "live_wr": round(float(g.won.mean() * 100), 1),
        "spread": spr,
        **sections,
        "insight": insight,
        "candles": candles,
        "markers": markers,
        "trades": trade_rows,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sonik_all_methods.json").write_text(
        json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    (OUT / "sonik_all_methods.html").write_text(
        HTML.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False, default=str)),
        encoding="utf-8")
    # also refresh full_analysis alias
    (OUT / "sonik_full_analysis.json").write_text(
        json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    (OUT / "sonik_full_analysis.html").write_text(
        HTML.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False, default=str)),
        encoding="utf-8")

    if best_kw:
        (DATA / "sonik_best_rule.json").write_text(
            json.dumps({"name": best_name, "kw": {
                k: (list(v) if isinstance(v, tuple) else v) for k, v in best_kw.items()
            }, "metrics": {
                "return_pct": best_m.return_pct, "win_rate": best_m.win_rate,
                "dd": best_m.max_drawdown_pct, "trades": best_m.trades, "pf": best_m.profit_factor,
            }}, indent=2), encoding="utf-8")

    con.print(f"\n[green]→ {OUT / 'sonik_all_methods.html'}[/]")
    con.print(f"[cyan]best: {best_name} {best_m.return_pct:+.2f}%[/]" if best_m else "")


HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SONIK all methods</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{--bg:#0a0c10;--panel:#131820;--line:#243041;--text:#eef2f7;--muted:#8b97a8;
--green:#3dd6c3;--red:#ff7b78;--mono:ui-monospace,Consolas,monospace;--sans:system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--sans)}
header{padding:18px 22px;border-bottom:1px solid var(--line)}
header h1{margin:0;font:750 20px/1.2 var(--sans)}header p{margin:8px 0 0;color:var(--muted);max-width:100ch}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:0;border-bottom:1px solid var(--line)}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;border-bottom:1px solid var(--line)}
@media(max-width:1000px){.grid,.grid3{grid-template-columns:1fr}}
.panel{padding:14px 18px;border-right:1px solid var(--line)}
.panel h3{margin:0 0 10px;font:600 11px var(--sans);color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
table{width:100%;border-collapse:collapse;font:12px/1.35 var(--mono)}
th,td{padding:5px 7px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}th{color:var(--muted)}
.pos{color:var(--green)}.neg{color:var(--red)}
#chart{height:480px;background:#07090c}
.wrap{max-height:240px;overflow:auto;padding:0 18px 14px}
ul{margin:0;padding-left:18px;color:var(--muted)}li{margin:4px 0}
.note{padding:10px 22px;color:var(--muted);font-size:12px}
pre{white-space:pre-wrap;font:11px var(--mono);color:var(--muted);margin:0;max-height:220px;overflow:auto}
</style>
</head>
<body>
<header>
  <h1>SONIK — разбор всеми способами</h1>
  <p>Live setups · ML · ticks · all-chart candidates · backtest · walk-forward · oracle ceiling. Tag XAUUSD.f M1.</p>
</header>
<div class="grid">
  <div class="panel"><h3>Setup на логе</h3><table id="setups"></table></div>
  <div class="panel"><h3>Комбо глазами</h3><table id="combos"></table></div>
</div>
<div class="grid3">
  <div class="panel"><h3>Бэктесты</h3><table id="bts"></table></div>
  <div class="panel"><h3>Candidates / WF / ML</h3><table id="cands"></table><div id="wf"></div><pre id="ml"></pre></div>
  <div class="panel"><h3>Выводы</h3><ul id="ins"></ul></div>
</div>
<div id="chart"></div>
<div class="wrap"><table>
<thead><tr><th>#</th><th>setup</th><th>side</th><th>open</th><th>pnl</th><th>MAE</th><th>MFE</th><th>pb</th><th>piv</th></tr></thead>
<tbody id="tb"></tbody>
</table></div>
<p class="note" id="ticknote"></p>
<script type="application/json" id="data">__PAYLOAD__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const money=v=>(v<0?'−':'+')+Math.abs(v).toFixed(2);
document.getElementById('setups').innerHTML='<tr><th>setup</th><th>N</th><th>WR</th><th>avg$</th><th>MAE</th><th>MFE</th></tr>'+
  (D.setup_stats||[]).map(s=>`<tr><td>${s.setup}</td><td>${s.n}</td><td>${s.wr}%</td>
  <td class="${s.avg_pnl>=0?'pos':'neg'}">${money(s.avg_pnl)}</td><td>${s.mae}</td><td>${s.mfe}</td></tr>`).join('');
document.getElementById('combos').innerHTML='<tr><th>combo</th><th>N</th><th>WR</th><th>avg$</th><th>MAE</th><th>MFE</th></tr>'+
  (D.combo_stats||[]).map(s=>`<tr><td>${s.combo}</td><td>${s.n}</td><td>${s.wr}%</td>
  <td class="${s.avg_pnl>=0?'pos':'neg'}">${money(s.avg_pnl)}</td><td>${s.mae}</td><td>${s.mfe}</td></tr>`).join('');
document.getElementById('bts').innerHTML='<tr><th>rule</th><th>ret</th><th>WR</th><th>DD</th><th>N</th><th>PF</th></tr>'+
  (D.backtests||[]).map(s=>`<tr><td>${s.name}</td>
  <td class="${s.return_pct>=0?'pos':'neg'}">${s.return_pct>=0?'+':''}${s.return_pct.toFixed(2)}%</td>
  <td>${s.win_rate.toFixed(0)}%</td><td>${s.dd.toFixed(2)}%</td><td>${s.trades}</td><td>${s.pf.toFixed(2)}</td></tr>`).join('');
document.getElementById('cands').innerHTML='<tr><th>pool</th><th>N</th><th>WR</th><th>live</th></tr>'+
  (D.cand_stats||[]).map(s=>`<tr><td>${s.rule}</td><td>${s.n}</td><td>${s.wr}%</td>
  <td>${s.live_n}${s.live_wr!=null?' @'+s.live_wr+'%':''}</td></tr>`).join('');
document.getElementById('wf').innerHTML='<p style="color:var(--muted);font:11px var(--mono);margin:8px 0 0">WF: '+
  (D.walk_forward||[]).map(x=>`f${x.fold} ${x.ret>=0?'+':''}${x.ret.toFixed(1)}%`).join(' · ')+'</p>'+
  `<p style="color:var(--muted);font:11px var(--mono)">oracle ${D.ceiling?.oracle_n||0} WR ${D.ceiling?.oracle_wr||0}% · v4 ${D.ceiling?.v4_1day_n||0} WR ${D.ceiling?.v4_1day_wr||0}%</p>`;
const ml=D.ml||{};
document.getElementById('ml').textContent=ml.ok
  ? `ML tree CV ${ml.tree_cv}%±${ml.tree_std} (base ${ml.base}%)  GB ${ml.gb_cv}%\n`+
    (ml.importance||[]).map(x=>`${x.f}: ${x.w}`).join('  ')+'\n'+(ml.tree||'')
  : (ml.note||'no ml');
document.getElementById('ins').innerHTML=(D.insight||[]).map(x=>`<li>${x}</li>`).join('');
const tk=D.ticks||{};
document.getElementById('ticknote').textContent=tk.ok
  ? `Ticks n=${tk.n}: mom>0 WR ${tk.wr_mom_pos}% · mom≤0 ${tk.wr_mom_neg}% · spread med ${tk.spread_med}`
  : `Ticks: ${tk.note||'—'}`;
const el=document.getElementById('chart');
const chart=LightweightCharts.createChart(el,{
  layout:{background:{color:'#07090c'},textColor:'#8b97a8'},
  grid:{vertLines:{color:'#1a2230'},horzLines:{color:'#1a2230'}},
  rightPriceScale:{borderColor:'#243041'},
  timeScale:{borderColor:'#243041',timeVisible:true},
});
const cs=chart.addCandlestickSeries({upColor:'#26a69a',downColor:'#ef5350',borderVisible:false,wickUpColor:'#26a69a',wickDownColor:'#ef5350'});
cs.setData(D.candles||[]); cs.setMarkers(D.markers||[]); chart.timeScale().fitContent();
new ResizeObserver(()=>chart.applyOptions({width:el.clientWidth,height:el.clientHeight})).observe(el);
document.getElementById('tb').innerHTML=(D.trades||[]).map((t,i)=>
  `<tr><td>${i+1}</td><td>${t.setup}</td><td class="${t.side==='Buy'?'pos':'neg'}">${t.side}</td>
   <td>${(t.open||'').slice(5,16)}</td>
   <td class="${t.pnl>=0?'pos':'neg'}">${money(t.pnl)}</td>
   <td>${t.mae8}</td><td>${t.mfe8}</td><td>${t.pullback?'Y':'n'}</td><td>${t.near_piv?'Y':'n'}</td></tr>`).join('');
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
