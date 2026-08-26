"""HSS на BTC: M5 / M15 / H1, R:R 1:1.5, плечо 100x.

Комиссия — крипто-тейкер Bybit 0.055% на вход и на выход (и SL, и TP).
Не TradFi $/лот.

    python scripts/hss_btc_report.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from rich.console import Console
from rich.table import Table

from neft.backtest import crypto_data, data as ohlc, metrics
from neft.backtest.engine import Backtester, Costs
from neft.core import machine_config
from neft.core.config import ROOT
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.scalp_ha import ScalpHA
from compare_crypto import spec_for

from hss_cfd_report import (
    OVERVIEW_RULE, _candles, _json_dump, _markers, _trade_rows,
)

con = Console()
OUT = ROOT / "dashboard"
SYMBOL = "BTC/USDT:USDT"
LEVERAGE = 100
DAYS = 90
TF_RULE = {"M5": "5min", "M15": "15min", "H1": "1h"}
CHARTS = {
    "M5": OUT / "hss_charts_btc_m5",
    "M15": OUT / "hss_charts_btc_m15",
    "H1": OUT / "hss_charts_btc_h1",
}
REPORT = {
    "M5": "hss_report_btc_m5.json",
    "M15": "hss_report_btc_m15.json",
    "H1": "hss_report_btc_h1.json",
}


def _load_btc(days: int) -> tuple[pd.DataFrame, str]:
    last_err = None
    for ex in ("bybit", "binance"):
        try:
            df = crypto_data.load(SYMBOL, "5m", days=days, exchange_id=ex)
            if len(df):
                return df, ex
        except Exception as e:
            last_err = e
            con.print(f"[yellow]{ex}: {e}[/]")
    raise RuntimeError(f"нет истории BTC: {last_err}")


def _crypto_costs(spec, px: float, leverage: int) -> Costs:
    """Тейкер 0.055% на обе стороны — как просил, не мейкер на TP."""
    taker = crypto_data.TAKER_FEE * float(px) * float(spec.contract_size)
    return Costs(
        spread_points=spec.default_spread,
        contract_size=float(spec.contract_size),
        point=float(spec.point),
        commission_per_lot=taker,
        commission_maker_per_lot=taker,
        commission_on_close=True,
        leverage=int(leverage),
    )


def run_btc(df: pd.DataFrame, *, balance: float, risk: float, rr: float,
            pullback: int, session, ema: int, leverage: int,
            lite: bool = False, ta_filter: str = "full") -> dict:
    px = float(df.close.median())
    spec = spec_for(SYMBOL, px)
    limits = RiskLimits(
        risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
        max_volume=1e6, max_daily_loss_pct=100.0,
        max_drawdown_pct=100.0, min_free_margin_pct=0.0,
    )
    rm = RiskManager(start_balance=balance, limits=limits)
    # Раунд-трип тейкер: вход + выход, чтобы мелкие doji отсекались.
    taker_rt = 2.0 * crypto_data.TAKER_FEE * px * spec.contract_size
    strat = ScalpHA(
        ema_period=ema, pullback_bars=pullback, rr=rr,
        vol_mode="min", vol_window=3, entry_mode="market",
        setup_mode="sr_doji", ta_filter=ta_filter,
        require_structure=False, block_after_small_doji=False,
        session=session, risk_pct=risk,
        risk_manager=rm, spec=spec, commission_per_lot=taker_rt,
    )
    costs = _crypto_costs(spec, px, leverage)
    res = Backtester(strat, rm, costs, start_balance=balance, symbol=SYMBOL).run(df)
    m = metrics.compute(res.equity, res.trades, balance, res.ruined)
    days = max(1, (df.time.iloc[-1] - df.time.iloc[0]).days)
    out = {
        "symbol": "BTCUSDT",
        "bars": len(df),
        "from": str(df.time.iloc[0]),
        "to": str(df.time.iloc[-1]),
        "days": days,
        "metrics": m.as_dict(),
        "setups": int(getattr(strat, "setups_seen", 0) or 0),
        "expired": int(res.expired),
        "rejected": len(res.rejected),
        "skipped_session": int(getattr(strat, "skipped_session", 0) or 0),
        "skipped_fee": int(getattr(strat, "skipped_fee", 0) or 0),
        "skipped_ta": int(getattr(strat, "skipped_ta", 0) or 0),
        "median_px": px,
        "taker_pct": crypto_data.TAKER_FEE * 100,
        "taker_rt_per_btc": taker_rt,
    }
    if lite:
        out["trades"] = []
        out["markers"] = []
        return out
    out["trades"] = _trade_rows(res.trades, strat.df)
    out["markers"] = _markers(res.trades, strat.df)
    out["_df"] = df
    return out


def write_chart(df_trade: pd.DataFrame, markers: list, trades: list,
                metrics_d: dict, trade_tf: str) -> str:
    dest = CHARTS[trade_tf]
    dest.mkdir(parents=True, exist_ok=True)
    overview = ohlc.resample_ohlc(df_trade, OVERVIEW_RULE.get(trade_tf, "15min"))
    payload = {
        "symbol": "BTCUSDT",
        "tf": trade_tf,
        "metrics": metrics_d,
        "trades": trades,
        "markers": markers,
        "candles_trade": _candles(df_trade),
        "candles_overview": _candles(overview),
        "period": [str(df_trade.time.iloc[0]), str(df_trade.time.iloc[-1])],
        "candles_m5": _candles(df_trade),
        "candles_m15": _candles(overview),
        "candles_m1": [],
    }
    (dest / "BTCUSDT.json").write_text(_json_dump(payload), encoding="utf-8")
    return f"{dest.name}/BTCUSDT.json"


def _summ(r: dict) -> dict:
    m = r["metrics"]
    return {
        "trades": m["trades"], "wins": m["wins"], "losses": m["losses"],
        "win_rate": m["win_rate"], "net_profit": round(m["net_profit"], 2),
        "return_pct": m["return_pct"], "profit_factor": m["profit_factor"],
        "max_drawdown_pct": m["max_drawdown_pct"],
        "setups": r.get("setups", 0), "skipped_fee": r.get("skipped_fee", 0),
        "skipped_ta": r.get("skipped_ta", 0),
    }


def _write_report(exp: dict, r: dict, *, balance: float, risk: float,
                  ema: int, exchange: str) -> None:
    m = r["metrics"]
    trade_tf = exp["tf"]
    chart = write_chart(r.pop("_df"), r["markers"], r["trades"], m, trade_tf)
    report = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "strategy": "HSS",
        "days": DAYS,
        "params": {
            "tf": trade_tf, "balance": balance, "risk_pct": risk, "rr": exp["rr"],
            "pullback": 2, "ema": ema, "session": exp["sess_label"],
            "entry_mode": "market", "vol_mode": "min",
            "setup_mode": "sr_doji", "ta_filter": exp.get("ta_filter", "full"),
            "symbol": SYMBOL, "leverage": LEVERAGE, "exchange": exchange,
            "fees": "crypto_taker", "taker_pct": crypto_data.TAKER_FEE * 100,
            "variant": exp["id"], "variant_title": exp["title"],
        },
        "summary": {
            "symbols": 1, "errors": 0, "trades": m["trades"],
            "wins": m["wins"], "losses": m["losses"],
            "win_rate": m["win_rate"],
            "net_profit": round(m["net_profit"], 2),
            "return_pct_sum": round(m["return_pct"], 2),
        },
        "books": [{
            "symbol": "BTCUSDT", "broker": SYMBOL,
            "from": r.get("from"), "to": r.get("to"),
            "days": r.get("days"), "bars": r.get("bars"),
            "metrics": m, "setups": r.get("setups"),
            "chart": chart, "error": None,
        }],
    }
    path = OUT / REPORT[trade_tf]
    path.write_text(_json_dump(report), encoding="utf-8")
    con.print(f"[dim]отчёт {trade_tf} → {path.name}[/]")


def main() -> None:
    cfg = machine_config.load()
    hss = (cfg.get("strategies") or {}).get("hss") or {}
    balance = float(cfg.get("deposit") or 1000)
    risk = float(cfg.get("risk_pct") or 0.5)
    ema = int(hss.get("ema") or 100)

    con.print(f"[cyan]BTC {DAYS}д · crypto HSS doji+S/R · {LEVERAGE}x · "
              f"тейкер {crypto_data.TAKER_FEE*100:.3f}% × 2[/]")
    df5, exchange = _load_btc(DAYS)
    end = df5.time.iloc[-1]
    df5 = df5[df5.time >= end - pd.Timedelta(days=DAYS)].reset_index(drop=True)
    con.print(f"[dim]{exchange} · {len(df5)} баров 5m · "
              f"{df5.time.iloc[0]} — {df5.time.iloc[-1]}[/]")

    frames = {
        "M5": df5,
        "M15": ohlc.resample_ohlc(df5, TF_RULE["M15"]),
        "H1": ohlc.resample_ohlc(df5, TF_RULE["H1"]),
    }

    experiments = []
    for tf in ("M5", "M15", "H1"):
        for rr in (1.5, 2.0):
            for ta, ta_title in (("full", "ТА RSI+room"), ("off", "без RSI")):
                rid = f"{tf.lower()}_rr{str(rr).replace('.', '')}_{ta}"
                experiments.append(dict(
                    id=rid,
                    title=f"BTC {tf} · 1:{rr} · 24ч · {ta_title} · 100x",
                    tf=tf, rr=rr, session=None, sess_label="24h",
                    ta_filter=ta,
                ))

    results = []
    for exp in experiments:
        df = frames[exp["tf"]]
        con.print(f"\n[cyan]▸ {exp['id']}[/] {exp['title']}")
        r = run_btc(
            df, balance=balance, risk=risk, rr=float(exp["rr"]), pullback=2,
            session=exp["session"], ema=ema, leverage=LEVERAGE, lite=True,
            ta_filter=exp.get("ta_filter", "full"),
        )
        s = _summ(r)
        rec = {"id": exp["id"], "title": exp["title"], "tf": exp["tf"],
               "params": {"rr": exp["rr"], "session": exp["sess_label"],
                          "leverage": LEVERAGE,
                          "ta_filter": exp.get("ta_filter", "full")},
               "summary": s}
        results.append(rec)
        c = "green" if s["net_profit"] >= 0 else "red"
        con.print(
            f"  [{c}]N {s['trades']} · лузеров {s['losses']} · "
            f"WR {s['win_rate']:.1f}% · Σ ${s['net_profit']:+,.2f}[/]"
        )

    t = Table(title="crypto HSS · doji+S/R · BTC · тейкер 0.055% · 100x · 90д",
              box=None, pad_edge=False, title_style="bold cyan")
    for col in ("вариант", "ТФ", "R:R", "ТА", "сделок", "лузеров", "WR%", "Σ PnL"):
        t.add_column(col, justify="right" if col != "вариант" else "left")
    for e in sorted(results, key=lambda x: -x["summary"]["net_profit"]):
        s = e["summary"]
        c = "green" if s["net_profit"] >= 0 else "red"
        t.add_row(e["id"], e["tf"], f"1:{e['params']['rr']}",
                  e["params"]["ta_filter"],
                  str(s["trades"]), str(s["losses"]),
                  f"{s['win_rate']:.1f}",
                  f"[{c}]{s['net_profit']:+.2f}[/]")
    con.print(t)

    winners = {}
    for tf in ("M5", "M15", "H1"):
        pool = [e for e in results if e["tf"] == tf]
        best = max(pool, key=lambda e: (
            e["summary"]["net_profit"],
            e["summary"]["win_rate"],
            e["summary"]["trades"],
        ))
        winners[tf] = best
        con.print(f"[bold]лучший {tf}:[/] {best['id']} · "
                  f"WR {best['summary']['win_rate']:.1f}% · "
                  f"${best['summary']['net_profit']:+.2f}")

    (OUT / "hss_btc_compare.json").write_text(_json_dump({
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "symbol": SYMBOL, "leverage": LEVERAGE,
        "setup_mode": "sr_doji",
        "fees": "crypto_taker", "taker_pct": crypto_data.TAKER_FEE * 100,
        "exchange": exchange, "days": DAYS,
        "winners": {k: v["id"] for k, v in winners.items()},
        "experiments": results,
    }), encoding="utf-8")

    for tf, best in winners.items():
        exp = next(x for x in experiments if x["id"] == best["id"])
        df = frames[tf]
        r = run_btc(
            df, balance=balance, risk=risk, rr=float(exp["rr"]), pullback=2,
            session=exp["session"], ema=ema, leverage=LEVERAGE, lite=False,
            ta_filter=exp.get("ta_filter", "full"),
        )
        _write_report(exp, r, balance=balance, risk=risk, ema=ema,
                      exchange=exchange)

    con.print("[dim]http://127.0.0.1:8787/hss_report_btc_m5.html[/]")
    con.print("[dim]http://127.0.0.1:8787/hss_report_btc_m15.html[/]")
    con.print("[dim]http://127.0.0.1:8787/hss_report_btc_h1.html[/]")


if __name__ == "__main__":
    main()
