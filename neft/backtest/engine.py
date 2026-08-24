"""Бэктест по барам M1. Считает то, что реально съедает депозит: спред,
комиссию, маржу и стоп-аут.

Пессимистичные допущения — лучше недооценить результат, чем переоценить:
  * вход по цене закрытия бара сигнала плюс спред;
  * если в одном баре задеты и SL, и TP — считаем, что сработал SL;
  * маржа резервируется по цене входа, стоп-аут проверяется на каждом баре.
"""
from dataclasses import dataclass, field

import pandas as pd

from neft.core.models import Side
from neft.core.risk import RiskManager
from neft.core.strategy import Bar, ClosedTrade, Signal, Strategy


@dataclass
class Costs:
    spread_points: float = 1.0        # если 0 в истории — берём это значение
    commission_per_lot: float = 0.0   # $ за лот, тейкер (вход всегда, SL/stop_out на выходе)
    commission_maker_per_lot: float = 0.0  # $ за лот, мейкер (TP выходит лимитом — дешевле)
    contract_size: float = 100_000.0
    point: float = 1e-05
    leverage: int = 500
    stop_out_level: float = 0.5       # маржин-колл при equity < 50% от маржи


@dataclass
class Pending:
    """Отложенный стоп-ордер: срабатывает, когда цена дошла до уровня входа.

    Это не украшение, а фильтр: если после doji рынок не пробил её экстремум,
    сделки просто не будет. Половина сигналов отсеивается сама собой.
    """
    signal: object
    price: float
    placed_at: int
    expires_at: int


@dataclass
class Position:
    side: Side
    volume: float
    entry: float
    sl: float
    tp: float
    margin: float
    opened_at: int
    opened_time: object = None


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list[ClosedTrade]
    rejected: list[tuple[pd.Timestamp, str]] = field(default_factory=list)
    expired: int = 0          # отложенные ордера, не дождавшиеся цены
    ruined: bool = False
    ruin_time: pd.Timestamp | None = None
    halt_reason: str = ""


class Backtester:
    def __init__(
        self, strategy: Strategy, risk: RiskManager,
        costs: Costs, start_balance: float = 1000.0,
        symbol: str | None = None,
    ):
        self.strategy = strategy
        self.risk = risk
        self.costs = costs
        self.start_balance = start_balance
        # Нужен только для новостного гейта (risk.news_blocked). Без него
        # (None) поведение не меняется — новостная проверка молча пропускает.
        self.symbol = symbol

    def _pnl(self, pos: Position, exit_price: float, reason: str | None = None) -> float:
        """reason='tp' — выход лимитным ордером на цели, комиссия мейкера
        (дешевле). Иначе (sl/stop_out/ещё открыта) — маркетом, тейкер: SL
        обязан гарантированно исполниться, лимит там рискован. Вход всегда
        по рынку/стопу — тейкер, скидки маркету взять неоткуда."""
        direction = 1 if pos.side is Side.BUY else -1
        gross = (exit_price - pos.entry) * direction * pos.volume * self.costs.contract_size
        entry_fee = self.costs.commission_per_lot * pos.volume
        exit_rate = (self.costs.commission_maker_per_lot if reason == "tp"
                     else self.costs.commission_per_lot)
        return gross - entry_fee - exit_rate * pos.volume

    def run(self, df: pd.DataFrame) -> BacktestResult:
        df = self.strategy.prepare(df).reset_index(drop=True)
        c = self.costs
        balance = self.start_balance
        pos: Position | None = None
        pending: Pending | None = None
        expired = 0
        trades: list[ClosedTrade] = []
        rejected: list[tuple[pd.Timestamp, str]] = []
        equity_curve: list[float] = []
        ruined = False
        ruin_time = None

        for i, row in enumerate(df.itertuples()):
            spread = (row.spread or c.spread_points) * c.point
            bar = Bar(row.time, row.open, row.high, row.low, row.close, row.spread,
                      index=i, volume=getattr(row, "tick_volume", 0.0))

            # 1. Сопровождение открытой позиции: проверяем SL/TP внутри бара.
            if pos is not None:
                hit_sl = row.low <= pos.sl if pos.side is Side.BUY else row.high >= pos.sl
                hit_tp = row.high >= pos.tp if pos.side is Side.BUY else row.low <= pos.tp
                exit_price = reason = None
                if hit_sl:              # SL приоритетнее — пессимистично
                    exit_price, reason = pos.sl, "sl"
                elif hit_tp:
                    exit_price, reason = pos.tp, "tp"

                if exit_price is not None:
                    pnl = self._pnl(pos, exit_price, reason)
                    balance += pnl
                    trade = ClosedTrade(
                        side=pos.side, volume=pos.volume, entry=pos.entry,
                        exit=exit_price, pnl=pnl, reason=reason,
                        bars_held=i - pos.opened_at,
                        opened_at=pos.opened_time, closed_at=row.time,
                    )
                    trades.append(trade)
                    self.strategy.on_trade_closed(trade)
                    self.risk.on_trade_closed(trade)
                    pos = None

            # Оценка капитала до исполнения — для проверки риска.
            equity_now = balance if pos is None else balance

            # 1b. Отложенный стоп: сработал ли на этом баре.
            if pending is not None and pos is None:
                sig = pending.signal
                buy = sig.side is Side.BUY
                touched = row.high >= pending.price if buy else row.low <= pending.price
                if touched:
                    # Отложенный ордер согласовывался ЗАРАНЕЕ (approve() видел
                    # только бар размещения), а исполняется здесь, на другом
                    # баре — новостное окно нужно проверить заново.
                    news_blocked, news_why = self.risk.news_blocked(row.time, self.symbol)
                    if news_blocked:
                        rejected.append((row.time, f"новость: {news_why}"))
                        pending = None
                        expired += 1
                        continue
                    entry = pending.price + (spread if buy else -spread)
                    sl, tp = sig.sl, sig.tp
                    if sig.sl_mode == "trigger_bar":
                        # Стоп за свечу, которая пробила уровень, а не за
                        # ту, на которой сигнал был выставлен.
                        sl = (row.low - sig.sl_buffer if buy
                              else row.high + sig.sl_buffer)
                        risk = abs(entry - sl)
                        if risk <= 0:
                            pending = None
                            expired += 1
                            continue
                        tp = entry + risk * sig.rr if buy else entry - risk * sig.rr
                        # Риск стал известен только сейчас — проверяем потолок.
                        rpct = self.risk.trade_risk_pct(
                            sig.volume, risk, equity_now, c.contract_size)
                        if rpct > self.risk.limits.max_risk_per_trade_pct + 1e-9:
                            rejected.append((row.time, f"риск {rpct:.2f}% при исполнении"))
                            pending = None
                            continue
                    margin = sig.volume * c.contract_size * abs(entry) / c.leverage
                    pos = Position(side=sig.side, volume=sig.volume, entry=entry,
                                   sl=sl, tp=tp, margin=margin, opened_at=i,
                                   opened_time=row.time)
                    pending = None
                elif i >= pending.expires_at:
                    pending = None
                    expired += 1

            # 2. Текущая эквити с учётом плавающей прибыли.
            if pos is not None:
                mark = row.close - spread if pos.side is Side.BUY else row.close + spread
                equity = balance + self._pnl(pos, mark)
            else:
                equity = balance
            equity_curve.append(equity)

            # 3. Стоп-аут брокера — считается до любых новых входов.
            if pos is not None and equity < pos.margin * c.stop_out_level:
                balance = equity
                trades.append(ClosedTrade(
                    side=pos.side, volume=pos.volume, entry=pos.entry,
                    exit=row.close, pnl=equity - balance, reason="stop_out",
                    bars_held=i - pos.opened_at,
                    opened_at=pos.opened_time, closed_at=row.time,
                ))
                pos = None
                ruined, ruin_time = True, row.time
                break
            if equity <= 0:
                ruined, ruin_time = True, row.time
                break

            self.risk.new_day(row.time.date(), balance)
            self.risk.update(equity)

            # 4. Новый сигнал — только через риск-слой.
            if hasattr(self.strategy, "set_equity"):
                self.strategy.set_equity(equity)
            signal: Signal | None = self.strategy.on_bar(
                bar, in_position=pos is not None or pending is not None)
            if signal is None or pos is not None or pending is not None:
                continue

            entry = row.close + spread if signal.side is Side.BUY else row.close - spread
            margin = signal.volume * c.contract_size * entry / c.leverage
            ok, why = self.risk.approve(
                signal, equity=equity, free_margin=equity,
                required_margin=margin, open_positions=0,
                sl_distance=abs(entry - signal.sl) if signal.sl else None,
                contract_size=c.contract_size, when=row.time, symbol=self.symbol,
            )
            if not ok:
                rejected.append((row.time, why))
                self.strategy.on_signal_rejected(signal, why)
                continue

            if signal.entry_type == "stop" and signal.entry is not None:
                pending = Pending(signal=signal, price=signal.entry, placed_at=i,
                                  expires_at=i + signal.expire_bars)
            else:
                pos = Position(
                    side=signal.side, volume=signal.volume, entry=entry,
                    sl=signal.sl, tp=signal.tp, margin=margin, opened_at=i,
                    opened_time=row.time,
                )

        idx = df["time"].iloc[: len(equity_curve)]
        return BacktestResult(
            equity=pd.Series(equity_curve, index=idx, name="equity"),
            trades=trades, rejected=rejected, expired=expired,
            ruined=ruined, ruin_time=ruin_time,
            halt_reason=self.risk.halt_reason,
        )
