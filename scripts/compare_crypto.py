"""Сравнение стратегий на крипте: M1 против M5, много пар.

Крипта торгуется круглосуточно, поэтому сессионные фильтры проверяются
отдельно — с ними и без них. На традиционных рынках они дают эдж, здесь
это надо доказать, а не предполагать.
"""
import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from rich.console import Console
from rich.table import Table

from neft.backtest import crypto_data, metrics
from neft.backtest.engine import Backtester, Costs
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.factory import crypto_strategy

con = Console()
BAL = 1000.0


@dataclass(frozen=True)
class CryptoSpec:
    """Спецификация под интерфейс, который ждут стратегии."""
    name: str
    point: float
    digits: int = 2
    contract_size: float = 1.0
    volume_min: float = 0.001
    volume_step: float = 0.001
    volume_max: float = 1000.0
    spread: int = 1

    @property
    def default_spread(self) -> float:
        return float(self.spread)


def spec_for(symbol: str, price: float) -> CryptoSpec:
    point = crypto_data._point_for(price)
    step = 0.001 if price >= 1000 else 0.01 if price >= 10 else 1.0
    return CryptoSpec(name=symbol, point=point,
                      volume_min=step, volume_step=step)


def evaluate(symbol, kind, tf, bars, risk=0.75, session=None):
    df = crypto_data.load(symbol, tf, bars)
    spec = spec_for(symbol, float(df.close.median()))
    rm = RiskManager(start_balance=BAL, limits=RiskLimits(
        risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0, max_volume=1e6,
        max_daily_loss_pct=100.0, max_drawdown_pct=100.0, min_free_margin_pct=0.0))

    # Все стратегии — через factory, чтобы session доходил до Squeeze/Breakout/Flow.
    strat = crypto_strategy(
        {"strategy": kind, "tf": tf, "session": session},
        rr=1.5 if kind in ("Breakout", "Squeeze") else 1.0,
        risk=risk, risk_manager=rm, spec=spec)

    px = float(df.close.median())
    costs = Costs(spread_points=spec.default_spread,
                  contract_size=spec.contract_size, point=spec.point,
                  commission_per_lot=crypto_data.TAKER_FEE * px * spec.contract_size,
                  commission_maker_per_lot=crypto_data.MAKER_FEE * px * spec.contract_size)
    res = Backtester(strat, rm, costs, start_balance=BAL).run(df)
    m = metrics.compute(res.equity, res.trades, BAL, res.ruined)
    days = (df.time.iloc[-1] - df.time.iloc[0]).days or 1
    return {
        "symbol": symbol, "kind": kind, "tf": tf,
        "session": "16-19" if session else "круглосуточно",
        "from": str(df.time.iloc[0]), "to": str(df.time.iloc[-1]), "days": days,
        "trades": m.trades, "win_rate": m.win_rate, "pf": m.profit_factor,
        "dd": m.max_drawdown_pct, "ret": m.return_pct,
        "per_month": m.return_pct / (days / 30.44),
        "trades_week": m.trades / (days / 7),
        "expectancy": m.expectancy, "streak": m.max_loss_streak,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", required=True)
    p.add_argument("--bars", type=int, default=40_000)
    p.add_argument("--risk", type=float, default=0.75)
    p.add_argument("--min-trades", type=int, default=15)
    p.add_argument("--json", default="logs/session_filter_crypto.json")
    a = p.parse_args()

    if a.symbols.strip().lower() in ("universe", "all", "*"):
        from neft.core.routing import CRYPTO_UNIVERSE
        a.symbols = ",".join(s for s in CRYPTO_UNIVERSE if "TON" not in s)

    syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    plan = []
    # П.5 плана: Flow / Squeeze / Breakout раньше почти не гонялись с сессией.
    # HSS и London S/R уже сравнивали 24/7 vs 16-19 — здесь фокус на тройке.
    for tf in ("5m", "15m"):
        for kind in ("Flow", "Squeeze", "Breakout"):
            for sess in (None, (16, 19)):
                plan.append((tf, kind, sess))

    rows = []
    for sym in syms:
        for tf, kind, sess in plan:
            try:
                r = evaluate(sym, kind, tf, a.bars, a.risk, sess)
            except Exception as e:
                con.print(f"[dim]{sym} {kind} {tf}: {type(e).__name__}: {e}[/]")
                continue
            rows.append(r)
            con.print(f"[dim]{sym:20s} {kind:11s} {tf:3s} {r['session']:14s} "
                      f"{r['trades']:>4} сд  {r['win_rate']:5.1f}%  "
                      f"{r['ret']:+8.2f}%[/]")

    Path(a.json).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    # Сводка: дельта сессии vs круглосуточно на одной связке symbol|kind|tf.
    pairs = {}
    for r in rows:
        key = (r["symbol"], r["kind"], r["tf"])
        pairs.setdefault(key, {})[r["session"]] = r
    deltas = []
    for key, by in pairs.items():
        a24 = by.get("круглосуточно")
        a16 = by.get("16-19")
        if not a24 or not a16:
            continue
        deltas.append({
            "symbol": key[0], "kind": key[1], "tf": key[2],
            "ret_24": a24["ret"], "ret_16": a16["ret"],
            "delta": a16["ret"] - a24["ret"],
            "tr_24": a24["trades"], "tr_16": a16["trades"],
            "pf_24": a24["pf"], "pf_16": a16["pf"],
        })
    deltas.sort(key=lambda x: -x["delta"])
    Path(a.json.replace(".json", "_delta.json")).write_text(
        json.dumps(deltas, ensure_ascii=False, indent=2), encoding="utf-8")

    good = [r for r in rows if r["trades"] >= a.min_trades]
    good.sort(key=lambda r: -r["per_month"])
    t = Table(title=f"СЕССИЯ Flow/Squeeze/Breakout · ${BAL:.0f} · риск {a.risk}%",
              box=None, pad_edge=False, title_style="bold cyan")
    for c in ("пара", "стратегия", "ТФ", "сессия", "дней", "сделок", "в нед.",
              "винрейт", "PF", "просадка", "% в мес", "% всего"):
        t.add_column(c, justify="right" if c not in ("пара", "стратегия", "ТФ", "сессия") else "left")
    for r in good[:40]:
        wc = "green" if r["win_rate"] > 50 else "yellow"
        dc = "green" if r["dd"] <= 5 else "red"
        c = "green" if r["ret"] > 0 else "red"
        t.add_row(r["symbol"].replace("/USDT:USDT", ""), r["kind"], r["tf"],
                  r["session"], str(r["days"]), str(r["trades"]),
                  f"{r['trades_week']:.1f}", f"[{wc}]{r['win_rate']:.1f}%[/]",
                  f"{r['pf']:.2f}", f"[{dc}]{r['dd']:.1f}%[/]",
                  f"[{c}]{r['per_month']:+.2f}%[/]", f"[{c}]{r['ret']:+.2f}%[/]")
    con.print(t)

    dtab = Table(title="Дельта 16-19 минус круглосуточно (выше = сессия помогла)",
                 box=None, pad_edge=False, title_style="bold yellow")
    for c in ("пара", "страт", "ТФ", "ret 24/7", "ret 16-19", "Δ%", "сд 24→16", "PF 24→16"):
        dtab.add_column(c, justify="right" if c not in ("пара", "страт", "ТФ") else "left")
    for d in deltas[:30]:
        col = "green" if d["delta"] > 0 else "red"
        dtab.add_row(
            d["symbol"].replace("/USDT:USDT", ""), d["kind"], d["tf"],
            f"{d['ret_24']:+.1f}%", f"{d['ret_16']:+.1f}%",
            f"[{col}]{d['delta']:+.1f}%[/]",
            f"{d['tr_24']}→{d['tr_16']}",
            f"{d['pf_24']:.2f}→{d['pf_16']:.2f}")
    con.print(dtab)
