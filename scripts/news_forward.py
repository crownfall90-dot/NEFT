"""Новостной CFD-бот: Impulse + Fade + Straddle на ForexFactory High-impact.

Режимы как у forward.py: --paper / demo / --live.
NewsGate НЕ включаем — здесь торгуем окно релиза, а не блокируем его.

    python scripts/news_forward.py --paper
    python scripts/news_forward.py --symbols EURUSD+,NAS100,UKOUSD --balance 1000
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import MetaTrader5 as mt5
import pandas as pd
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from neft.core import mt5_symbols as mt5sym
from neft.core import symbols as symlib
from neft.core.config import settings
from neft.core.machine_config import load as load_bot_cfg
from neft.core.models import Side
from neft.core.news import Event, load_calendar, near_high_event
from neft.core.portfolio import Portfolio
from neft.core.risk import RiskLimits, RiskManager
from neft.core.strategy import Bar, ClosedTrade, Signal
from neft.strategies.news_pulse import (
    EventTracker, NewsFade, NewsImpulse, NewsStraddle,
)

con = Console()
ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / "logs" / "news_forward.jsonl"
STATE = ROOT / "logs" / "news_forward_state.json"
STATUS = ROOT / "logs" / "news_status.json"
PANEL = ROOT / "dashboard" / "news_bot.json"
MAGIC = 20260826
PREPARE_BARS = 500
UTC_OFFSET = 3.0  # Bybit MT5 server


def jlog(**rec) -> None:
    rec["ts"] = datetime.now().isoformat(timespec="seconds")
    JOURNAL.parent.mkdir(exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_status(*, ok: bool, error: str = "", **extra) -> None:
    payload = {
        "ok": ok, "error": error or None,
        "ts": datetime.now().isoformat(timespec="seconds"), **extra,
    }
    STATUS.parent.mkdir(exist_ok=True)
    STATUS.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@dataclass
class PaperPending:
    side: str
    volume: float
    price: float
    sl: float
    tp: float
    expires_at: str
    strategy: str = "?"
    oco_id: str = ""


@dataclass
class PaperPosition:
    side: str
    volume: float
    entry: float
    sl: float
    tp: float
    opened_at: str
    strategy: str = "?"


@dataclass
class SymbolState:
    last_bar: str = ""
    pending: dict | None = None
    pending_oco: dict | None = None   # вторая нога straddle
    position: dict | None = None
    ticket: int = 0
    ticket_oco: int = 0
    pos_ticket: int = 0
    trades: list = field(default_factory=list)
    expired: int = 0
    rejected: int = 0
    owner: str = ""
    last_signal: str = ""


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def save_state(states: dict, balance: float) -> None:
    STATE.write_text(json.dumps(
        {"balance": balance,
         "symbols": {k: asdict(v) for k, v in states.items()}},
        ensure_ascii=False, indent=1), encoding="utf-8")


def fetch(symbol: str, bars: int = PREPARE_BARS) -> pd.DataFrame | None:
    r = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, bars)
    if r is None or len(r) < 50:
        return None
    df = pd.DataFrame(r)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df[["time", "open", "high", "low", "close", "tick_volume", "spread"]]


def _news_cfg() -> dict:
    return (load_bot_cfg().get("news_bot") or {})


class Watcher:
    def __init__(self, symbol: str, args, risk: RiskManager, state: SymbolState):
        self.symbol = symbol
        self.args = args
        self.risk = risk
        self.st = state
        self.spec = symlib.load(symbol)
        self.tracker = EventTracker()
        nb = _news_cfg()
        modes = set(nb.get("modes") or ["impulse", "fade", "straddle"])
        kw = dict(
            symbol=symbol, tracker=self.tracker,
            utc_offset_hours=UTC_OFFSET,
            post_window_min=float(nb.get("post_window_min") or args.post_window),
            pre_minutes=float(nb.get("pre_minutes") or args.pre_minutes),
            impulse_atr=float(nb.get("impulse_atr") or args.impulse_atr),
            rr=float(nb.get("rr") or args.rr),
            risk_pct=args.risk, risk_manager=risk, spec=self.spec,
        )
        self.impulse = NewsImpulse(**kw)
        self.fade = NewsFade(**kw)
        self.straddle = NewsStraddle(**kw)
        port = Portfolio()
        if "impulse" in modes:
            port.add(self.impulse, "Impulse")
        if "fade" in modes:
            port.add(self.fade, "Fade")
        if "straddle" in modes:
            port.add(self.straddle, "Straddle")
        self.strat = port
        self.df: pd.DataFrame | None = None
        self.row = None
        self.last_err = ""
        self.events: list[Event] = []

    def set_events(self, events: list[Event]) -> None:
        self.events = events
        for slot in self.strat.slots:
            if hasattr(slot.strategy, "set_events"):
                slot.strategy.set_events(events)

    def _point(self) -> float:
        return self.spec.point

    def _pnl(self, side: str, entry: float, exit_: float, volume: float) -> float:
        d = 1 if side == Side.BUY.value else -1
        gross = (exit_ - entry) * d * volume * self.spec.contract_size
        from neft.core.bybit_cfd_fees import commission_per_lot
        fee = commission_per_lot(self.symbol) * volume
        return gross - fee

    def poll(self, equity: float, live: bool) -> None:
        df = fetch(self.symbol)
        if df is None:
            self.last_err = "нет истории"
            return
        self.last_err = ""
        prepared = self.strat.prepare(df).reset_index(drop=True)
        self.df = self.impulse.df if self.impulse.df is not None else prepared
        closed_i = len(prepared) - 2
        if closed_i < 30:
            self.last_err = "мало баров"
            return
        self.row = self.df.iloc[closed_i]
        bar_time = str(self.row.time)

        if live:
            self._sync_live(bar_time)
        if bar_time == self.st.last_bar:
            return
        start = closed_i
        if self.st.last_bar:
            prev = prepared.index[prepared.time == pd.Timestamp(self.st.last_bar)]
            if len(prev):
                start = int(prev[0]) + 1
        for i in range(max(start, closed_i - 10), closed_i + 1):
            self._on_bar(prepared, i, equity, live)
        self.st.last_bar = bar_time

    def _cancel_oco_paper(self) -> None:
        self.st.pending = None
        self.st.pending_oco = None

    def _cancel_oco_live(self) -> None:
        for ticket_attr in ("ticket", "ticket_oco"):
            t = getattr(self.st, ticket_attr)
            if t:
                mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": t})
                setattr(self.st, ticket_attr, 0)
        self.st.pending = None
        self.st.pending_oco = None

    def _on_bar(self, d: pd.DataFrame, i: int, equity: float, live: bool) -> None:
        row = d.iloc[i]
        if not live:
            self._paper_manage(row)

        # Pending OCO не блокирует Impulse/Fade — они отменяют straddle.
        held = bool(self.st.position or self.st.pos_ticket)
        self.strat.set_equity(equity)
        bar = Bar(row.time, row.open, row.high, row.low, row.close,
                  int(row.spread), index=i, volume=float(row.tick_volume))
        sig = self.strat.on_bar(bar, in_position=held)
        owner = self.strat._owner.name if self.strat._owner else "?"
        if sig is None:
            return

        # Market signal (impulse/fade) while straddle pending → cancel OCO
        if sig.entry_type == "market" and (self.st.pending or self.st.pending_oco
                                           or self.st.ticket or self.st.ticket_oco):
            if live:
                self._cancel_oco_live()
            else:
                self._cancel_oco_paper()

        if held:
            return
        if self.st.pending or self.st.pending_oco or self.st.ticket or self.st.ticket_oco:
            # Уже висит OCO/pending — новые stop-сигналы не ставим
            if sig.entry_type != "market":
                return

        oco = None
        if owner == "Straddle":
            oco = self.straddle.consume_oco()

        entry_px = float(sig.entry) if sig.entry is not None else float(row.close)
        sl_dist = abs(entry_px - sig.sl) if sig.sl is not None else 0.0
        margin = sig.volume * self.spec.contract_size * abs(entry_px) / self.args.leverage
        ok, why = self.risk.approve(
            sig, equity=equity, free_margin=equity, required_margin=margin,
            open_positions=0, sl_distance=sl_dist,
            contract_size=self.spec.contract_size,
            when=row.time, symbol=self.symbol,
        )
        if not ok:
            self.st.rejected += 1
            jlog(type="reject", symbol=self.symbol, strategy=owner,
                 bar=str(row.time), why=why, volume=sig.volume)
            self.strat.on_signal_rejected(sig, why)
            return

        self.st.owner = owner
        self.st.last_signal = sig.reason
        jlog(type="signal", symbol=self.symbol, strategy=owner, bar=str(row.time),
             side=sig.side.value, volume=sig.volume, entry=entry_px,
             sl=sig.sl, tp=sig.tp, reason=sig.reason)

        if oco is not None:
            buy_sig, sell_sig = oco
            expires = str(d.iloc[min(i + buy_sig.expire_bars, len(d) - 1)].time)
            if live:
                self._place_live_oco(buy_sig, sell_sig, expires)
            else:
                self.st.pending = asdict(PaperPending(
                    side=buy_sig.side.value, volume=buy_sig.volume,
                    price=buy_sig.entry, sl=buy_sig.sl, tp=buy_sig.tp,
                    expires_at=expires, strategy=owner, oco_id=e_key(buy_sig)))
                self.st.pending_oco = asdict(PaperPending(
                    side=sell_sig.side.value, volume=sell_sig.volume,
                    price=sell_sig.entry, sl=sell_sig.sl, tp=sell_sig.tp,
                    expires_at=expires, strategy=owner, oco_id=e_key(sell_sig)))
                jlog(type="place_oco", mode="paper", symbol=self.symbol,
                     buy=buy_sig.entry, sell=sell_sig.entry, expires=expires)
            return

        if sig.entry_type == "market" or sig.entry is None:
            spread = (row.spread or 1) * self._point()
            buy = sig.side is Side.BUY
            fill = float(row.close) + (spread if buy else -spread)
            if live:
                self._place_live_market(sig, fill)
            else:
                self.st.position = asdict(PaperPosition(
                    side=sig.side.value, volume=sig.volume, entry=fill,
                    sl=sig.sl, tp=sig.tp, opened_at=str(row.time),
                    strategy=owner))
                jlog(type="fill", mode="paper", symbol=self.symbol,
                     strategy=owner, bar=str(row.time),
                     side=sig.side.value, volume=sig.volume, entry=fill,
                     sl=sig.sl, tp=sig.tp)
            return

        expires = str(d.iloc[min(i + sig.expire_bars, len(d) - 1)].time)
        if live:
            self._place_live(sig, expires)
        else:
            self.st.pending = asdict(PaperPending(
                side=sig.side.value, volume=sig.volume, price=sig.entry,
                sl=sig.sl, tp=sig.tp, expires_at=expires, strategy=owner))

    def _paper_manage(self, row) -> None:
        p = self.st.position
        if p:
            buy = p["side"] == Side.BUY.value
            hit_sl = row.low <= p["sl"] if buy else row.high >= p["sl"]
            hit_tp = row.high >= p["tp"] if buy else row.low <= p["tp"]
            exit_price = reason = None
            if hit_sl:
                exit_price, reason = p["sl"], "sl"
            elif hit_tp:
                exit_price, reason = p["tp"], "tp"
            if exit_price is not None:
                pnl = self._pnl(p["side"], p["entry"], exit_price, p["volume"])
                self.st.trades.append(pnl)
                self.st.position = None
                jlog(type="close", mode="paper", symbol=self.symbol,
                     strategy=p.get("strategy", "?"), bar=str(row.time),
                     side=p["side"], volume=p["volume"], entry=p["entry"],
                     exit=exit_price, pnl=round(pnl, 2), reason=reason)
                self.strat.on_trade_closed(ClosedTrade(
                    side=Side(p["side"]), volume=p["volume"], entry=p["entry"],
                    exit=exit_price, pnl=pnl, reason=reason, bars_held=0))
                self.st.owner = ""
                return

        # OCO / single pending
        for attr, other in (("pending", "pending_oco"), ("pending_oco", "pending")):
            q = getattr(self.st, attr)
            if not q or self.st.position:
                continue
            buy = q["side"] == Side.BUY.value
            touched = row.high >= q["price"] if buy else row.low <= q["price"]
            if touched:
                spread = (row.spread or 1) * self._point()
                entry = q["price"] + (spread if buy else -spread)
                self.st.position = asdict(PaperPosition(
                    side=q["side"], volume=q["volume"], entry=entry,
                    sl=q["sl"], tp=q["tp"], opened_at=str(row.time),
                    strategy=q.get("strategy", "?")))
                setattr(self.st, attr, None)
                setattr(self.st, other, None)  # OCO: снять вторую ногу
                if q.get("strategy") == "Straddle":
                    self.straddle.mark_filled()
                jlog(type="fill", mode="paper", symbol=self.symbol,
                     strategy=q.get("strategy", "?"), bar=str(row.time),
                     side=q["side"], volume=q["volume"], entry=entry,
                     sl=q["sl"], tp=q["tp"], oco=True)
                return
            if str(row.time) >= q["expires_at"]:
                setattr(self.st, attr, None)
                self.st.expired += 1
                jlog(type="expire", mode="paper", symbol=self.symbol,
                     strategy=q.get("strategy", "?"), bar=str(row.time),
                     price=q["price"])
                if not getattr(self.st, other):
                    self.st.owner = ""
                    if q.get("strategy") == "Straddle":
                        self.straddle.on_signal_rejected(
                            Signal(side=Side.BUY, volume=0), "expire")

    def _place_live_market(self, sig: Signal, fill: float) -> None:
        info = mt5.symbol_info(self.symbol)
        vol = max(info.volume_min,
                  min(round(round(sig.volume / info.volume_step) * info.volume_step, 8),
                      info.volume_max))
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            self.st.rejected += 1
            jlog(type="reject", mode="demo", symbol=self.symbol, why="нет котировки")
            return
        price = tick.ask if sig.side is Side.BUY else tick.bid
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol, "volume": vol,
            "type": mt5.ORDER_TYPE_BUY if sig.side is Side.BUY else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": round(sig.sl, info.digits), "tp": round(sig.tp, info.digits),
            "deviation": 30, "magic": MAGIC, "comment": "neft-news",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            why = (f"retcode={res.retcode} {res.comment}" if res
                   else str(mt5.last_error()))
            self.st.rejected += 1
            jlog(type="reject", mode="demo", symbol=self.symbol, why=why)
            self.strat.on_signal_rejected(sig, why)
            return
        self.st.pos_ticket = int(res.order or res.deal or 0)
        jlog(type="fill", mode="demo", symbol=self.symbol, strategy=self.st.owner,
             ticket=self.st.pos_ticket, side=sig.side.value, volume=vol,
             entry=fill, sl=sig.sl, tp=sig.tp)

    def _place_live(self, sig: Signal, expires: str) -> None:
        info = mt5.symbol_info(self.symbol)
        vol = max(info.volume_min,
                  min(round(round(sig.volume / info.volume_step) * info.volume_step, 8),
                      info.volume_max))
        req = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": self.symbol, "volume": vol,
            "type": (mt5.ORDER_TYPE_BUY_STOP if sig.side is Side.BUY
                     else mt5.ORDER_TYPE_SELL_STOP),
            "price": round(sig.entry, info.digits),
            "sl": round(sig.sl, info.digits), "tp": round(sig.tp, info.digits),
            "magic": MAGIC, "comment": "neft-news",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            why = (f"retcode={res.retcode} {res.comment}" if res
                   else str(mt5.last_error()))
            self.st.rejected += 1
            jlog(type="reject", mode="demo", symbol=self.symbol, why=why)
            return
        self.st.ticket = int(res.order)
        self.st.pending = {"side": sig.side.value, "volume": vol,
                           "price": sig.entry, "sl": sig.sl, "tp": sig.tp,
                           "expires_at": expires, "strategy": self.st.owner}
        jlog(type="place", mode="demo", symbol=self.symbol, strategy=self.st.owner,
             ticket=self.st.ticket, side=sig.side.value, entry=sig.entry)

    def _place_live_oco(self, buy: Signal, sell: Signal, expires: str) -> None:
        self._place_live(buy, expires)
        if not self.st.ticket:
            return
        info = mt5.symbol_info(self.symbol)
        vol = max(info.volume_min,
                  min(round(round(sell.volume / info.volume_step) * info.volume_step, 8),
                      info.volume_max))
        req = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": self.symbol, "volume": vol,
            "type": mt5.ORDER_TYPE_SELL_STOP,
            "price": round(sell.entry, info.digits),
            "sl": round(sell.sl, info.digits), "tp": round(sell.tp, info.digits),
            "magic": MAGIC, "comment": "neft-news-oco",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            # сняли buy если sell не встал
            mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": self.st.ticket})
            self.st.ticket = 0
            self.st.pending = None
            why = (f"oco sell fail: {res.retcode if res else mt5.last_error()}")
            self.st.rejected += 1
            jlog(type="reject", mode="demo", symbol=self.symbol, why=why)
            self.straddle.on_signal_rejected(buy, why)
            return
        self.st.ticket_oco = int(res.order)
        self.st.pending_oco = {"side": sell.side.value, "volume": vol,
                               "price": sell.entry, "sl": sell.sl, "tp": sell.tp,
                               "expires_at": expires, "strategy": "Straddle"}
        jlog(type="place_oco", mode="demo", symbol=self.symbol,
             buy_ticket=self.st.ticket, sell_ticket=self.st.ticket_oco,
             buy=buy.entry, sell=sell.entry)

    def _sync_live(self, bar_time: str) -> None:
        # OCO: если одна нога исполнилась — снять другую
        for ticket_attr, other_ticket, pend_attr, other_pend in (
            ("ticket", "ticket_oco", "pending", "pending_oco"),
            ("ticket_oco", "ticket", "pending_oco", "pending"),
        ):
            t = getattr(self.st, ticket_attr)
            if not t:
                continue
            orders = mt5.orders_get(ticket=t) or ()
            if not orders:
                setattr(self.st, ticket_attr, 0)
                setattr(self.st, pend_attr, None)
                pos = [p for p in (mt5.positions_get(symbol=self.symbol) or ())
                       if p.magic == MAGIC]
                if pos:
                    p = pos[0]
                    self.st.pos_ticket = int(p.ticket)
                    ot = getattr(self.st, other_ticket)
                    if ot:
                        mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ot})
                        setattr(self.st, other_ticket, 0)
                    setattr(self.st, other_pend, None)
                    self.straddle.mark_filled()
                    jlog(type="fill", mode="demo", symbol=self.symbol,
                         strategy=self.st.owner, ticket=int(p.ticket),
                         side="buy" if p.type == 0 else "sell",
                         volume=p.volume, entry=p.price_open)
                continue
            pend = getattr(self.st, pend_attr)
            if pend and bar_time >= pend["expires_at"]:
                mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": t})
                setattr(self.st, ticket_attr, 0)
                setattr(self.st, pend_attr, None)
                self.st.expired += 1
                jlog(type="expire", mode="demo", symbol=self.symbol, bar=bar_time)

        if self.st.pos_ticket:
            alive = [p for p in (mt5.positions_get(symbol=self.symbol) or ())
                     if int(p.ticket) == self.st.pos_ticket]
            if not alive:
                deals = mt5.history_deals_get(position=self.st.pos_ticket) or ()
                pnl = sum(d.profit + d.commission + d.swap for d in deals)
                exit_d = deals[-1] if deals else None
                reason = "?"
                if exit_d is not None:
                    reason = ("tp" if exit_d.reason == mt5.DEAL_REASON_TP
                              else "sl" if exit_d.reason == mt5.DEAL_REASON_SL
                              else "manual")
                self.st.trades.append(pnl)
                jlog(type="close", mode="demo", symbol=self.symbol,
                     strategy=self.st.owner, ticket=self.st.pos_ticket,
                     pnl=round(pnl, 2), reason=reason)
                self.st.pos_ticket = 0
                self.st.owner = ""

    def render_row(self) -> list:
        if self.last_err or self.row is None:
            return [self.symbol, f"[red]{self.last_err or '...'}[/]"] + [""] * 5
        tick = mt5.symbol_info_tick(self.symbol)
        n = len(self.st.trades)
        pnl = sum(self.st.trades)
        if self.st.position or self.st.pos_ticket:
            status = f"[cyan]{self.st.owner} позиция[/]"
        elif self.st.pending and self.st.pending_oco:
            status = (f"[yellow]OCO "
                      f"{self.st.pending['price']:.{self.spec.digits}f}/"
                      f"{self.st.pending_oco['price']:.{self.spec.digits}f}[/]")
        elif self.st.pending:
            status = f"[yellow]ордер @ {self.st.pending['price']:.{self.spec.digits}f}[/]"
        else:
            status = f"[dim]{self.st.last_signal or 'ждём новость'}[/]"
        return [
            self.symbol,
            f"{tick.bid:.{self.spec.digits}f}" if tick else "-",
            f"{n}",
            (f"[green]{pnl:+.2f}[/]" if pnl > 0
             else f"[red]{pnl:+.2f}[/]" if pnl < 0 else "[dim]0[/]"),
            f"[dim]{self.st.expired}/{self.st.rejected}[/]",
            status,
        ]


def e_key(sig: Signal) -> str:
    return sig.reason or ""


def render(watchers: list[Watcher], mode: str, equity: float, n: int,
           events: list[Event]) -> Group:
    t = Table(box=None, pad_edge=False)
    for c, j in (("символ", "left"), ("цена", "right"), ("сделок", "right"),
                 ("pnl", "right"), ("exp/rej", "right"), ("статус", "left")):
        t.add_column(c, justify=j)
    for w in watchers:
        t.add_row(*[str(x) for x in w.render_row()])
    now_utc = pd.Timestamp.now(tz="UTC").tz_localize(None)
    soon = [e for e in events if e.impact == "High"
            and 0 <= (e.time - now_utc).total_seconds() / 60 <= 180]
    soon.sort(key=lambda e: e.time)
    lines = [f"[bold]news-bot[/] · {mode} · equity {equity:.2f} · tick #{n}"]
    if soon:
        e = soon[0]
        mins = int((e.time - now_utc).total_seconds() / 60)
        lines.append(f"[dim]след. High:[/] {e.currency} {e.title} через {mins}м "
                     f"({e.time:%H:%M} UTC)")
    else:
        lines.append("[dim]High-релиз в ближайшие 3ч не найден[/]")
    return Group(Text.from_markup("\n".join(lines)), t)


def write_panel(watchers: list[Watcher], mode: str, equity: float,
                events: list[Event], n: int) -> None:
    now_utc = pd.Timestamp.now(tz="UTC").tz_localize(None)
    upcoming = []
    for e in events:
        if e.impact != "High":
            continue
        mins = (e.time - now_utc).total_seconds() / 60
        if -30 <= mins <= 360:
            upcoming.append({
                "time": e.time.isoformat(), "currency": e.currency,
                "title": e.title, "forecast": e.forecast,
                "previous": e.previous, "actual": e.actual,
                "mins": round(mins, 1),
            })
    payload = {
        "mode": mode, "equity": round(equity, 2), "tick": n,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "upcoming": upcoming[:20],
        "symbols": [{
            "symbol": w.symbol,
            "trades": len(w.st.trades),
            "pnl": round(sum(w.st.trades), 2),
            "owner": w.st.owner,
            "last_signal": w.st.last_signal,
            "position": w.st.position,
            "pending": w.st.pending,
            "pending_oco": w.st.pending_oco,
            "err": w.last_err,
        } for w in watchers],
    }
    PANEL.parent.mkdir(exist_ok=True)
    PANEL.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                     encoding="utf-8")


def refresh_calendar(events: list[Event]) -> list[Event]:
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    near = near_high_event(events, now, window_minutes=30) if events else True
    max_age = 0.02 if near else 1.0  # ~1 мин рядом с релизом, иначе 1ч
    try:
        return load_calendar(refresh=near, max_age_hours=max_age)
    except Exception as e:
        jlog(type="error", symbol="calendar", err=str(e))
        return events


def main() -> None:
    nb = _news_cfg()
    default_syms = nb.get("symbols") or list(mt5sym.CFD_SYMBOLS)
    if isinstance(default_syms, list) and not default_syms:
        default_syms = list(mt5sym.CFD_SYMBOLS)

    p = argparse.ArgumentParser(description="News CFD bot (impulse/fade/straddle)")
    p.add_argument("--symbols", default=",".join(default_syms))
    p.add_argument("--risk", type=float, default=float(nb.get("risk_pct") or 1.0),
                   help="риск на сделку, %% депозита (потолок 1%%)")
    p.add_argument("--balance", type=float, default=float(nb.get("deposit") or 1000))
    p.add_argument("--leverage", type=int, default=500)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--paper", action="store_true")
    p.add_argument("--live", action="store_true")
    p.add_argument("--rr", type=float, default=float(nb.get("rr") or 1.5))
    p.add_argument("--impulse-atr", type=float, default=float(nb.get("impulse_atr") or 0.8))
    p.add_argument("--pre-minutes", type=float, default=float(nb.get("pre_minutes") or 2))
    p.add_argument("--post-window", type=float, default=float(nb.get("post_window_min") or 15))
    p.add_argument("--daily", type=float, default=4.0)
    p.add_argument("--dd", type=float, default=12.0)
    p.add_argument("--reset", action="store_true")
    a = p.parse_args()

    if not mt5.initialize():
        err = f"MT5 не отвечает: {mt5.last_error()}"
        con.print(f"[red]{err}[/]")
        write_status(ok=False, error=err)
        raise SystemExit(2)
    acc = mt5.account_info()
    if acc is None:
        err = f"Нет данных счёта: {mt5.last_error()}"
        con.print(f"[red]{err}[/]")
        write_status(ok=False, error=err)
        mt5.shutdown()
        raise SystemExit(2)

    term = mt5.terminal_info()
    is_demo = acc.trade_mode != 2
    if a.live:
        if settings.demo_only:
            err = "DEMO_ONLY=true — боевые ордера заблокированы."
            con.print(f"[red]{err}[/]")
            write_status(ok=False, error=err)
            mt5.shutdown()
            raise SystemExit(2)
        mode = "live" if not is_demo else "demo"
    elif is_demo and not a.paper:
        mode = "demo"
    else:
        mode = "paper"
    send = mode in ("demo", "live")
    if mode == "demo" and not term.trade_allowed:
        con.print("[yellow]Алготрейдинг выключен — paper.[/]")
        mode, send = "paper", False
    if not is_demo and not a.paper and not a.live:
        con.print(f"[yellow]Боевой счёт {acc.login} — paper на живых котировках.[/]")

    if a.reset and STATE.exists():
        STATE.unlink()
    saved = load_state()
    balance = acc.balance if send else saved.get("balance", a.balance)
    # Новостной бот: депозит $1000, риск на сделку не выше 1%.
    a.risk = max(0.1, min(1.0, float(a.risk)))
    if not send:
        balance = float(nb.get("deposit") or a.balance or 1000.0)

    limits = RiskLimits(
        risk_per_trade_pct=a.risk, max_risk_per_trade_pct=1.0,
        min_risk_per_trade_pct=0.1,
        max_volume=10.0, max_daily_loss_pct=a.daily,
        max_drawdown_pct=a.dd, max_open_positions=1, min_free_margin_pct=0.0,
    )
    risk = RiskManager(start_balance=balance, limits=limits)
    # намеренно без set_news_gate

    events = load_calendar(refresh=True, max_age_hours=0)
    con.print(f"[dim]Календарь FF: {len(events)} событий "
              f"(High={sum(1 for e in events if e.impact == 'High')})[/]")

    requested = [x.strip() for x in a.symbols.split(",") if x.strip()]
    resolved, missing = mt5sym.resolve_many(
        requested, tradable_only=(mode == "demo"))
    for s in missing:
        con.print(f"[red]{s}: нет у брокера — пропускаю[/]")
    watchers: list[Watcher] = []
    for req_name, broker_name in resolved:
        if req_name != broker_name:
            con.print(f"[dim]{req_name} → {broker_name}[/]")
        raw = saved.get("symbols", {}).get(broker_name) or {}
        from dataclasses import fields as dc_fields
        known = {f.name for f in dc_fields(SymbolState)}
        st = SymbolState(**{k: v for k, v in raw.items() if k in known})
        w = Watcher(broker_name, a, risk, st)
        w.set_events(events)
        watchers.append(w)
    if not watchers:
        err = "Не осталось инструментов."
        con.print(f"[red]{err}[/]")
        write_status(ok=False, error=err)
        mt5.shutdown()
        raise SystemExit(2)

    write_status(ok=True, server=acc.server, login=acc.login, mode=mode,
                 symbols=[w.symbol for w in watchers], bot="news")
    jlog(type="start", mode=mode, account=str(acc.login), server=acc.server,
         symbols=[w.symbol for w in watchers], risk=a.risk, balance=balance,
         events=len(events))

    n = 0
    from rich.live import Live
    try:
        with Live(console=con, refresh_per_second=2, screen=False) as live:
            while True:
                n += 1
                if n % max(1, int(60 / max(a.interval, 1))) == 1:
                    events = refresh_calendar(events)
                    for w in watchers:
                        w.set_events(events)

                acc_now = mt5.account_info()
                if acc_now is None:
                    if not mt5.initialize():
                        con.print(f"[red]MT5 отвалился: {mt5.last_error()}[/]")
                        break
                    acc_now = mt5.account_info()
                    if acc_now is None:
                        break
                equity = (acc_now.equity if send
                          else balance + sum(sum(w.st.trades) for w in watchers))
                risk.update(equity)
                for w in watchers:
                    try:
                        w.poll(equity, live=send)
                    except Exception as e:  # noqa: BLE001
                        w.last_err = str(e)[:60]
                        jlog(type="error", symbol=w.symbol, err=str(e))

                try:
                    live.update(render(watchers, mode, equity, n, events))
                except Exception as e:  # noqa: BLE001
                    jlog(type="error", symbol="render", err=str(e))
                save_state({w.symbol: w.st for w in watchers}, balance)
                try:
                    write_panel(watchers, mode, equity, events, n)
                except Exception as e:  # noqa: BLE001
                    jlog(type="error", symbol="panel", err=str(e))
                if risk.halted:
                    jlog(type="halt", why=risk.halt_reason)
                    con.print(f"[red]KILL-SWITCH: {risk.halt_reason}[/]")
                    break
                time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    finally:
        save_state({w.symbol: w.st for w in watchers}, balance)
        jlog(type="stop", updates=n)
        write_status(ok=True, stopped=True, updates=n)
        mt5.shutdown()


if __name__ == "__main__":
    main()
