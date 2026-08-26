"""Volume imbalance + trailing exit probes for SonikPulse."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from rich.console import Console

con = Console()


def first_hit_fixed(df, i, side, sl=2.5, tp=4.0):
    entry = float(df.close.iloc[i])
    for j in range(i + 1, min(i + 90, len(df))):
        if side > 0:
            if df.low.iloc[j] <= entry - sl:
                return "SL", entry - sl, j - i
            if df.high.iloc[j] >= entry + tp:
                return "TP", entry + tp, j - i
        else:
            if df.high.iloc[j] >= entry + sl:
                return "SL", entry + sl, j - i
            if df.low.iloc[j] <= entry - tp:
                return "TP", entry - tp, j - i
    return "NA", entry, 0


def first_hit_trail(df, i, side, sl=2.5, arm=1.5, trail=1.2, tp_cap=8.0):
    """BE/trail: after +arm move SL to entry+be_lock, then trail by `trail`."""
    entry = float(df.close.iloc[i])
    stop = entry - side * sl
    best = entry
    armed = False
    for j in range(i + 1, min(i + 120, len(df))):
        hi, lo = float(df.high.iloc[j]), float(df.low.iloc[j])
        if side > 0:
            best = max(best, hi)
            if not armed and best >= entry + arm:
                armed = True
                stop = max(stop, entry + 0.2)  # lock small BE
            if armed:
                stop = max(stop, best - trail)
            if lo <= stop:
                return "SL", stop, j - i, armed
            if hi >= entry + tp_cap:
                return "TP", entry + tp_cap, j - i, armed
        else:
            best = min(best, lo)
            if not armed and best <= entry - arm:
                armed = True
                stop = min(stop, entry - 0.2)
            if armed:
                stop = min(stop, best + trail)
            if hi >= stop:
                return "SL", stop, j - i, armed
            if lo <= entry - tp_cap:
                return "TP", entry - tp_cap, j - i, armed
    return "NA", entry, 0, armed


def main():
    trades = pd.read_csv("data/sonik_trades.csv")
    trades["open"] = pd.to_datetime(trades["open"])
    m1 = pd.read_csv("data/XAUUSD.f_M1_221d.csv", parse_dates=["time"])
    t = trades[(trades.open >= m1.time.min()) & (trades.open <= m1.time.max())].copy()

    tr = pd.concat([
        m1.high - m1.low,
        (m1.high - m1.close.shift()).abs(),
        (m1.low - m1.close.shift()).abs(),
    ], axis=1).max(axis=1)
    df = m1.copy()
    df["atr"] = tr.rolling(14).mean()
    df["body_atr"] = (df.close - df.open).abs() / df.atr
    df["dir"] = np.sign(df.close - df.open)
    df["broke"] = (
        ((df.dir > 0) & (df.close > df.high.shift(1)))
        | ((df.dir < 0) & (df.close < df.low.shift(1)))
    )
    df["vol_ma"] = df.tick_volume.rolling(20).mean()
    df["vol_r"] = df.tick_volume / df.vol_ma
    rng = (df.high - df.low).replace(0, np.nan)
    df["clv"] = ((df.close - df.low) - (df.high - df.close)) / rng
    df["vol_imbalance"] = df.clv * df.vol_r
    df["up_vol"] = np.where(df.close > df.open, df.tick_volume, 0.0)
    df["dn_vol"] = np.where(df.close < df.open, df.tick_volume, 0.0)
    up5 = pd.Series(df.up_vol).rolling(5).sum()
    dn5 = pd.Series(df.dn_vol).rolling(5).sum()
    df["vol_delta5"] = (up5 - dn5) / (up5 + dn5).replace(0, np.nan)
    df["mins"] = df.time.dt.hour * 60 + df.time.dt.minute
    df["ema21"] = df.close.ewm(span=21, adjust=False).mean()

    def pack(i, side):
        row = df.iloc[i]
        return {
            "vol_r": float(row.vol_r),
            "clv": float(row.clv) * side if row.clv == row.clv else 0.0,
            "imb": float(row.vol_imbalance) * side if row.vol_imbalance == row.vol_imbalance else 0.0,
            "delta5": float(row.vol_delta5) * side if row.vol_delta5 == row.vol_delta5 else 0.0,
        }

    live_rows = []
    for r in t.itertuples():
        i = int(df.time.searchsorted(r.open, side="right") - 1)
        if i < 25:
            continue
        side = 1 if r.side == "Buy" else -1
        f = pack(i, side)
        f["live"] = 1
        live_rows.append(f)
    L = pd.DataFrame(live_rows)

    cand = df[(df.mins.between(420, 600)) & (df.body_atr >= 0.5) & (df.broke)].index.tolist()
    rng_gen = np.random.default_rng(0)
    sample = rng_gen.choice(cand, size=min(500, len(cand)), replace=False)
    rand_rows = []
    for i in sample:
        i = int(i)
        side = 1 if df.close.iloc[i] > df.open.iloc[i] else -1
        f = pack(i, side)
        f["live"] = 0
        rand_rows.append(f)
    R = pd.DataFrame(rand_rows)

    con.print("[bold]LIVE vs RAND volume[/]")
    for c in ["vol_r", "clv", "imb", "delta5"]:
        con.print(
            f"  {c:8} L={L[c].median():7.3f} R={R[c].median():7.3f} "
            f"(mean L={L[c].mean():.3f} R={R[c].mean():.3f})"
        )

    # Candidate grid with v4-like filters + volume thresholds
    rows = []
    for i in range(60, len(df) - 90):
        row = df.iloc[i]
        if not (420 <= row.mins < 510):
            continue
        if row.atr != row.atr or row.body_atr < 0.5 or not bool(row.broke):
            continue
        side = 1 if row.close > row.open else -1
        prior = ((df.close.iloc[i - 1] - df.close.iloc[i - 6]) / row.atr) * side
        if prior > -0.6:
            continue
        slope = ((df.ema21.iloc[i] - df.ema21.iloc[i - 5]) / row.atr) * side
        if slope < 0.15:
            continue
        kind, px, bars = first_hit_fixed(df, i, side)
        if kind == "NA":
            continue
        rows.append({
            "win": 1 if kind == "TP" else 0,
            "delta": float(row.vol_delta5) * side if row.vol_delta5 == row.vol_delta5 else 0.0,
            "vol_r": float(row.vol_r) if row.vol_r == row.vol_r else 1.0,
            "imb": float(row.vol_imbalance) * side if row.vol_imbalance == row.vol_imbalance else 0.0,
            "i": i, "side": side,
        })
    f = pd.DataFrame(rows)
    con.print(f"\n[bold]v4-like candidates[/] N={len(f)} WR={f.win.mean()*100:.1f}%")
    for thr in [0.0, 0.2, 0.4, 0.6]:
        m = f.delta >= thr
        if m.sum() >= 15:
            con.print(f"  delta>={thr}: N={m.sum()} WR={f.loc[m, 'win'].mean()*100:.1f}%")
    for thr in [1.0, 1.2, 1.5, 2.0]:
        m = f.vol_r >= thr
        if m.sum() >= 15:
            con.print(f"  vol_r>={thr}: N={m.sum()} WR={f.loc[m, 'win'].mean()*100:.1f}%")
    for thr in [0.0, 0.5, 1.0, 1.5]:
        m = f.imb >= thr
        if m.sum() >= 15:
            con.print(f"  imb>={thr}: N={m.sum()} WR={f.loc[m, 'win'].mean()*100:.1f}%")

    # Trailing vs fixed on live entries and on v4 candidates
    con.print("\n[bold]Exits on LIVE entries[/]")
    for name, fn in [
        ("fixed 2.5/4", lambda i, s: first_hit_fixed(df, i, s, 2.5, 4.0)),
        ("fixed 2.5/6", lambda i, s: first_hit_fixed(df, i, s, 2.5, 6.0)),
        ("trail arm1.5/tr1.2", lambda i, s: first_hit_trail(df, i, s, 2.5, 1.5, 1.2, 8)[:3]),
        ("trail arm2.0/tr1.5", lambda i, s: first_hit_trail(df, i, s, 2.5, 2.0, 1.5, 10)[:3]),
        ("trail arm1.2/tr1.0", lambda i, s: first_hit_trail(df, i, s, 2.2, 1.2, 1.0, 7)[:3]),
    ]:
        pnls = []
        for r in t.itertuples():
            i = int(df.time.searchsorted(r.open, side="right") - 1)
            if i < 10:
                continue
            side = 1 if r.side == "Buy" else -1
            kind, px, bars = fn(i, side)
            if kind == "NA":
                continue
            entry = float(df.close.iloc[i])
            pnl = (px - entry) * side  # points
            pnls.append(pnl)
        arr = np.array(pnls)
        con.print(
            f"  {name:22} N={len(arr)} WR={(arr>0).mean()*100:.0f}% "
            f"sum={arr.sum():.1f} avg={arr.mean():.2f} med={np.median(arr):.2f}"
        )

    con.print("\n[bold]Exits on v4 candidates (1/day)[/]")
    # dedupe 1/day first signal
    for name, fn in [
        ("fixed 2.5/4", lambda i, s: first_hit_fixed(df, i, s, 2.5, 4.0)),
        ("trail arm1.5/tr1.2", lambda i, s: first_hit_trail(df, i, s, 2.5, 1.5, 1.2, 8)[:3]),
        ("trail arm2/tr1.5 +d0.2", None),
    ]:
        used = {}
        pnls = []
        for row in f.itertuples():
            i, side = int(row.i), int(row.side)
            if name.endswith("+d0.2") and row.delta < 0.2:
                continue
            day = df.time.iloc[i].date()
            if used.get(day, 0) >= 1:
                continue
            if name.endswith("+d0.2"):
                kind, px, bars = first_hit_trail(df, i, side, 2.5, 2.0, 1.5, 10)[:3]
            else:
                kind, px, bars = fn(i, side)
            if kind == "NA":
                continue
            entry = float(df.close.iloc[i]) + side * 0.16  # spread
            pnl = (px - entry) * side
            pnls.append(pnl)
            used[day] = used.get(day, 0) + 1
        arr = np.array(pnls)
        if len(arr):
            con.print(
                f"  {name:22} N={len(arr)} WR={(arr>0).mean()*100:.0f}% "
                f"sum={arr.sum():.1f} avg={arr.mean():.2f}"
            )


if __name__ == "__main__":
    main()
