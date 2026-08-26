"""Ретроспективная проверка: изменил бы новостной фильтр результат бэктеста.

Требует FRED_API_KEY в .env (см. neft/core/news_historical.py). Покрывает
только USD-события — честно показывает эффект именно для них, не выдаёт
это за полную картину по всем валютам.

    python scripts/news_backtest.py --symbol NAS100 --tf M1
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from rich.console import Console
from rich.table import Table

from neft.backtest import crypto_data, data, metrics
from neft.backtest.engine import Backtester, Costs
from neft.core import symbols
from neft.core.news import NewsGate
from neft.core.news_historical import load_historical
from neft.core.risk import RiskLimits, RiskManager
from neft.strategies.scalp_ha import ScalpHA
from scripts.compare_crypto import spec_for

con = Console()


def _is_crypto(symbol: str) -> bool:
    return "/USDT" in symbol or symbol.endswith("USDT")


def _setup(symbol, tf, bars, risk, session, utc_offset, buffer, with_news):
    if _is_crypto(symbol):
        df = crypto_data.load(symbol, tf.replace("M", "").lower() + "m" if tf.startswith("M") else tf, bars)
        spec = spec_for(symbol, float(df.close.median()))
        spread = spec.default_spread
        point = spec.point
        contract = spec.contract_size
        sess = None if session is None else session
        offset = utc_offset if utc_offset is not None else 0.0
    else:
        spec = symbols.load(symbol)
        df = data.load(symbol, tf, bars)
        spread = spec.default_spread
        point = spec.point
        contract = spec.contract_size
        sess = (16, 19) if session is None else session
        offset = utc_offset if utc_offset is not None else 3.0

    limits = RiskLimits(risk_per_trade_pct=risk, max_risk_per_trade_pct=3.0,
                        max_volume=100.0, max_daily_loss_pct=100.0,
                        max_drawdown_pct=100.0, min_free_margin_pct=0.0)
    rm = RiskManager(start_balance=1000.0, limits=limits)
    events = []
    if with_news:
        events = load_historical(str(df.time.iloc[0].date()),
                                 str(df.time.iloc[-1].date()))
        con.print(f"[dim]история {df.time.iloc[0]} — {df.time.iloc[-1]}[/]")
        con.print(f"[dim]исторических USD-событий в окне: {len(events)}, "
                  f"буфер ±{buffer} мин, utc_offset={offset}, "
                  f"сессия={sess or 'круглосуточно'}[/]")
        in_sess = 0
        for e in events:
            local = e.time + pd.Timedelta(hours=offset)
            if sess is None or sess[0] <= local.hour < sess[1]:
                in_sess += 1
                con.print(f"  [dim]в сессии:[/] {e.time:%a %d.%m %H:%M UTC}  {e.title}")
        con.print(f"[dim]из них попадает в торговую сессию: {in_sess}/{len(events)}[/]")
        gate = NewsGate(buffer_minutes=buffer, events=events)
        rm.set_news_gate(gate, utc_offset_hours=offset)

    strat = ScalpHA(rr=1.0, pullback_bars=2, session=sess, vol_mode="min",
                    vol_window=3, entry_mode="stop", risk_pct=risk,
                    risk_manager=rm, spec=spec)
    costs = Costs(spread_points=spread, contract_size=contract, point=point)
    res = Backtester(strat, rm, costs, start_balance=1000.0, symbol=symbol).run(df)
    return metrics.compute(res.equity, res.trades, 1000.0, res.ruined), res


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="NAS100")
    p.add_argument("--tf", default="M1")
    p.add_argument("--bars", type=int, default=99_000)
    p.add_argument("--risk", type=float, default=0.75)
    p.add_argument("--buffer", type=int, default=5,
                   help="окно блокировки ±N минут вокруг релиза")
    p.add_argument("--utc-offset", type=float, default=None,
                   help="сдвиг времени баров относительно UTC; MT5=3, крипта=0")
    a = p.parse_args()

    try:
        m_off, _ = _setup(a.symbol, a.tf, a.bars, a.risk, None, a.utc_offset,
                          a.buffer, with_news=False)
        m_on, res_on = _setup(a.symbol, a.tf, a.bars, a.risk, None, a.utc_offset,
                              a.buffer, with_news=True)
    except RuntimeError as e:
        con.print(f"[red]{e}[/]")
        raise SystemExit(1)

    t = Table(title=f"{a.symbol} {a.tf} · с фильтром новостей (USD, FRED) и без",
              box=None, pad_edge=False, title_style="bold cyan")
    for c in ("", "без фильтра", "с фильтром", "разница"):
        t.add_column(c, justify="right" if c else "left")

    def delta_n(x, y):
        return f"{y - x:+d}"

    def delta_f(x, y, suffix=""):
        return f"{y - x:+.2f}{suffix}"

    t.add_row("сделок", str(m_off.trades), str(m_on.trades),
              delta_n(m_off.trades, m_on.trades))
    t.add_row("винрейт", f"{m_off.win_rate:.1f}%", f"{m_on.win_rate:.1f}%",
              delta_f(m_off.win_rate, m_on.win_rate, " п.п."))
    t.add_row("PF", f"{m_off.profit_factor:.2f}", f"{m_on.profit_factor:.2f}",
              delta_f(m_off.profit_factor, m_on.profit_factor))
    t.add_row("просадка", f"{m_off.max_drawdown_pct:.1f}%",
              f"{m_on.max_drawdown_pct:.1f}%",
              delta_f(m_off.max_drawdown_pct, m_on.max_drawdown_pct, " п.п."))
    t.add_row("доход", f"{m_off.return_pct:+.2f}%", f"{m_on.return_pct:+.2f}%",
              delta_f(m_off.return_pct, m_on.return_pct, " п.п."))
    con.print(t)
    news_rej = [r for r in res_on.rejected if str(r[1]).startswith("новость:")]
    con.print(f"\nотклонено по новостям: {len(news_rej)} "
              f"(всего отказов риск-слоя: {len(res_on.rejected)})")
    con.print("[dim]покрытие: только USD (CPI/NFP/GDP/PPI/PCE/FOMC). "
              "EUR/GBP/AUD в этом тесте не участвуют.[/]")
