"""Шаг 1 плана ML-фильтра: датасет «признаки на баре входа → исход сделки».

Бэктест уже даёт исход (pnl/reason), но не то, что видели индикаторы в
момент входа — агрегаты (win_rate, PF) это стирают. Здесь для каждой
закрытой сделки берём df.iloc[trade.opened_at] — тот самый бар, на котором
стратегия решила войти — и сохраняем его показания рядом с исходом.

Признаки для каждой стратегии свои (у HSS это doji/объём, у Flow — ATR-
нормированные расстояния до VWAP/ORB) и переводятся в масштаб-независимый
вид: сырые цены/ATR в JSON не идут, только то, что сравнимо между BTC
за 60000$ и PEPE за 0.000004$.

    python scripts/ml_dataset.py --symbols ETH/USDT:USDT,SOL/USDT:USDT --bars 40000
    python scripts/ml_dataset.py  # вся вселенная, глубина по умолчанию
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from rich.console import Console

from neft.backtest import crypto_data
from neft.backtest.engine import Backtester, Costs
from neft.core.risk import RiskLimits, RiskManager
from neft.core.routing import CRYPTO_UNIVERSE
from neft.strategies.factory import crypto_strategy
from scripts.compare_crypto import spec_for

con = Console()
BAL = 1000.0

# Одни и те же связки, что в compare_crypto.py — те же данные, тот же смысл.
CONFIGS = [
    ("HSS", "1m", None), ("HSS", "1m", (16, 19)),
    ("Flow", "5m", None),
    ("London S/R", "5m", None), ("London S/R", "5m", (16, 19)),
    ("London S/R", "1m", None),
    ("Squeeze", "5m", None),
    ("Breakout", "5m", None),
]


def _safe(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def features_at(kind: str, row) -> dict:
    """Масштаб-независимый снимок бара входа — свой набор на стратегию."""
    g = lambda name: _safe(getattr(row, name, None))  # noqa: E731
    out: dict[str, float] = {}
    if kind == "HSS":
        for k in ("body_ratio", "top_wick_ratio", "bot_wick_ratio",
                   "bars_since_cross", "pullbacks_done"):
            v = g(k)
            if v is not None:
                out[k] = v
        for k in ("ha_bull", "is_doji", "clean_bull", "clean_bear",
                   "big_doji", "high_volume", "small_doji"):
            v = getattr(row, k, None)
            if v is not None:
                out[k] = float(bool(v))
    elif kind == "Flow":
        atr = g("atr") or 0.0
        close = g("close")
        h = g("hour")
        if h is not None:
            out["hour"] = h
        if atr and close is not None:
            for k in ("ny_vwap", "vwap", "orb_high", "orb_low", "dc_high", "dc_low"):
                v = g(k)
                if v is not None:
                    out[f"dist_{k}_atr"] = (close - v) / atr
        atr_slow = g("atr_slow")
        if atr and atr_slow:
            out["vol_expansion"] = atr / atr_slow
        for k in ("asia_ready", "orb_ready"):
            v = getattr(row, k, None)
            if v is not None:
                out[k] = float(bool(v))
    elif kind in ("London S/R", "Squeeze"):
        hi, lo, close = g("last_high"), g("last_low"), g("close")
        h = g("hour")
        if h is not None:
            out["hour"] = h
        if hi is not None and lo is not None and close is not None and hi != lo:
            rng = hi - lo
            out["pos_in_range"] = (close - lo) / rng
            out["dist_to_high"] = (hi - close) / rng
            out["dist_to_low"] = (close - lo) / rng
    elif kind == "Breakout":
        h = g("hour")
        if h is not None:
            out["hour"] = h
    return out


def collect(symbol: str, bars: int) -> list[dict]:
    rows: list[dict] = []
    for kind, tf, session in CONFIGS:
        try:
            df = crypto_data.load(symbol, tf, bars)
        except Exception as e:  # noqa: BLE001
            con.print(f"[dim]{symbol} {tf}: {e}[/]")
            continue
        if df is None or len(df) < 300:
            continue
        spec = spec_for(symbol, float(df.close.median()))
        rm = RiskManager(start_balance=BAL, limits=RiskLimits(
            risk_per_trade_pct=0.75, max_risk_per_trade_pct=3.0, max_volume=1e6,
            max_daily_loss_pct=100.0, max_drawdown_pct=100.0, min_free_margin_pct=0.0))
        strat = crypto_strategy({"strategy": kind, "tf": tf, "session": session},
                                rr=1.5, risk=0.75, risk_manager=rm, spec=spec)
        px = float(df.close.median())
        costs = Costs(spread_points=spec.default_spread, contract_size=spec.contract_size,
                      point=spec.point,
                      commission_per_lot=crypto_data.TAKER_FEE * px * spec.contract_size,
                      commission_maker_per_lot=crypto_data.MAKER_FEE * px * spec.contract_size)
        prepared = strat.prepare(df.copy()).reset_index(drop=True)
        res = Backtester(strat, rm, costs, start_balance=BAL).run(df)
        # ClosedTrade.opened_at — время бара (см. engine.py: opened_at=pos.
        # opened_time), не позиционный индекс. Ищем строку по времени.
        time_idx = pd.Index(prepared["time"])
        for t in res.trades:
            if t.opened_at is None:
                continue
            pos_ix = time_idx.get_indexer([pd.Timestamp(t.opened_at)])[0]
            if pos_ix < 0:
                continue
            row = prepared.iloc[pos_ix]
            feats = features_at(kind, row)
            if not feats:
                continue
            risk_amt = BAL * 0.0075
            rows.append({
                "symbol": symbol, "kind": kind, "tf": tf,
                "session": "16-19" if session else "круглосуточно",
                "time": str(row.time), "reason": t.reason,
                "pnl": round(t.pnl, 6),
                "win": 1 if t.pnl > 0 else 0,
                "r_multiple": round(t.pnl / risk_amt, 4) if risk_amt else 0.0,
                "features": feats,
            })
    return rows


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default=",".join(CRYPTO_UNIVERSE))
    p.add_argument("--bars", type=int, default=40_000)
    p.add_argument("--out", default="logs/ml_dataset.jsonl")
    args = p.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    out_path = Path(args.out)
    out_path.parent.mkdir(exist_ok=True)
    total = 0
    with out_path.open("w", encoding="utf-8") as f:
        for i, sym in enumerate(symbols, 1):
            rows = collect(sym, args.bars)
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            total += len(rows)
            con.print(f"[dim]{i}/{len(symbols)} {sym}: {len(rows)} сделок, всего {total}[/]")
    con.print(f"[green]готово: {total} сделок -> {out_path}[/]")


if __name__ == "__main__":
    main()
