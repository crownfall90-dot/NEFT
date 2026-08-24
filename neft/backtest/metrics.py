"""Метрики результата. Для мартингейла главные — просадка и максимальная
серия убытков, а не итоговая прибыль."""
from dataclasses import dataclass, asdict

import pandas as pd

from neft.core.strategy import ClosedTrade


@dataclass
class Metrics:
    start_balance: float
    end_balance: float
    net_profit: float
    return_pct: float
    trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    max_drawdown_abs: float
    max_loss_streak: int
    largest_win: float
    largest_loss: float
    avg_win: float
    avg_loss: float
    expectancy: float
    ruined: bool

    def as_dict(self) -> dict:
        return asdict(self)


def compute(equity: pd.Series, trades: list[ClosedTrade],
            start_balance: float, ruined: bool) -> Metrics:
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))

    streak = best_streak = 0
    for t in trades:
        streak = streak + 1 if t.pnl <= 0 else 0
        best_streak = max(best_streak, streak)

    peak = equity.cummax()
    dd_abs = (peak - equity)
    dd_pct = (dd_abs / peak * 100)
    end = float(equity.iloc[-1]) if len(equity) else start_balance

    return Metrics(
        start_balance=start_balance,
        end_balance=end,
        net_profit=end - start_balance,
        return_pct=(end / start_balance - 1) * 100,
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(trades) * 100 if trades else 0.0,
        profit_factor=gross_win / gross_loss if gross_loss else float("inf"),
        max_drawdown_pct=float(dd_pct.max()) if len(dd_pct) else 0.0,
        max_drawdown_abs=float(dd_abs.max()) if len(dd_abs) else 0.0,
        max_loss_streak=best_streak,
        largest_win=max((t.pnl for t in wins), default=0.0),
        largest_loss=min((t.pnl for t in losses), default=0.0),
        avg_win=gross_win / len(wins) if wins else 0.0,
        avg_loss=-gross_loss / len(losses) if losses else 0.0,
        expectancy=sum(t.pnl for t in trades) / len(trades) if trades else 0.0,
        ruined=ruined,
    )
