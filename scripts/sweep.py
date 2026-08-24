"""Собирает все прогоны в один JSON для панели управления."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

from neft.backtest import data, metrics
from neft.backtest.engine import Backtester, Costs
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.martingale import Martingale

def run(df, balance=1000.0, steps=10, mult=2.0, lot=0.01, tp=10, sl=10,
        risk_pct=None, max_risk=100.0):
    limits = RiskLimits(
        risk_per_trade_pct=risk_pct or 1.0, max_risk_per_trade_pct=max_risk,
        max_volume=10.0, max_daily_loss_pct=100.0,
        max_drawdown_pct=100.0, min_free_margin_pct=0.0,
    )
    risk = RiskManager(start_balance=balance, limits=limits)
    strat = Martingale(base_volume=lot, risk_pct=risk_pct, risk_manager=risk,
                       tp_pips=tp, sl_pips=sl, multiplier=mult, max_steps=steps)
    res = Backtester(strat, risk, Costs(spread_points=1.0),
                     start_balance=balance).run(df)
    m = metrics.compute(res.equity, res.trades, balance, res.ruined)
    return strat, res, m


def curve(res, points=900):
    eq = res.equity
    step = max(1, len(eq) // points)
    return [{"t": str(i), "v": round(float(v), 2)} for i, v in eq.iloc[::step].items()]


def main():
    df = data.load("EURUSD+", "M1", 50_000)
    out = {
        "symbol": "EURUSD+", "timeframe": "M1", "bars": len(df),
        "period": [str(df.time.iloc[0]), str(df.time.iloc[-1])],
        "spread_points": 1.0, "leverage": 500,
    }

    # Основной прогон — под правилом риска 0.5-3%.
    strat, res, m = run(df, balance=1000.0, steps=10, risk_pct=1.0, max_risk=3.0)
    out["main"] = {
        "params": {"balance": 1000, "risk_pct": 1.0, "max_risk_pct": 3.0,
                   "tp": 10, "sl": 10, "mult": 2.0, "steps": 10},
        "metrics": m.as_dict(), "equity": curve(res),
        "max_step": strat.max_step_seen, "resets": strat.resets,
        "blocked_by_risk": strat.blocked_by_risk,
        "trades": [{"side": t.side.value, "vol": t.volume, "pnl": round(t.pnl, 2),
                    "reason": t.reason, "bars": t.bars_held} for t in res.trades],
    }

    # Варианты управления объёмом.
    out["variants"] = []
    for label, kw in [
        ("Фиксированный лот", dict(steps=1, mult=1.0)),
        ("Мартингейл без риск-лимита", dict(steps=10, mult=2.0)),
        ("Риск 1%, потолок 3%", dict(steps=10, mult=2.0, risk_pct=1.0, max_risk=3.0)),
        ("Риск 2%, потолок 3%", dict(steps=10, mult=2.0, risk_pct=2.0, max_risk=3.0)),
    ]:
        s, r, mm = run(df, balance=1000.0, **kw)
        out["variants"].append({
            "label": label, "metrics": mm.as_dict(),
            "max_step": s.max_step_seen, "resets": s.resets,
            "blocked": s.blocked_by_risk,
            "equity": curve(r, 400),
        })

    # Тот же мартингейл при разных депозитах — проверка на выживание.
    out["deposits"] = []
    for b in (2000, 1500, 1000, 700, 500, 400, 300, 200):
        s, r, mm = run(df, balance=float(b), steps=15)
        out["deposits"].append({
            "balance": b, "end": round(mm.end_balance, 2),
            "return_pct": round(mm.return_pct, 2),
            "dd_pct": round(mm.max_drawdown_pct, 2),
            "ruined": mm.ruined, "trades": mm.trades,
        })

    # Разные уровни риска внутри коридора 0.5-3%.
    out["risk_levels"] = []
    for rp in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        s_, r_, m_ = run(df, balance=1000.0, steps=10, risk_pct=rp, max_risk=3.0)
        out["risk_levels"].append({
            "risk_pct": rp, "return_pct": round(m_.return_pct, 2),
            "dd_pct": round(m_.max_drawdown_pct, 2), "trades": m_.trades,
            "blocked": s_.blocked_by_risk, "end": round(m_.end_balance, 2),
        })

    # Прогрессия лота: сколько стоит каждый следующий шаг серии.
    # Лестница мартингейла от базового лота 0.10 (риск 1% на $1000).
    out["ladder"] = [
        {"step": n, "lot": round(0.10 * 2 ** n, 2),
         "loss": round(0.10 * 2 ** n * 100_000 * 0.0010, 2),
         "risk_pct": round(0.10 * 2 ** n * 100_000 * 0.0010 / 1000 * 100, 2)}
        for n in range(9)
    ]

    Path("logs/dashboard.json").write_text(
        json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print("logs/dashboard.json готов")
    print(f"  основной: {out['main']['metrics']['return_pct']:+.2f}%  "
          f"сделок {out['main']['metrics']['trades']}")
    for d in out["risk_levels"]:
        print(f"  риск {d['risk_pct']:.1f}% -> {d['return_pct']:+7.2f}%  "
              f"DD {d['dd_pct']:5.1f}%  отказов {d['blocked']}")


if __name__ == "__main__":
    main()
