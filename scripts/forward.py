"""Форвард-тест ЕДИНОЙ МАШИНЫ (HSS + London S/R с маршрутизацией) на живом
потоке. Отслеживает все инструменты сразу, статистика копится в журнал.

Режимы:
  * demo   - демо-счёт брокера: отложенные стоп-ордера уходят в терминал.
             У Bybit демо нет — панель «Демо» для MT5 запускает paper.
  * paper  - ордера НЕ отправляются; сделки по правилам бэктеста на живых
             котировках. Это безопасная проверка Bybit CFD (NAS100, …).
  * live   - боевой счёт, нужен DEMO_ONLY=false и --live.

    python scripts/forward.py --paper             # все CFD машины, без ордеров
    python scripts/forward.py --symbols NAS100,DJ30
    python scripts/forward.py --report
"""
import argparse
import json
import os
import re
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

from neft.core import symbols as symlib
from neft.core import mt5_symbols as mt5sym
from neft.core.config import settings
from neft.core.machine_config import load as load_bot_cfg
from neft.core.models import Side
from neft.core.portfolio import Portfolio
from neft.core.news import NewsGate, load_calendar
from neft.core.risk import RiskLimits, RiskManager
from neft.core.routing import enabled_for, strategy_on
from neft.core.scanner import (
    allow_new_entries, rank_hits, scan_overlay, score_watcher, scanner_applies,
)
from neft.core.strategy import Bar, ClosedTrade
from neft.strategies.london_sr import LondonSR
from neft.strategies.scalp_ha import ScalpHA

con = Console()
ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / "logs" / "forward.jsonl"
STATE = ROOT / "logs" / "forward_state.json"
STATUS = ROOT / "logs" / "mt5_status.json"
LIVE_HTML = ROOT / "dashboard" / "live.html"
CHART_JSON = ROOT / "dashboard" / "mt5_chart.json"
SCAN_STATE = ROOT / "logs" / "mt5_scanner_state.json"
_RICH = re.compile(r"\[/?[^\]]*\]")
MAGIC = 20260821          # та же метка, что у market_order - это наши сделки
PREPARE_BARS = 3000       # окно для EMA100, свингов London S/R и медианы бара
HSS_TF_RULE = {"M1": None, "M5": "5min", "M15": "15min"}


def _hss_cfg() -> dict:
    return (load_bot_cfg().get("strategies") or {}).get("hss") or {}


def _trade_bars(raw: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Торговый ТФ HSS: live тянет M1, стратегия может жить на M5."""
    rule = HSS_TF_RULE.get(str(tf or "M1").upper())
    if not rule:
        return raw
    from neft.backtest.data import resample_ohlc
    return resample_ohlc(raw, rule)


# Полный CFD-набор Bybit. У Bybit нет демо-серверов — проверка бота =
# paper на Live-котировках (см. --paper / режим demo в панели).
ALL_SYMBOLS = ",".join(mt5sym.CFD_SYMBOLS)


def jlog(**rec) -> None:
    rec["ts"] = datetime.now().isoformat(timespec="seconds")
    JOURNAL.parent.mkdir(exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_mt5_status(*, ok: bool, error: str = "", **extra) -> None:
    """Короткий статус для админки (preflight / падение без traceback в UI)."""
    payload = {
        "ok": ok,
        "error": error or None,
        "ts": datetime.now().isoformat(timespec="seconds"),
        **extra,
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
    expires_at: str          # время бара, после которого ордер снимается
    strategy: str = "?"


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
    """Всё, что переживает перезапуск. Иначе после падения бот теряет позицию."""
    last_bar: str = ""
    pending: dict | None = None
    position: dict | None = None
    ticket: int = 0          # demo-режим: тикет отложенного ордера
    pos_ticket: int = 0      # demo-режим: тикет позиции
    trades: list = field(default_factory=list)   # pnl закрытых сделок
    expired: int = 0
    rejected: int = 0
    owner: str = ""          # чья стратегия держит текущий ордер/позицию


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def save_state(states: dict, balance: float) -> None:
    STATE.write_text(json.dumps(
        {"balance": balance,
         "symbols": {k: asdict(v) for k, v in states.items()}},
        ensure_ascii=False, indent=1), encoding="utf-8")


# ── данные ────────────────────────────────────────────────────────────
def fetch(symbol: str, bars: int = PREPARE_BARS) -> pd.DataFrame | None:
    r = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, bars)
    if r is None or len(r) < 200:
        return None
    df = pd.DataFrame(r)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df[["time", "open", "high", "low", "close", "tick_volume", "spread"]]


class Watcher:
    """Один инструмент: свой экземпляр стратегии, своё состояние."""

    def __init__(self, symbol: str, args, risk: RiskManager, state: SymbolState):
        self.symbol = symbol
        hss_cfg = _hss_cfg()
        self.hss_tf = str(hss_cfg.get("tf") or "M1").upper()
        self.tf = {"M5": "5m", "M15": "15m"}.get(self.hss_tf, "1m")
        self.args = args
        self.risk = risk
        self.st = state
        self.spec = symlib.load(symbol)

        # Та же сборка, что в scripts/machine.py: HSS первым (сетап короче
        # и точнее), London S/R вторым, маршрутизация по инструменту.
        rr = getattr(args, "rr", 1.0)
        pb = int(getattr(args, "pullback", 2))
        hss_sess = getattr(args, "hss_session_tuple", (16, 19))
        lon = getattr(args, "london_tuple", (11, 16))
        ny = getattr(args, "ny_tuple", (16, 23))
        vol_mode = str(hss_cfg.get("vol_mode") or "min")
        entry_mode = str(hss_cfg.get("entry_mode") or "stop")
        match_doji = bool(hss_cfg.get("matching_doji")
                          or hss_cfg.get("require_matching_doji"))
        self.hss = ScalpHA(rr=rr, pullback_bars=pb, session=hss_sess,
                           vol_mode=vol_mode, vol_window=3, entry_mode=entry_mode,
                           require_matching_doji=match_doji,
                           risk_pct=args.risk, risk_manager=risk, spec=self.spec)
        self.lsr = LondonSR(london=lon, ny=ny, min_rr=rr,
                            risk_pct=args.risk, risk_manager=risk, spec=self.spec)
        self.strat = Portfolio().add(self.hss, "HSS").add(self.lsr, "London S/R")
        self.routes = enabled_for(symbol) if not args.no_route else \
            {"HSS": True, "London S/R": True}
        self._apply_panel()

        self.df: pd.DataFrame | None = None
        self.row = None          # последний закрытый бар с признаками HSS
        self.last_err = ""

    def _apply_panel(self) -> None:
        st = (load_bot_cfg().get("strategies") or {})
        for slot in self.strat.slots:
            slot.enabled = bool(self.routes.get(slot.name, True)
                                and strategy_on(slot.name, st))

    # ── общее ─────────────────────────────────────────────────────────
    def _point(self) -> float:
        return self.spec.point

    def _pnl(self, side: str, entry: float, exit_: float, volume: float) -> float:
        d = 1 if side == Side.BUY.value else -1
        gross = (exit_ - entry) * d * volume * self.spec.contract_size
        # Bybit TradFi: комиссия при открытии ($/лот).
        from neft.core.bybit_cfd_fees import commission_per_lot
        fee = commission_per_lot(self.symbol) * volume
        return gross - fee

    def poll(self, equity: float, live: bool) -> None:
        self._apply_panel()
        df = fetch(self.symbol)
        if df is None:
            self.last_err = "нет истории"
            return
        self.last_err = ""
        trade = _trade_bars(df, self.hss_tf)
        prepared = self.strat.prepare(trade).reset_index(drop=True)
        # Portfolio.prepare() возвращает СЫРОЙ df — признаки (EMA, doji,
        # чистый откат) лежат в self.hss.df с тем же индексом, готовит их
        # ScalpHA.prepare() внутри Portfolio.prepare().
        self.df = self.hss.df
        closed_i = len(prepared) - 2          # последний ЗАКРЫТЫЙ бар
        self.row = self.df.iloc[closed_i]
        bar_time = str(self.row.time)

        if live:
            self._sync_live(bar_time)
        if bar_time == self.st.last_bar:
            return                            # бар уже обработан
        # Догоняем пропущенные бары (сеть моргнула, окно свернули).
        start = closed_i
        if self.st.last_bar:
            prev = prepared.index[prepared.time == pd.Timestamp(self.st.last_bar)]
            if len(prev):
                start = int(prev[0]) + 1
        for i in range(max(start, closed_i - 20), closed_i + 1):
            self._on_bar(prepared, i, equity, live)
        self.st.last_bar = bar_time

    def _on_bar(self, d: pd.DataFrame, i: int, equity: float, live: bool) -> None:
        row = d.iloc[i]
        if not live:
            self._paper_manage(row)
        in_pos = bool(self.st.position or self.st.pending
                      or self.st.pos_ticket or self.st.ticket)
        self.strat.set_equity(equity)
        bar = Bar(row.time, row.open, row.high, row.low, row.close,
                  int(row.spread), index=i, volume=float(row.tick_volume))
        sig = self.strat.on_bar(bar, in_position=in_pos)
        owner = self.strat._owner.name if self.strat._owner else "?"
        if sig is None or in_pos:
            return
        if not getattr(self, "hot", True):
            self.st.rejected += 1
            jlog(type="reject", symbol=self.symbol, strategy=owner, bar=str(row.time),
                 why="сканер: инструмент не в топе / вне выбранного рынка")
            self.strat.on_signal_rejected(sig, "сканер")
            return

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
            jlog(type="reject", symbol=self.symbol, strategy=owner, bar=str(row.time),
                 why=why, volume=sig.volume, entry=entry_px, sl=sig.sl)
            self.strat.on_signal_rejected(sig, why)
            return

        jlog(type="signal", symbol=self.symbol, strategy=owner, bar=str(row.time),
             side=sig.side.value, volume=sig.volume, entry=entry_px,
             sl=sig.sl, tp=sig.tp,
             risk_pct=round(self.risk.trade_risk_pct(
                 sig.volume, sl_dist, equity, self.spec.contract_size), 2))
        self.st.owner = owner

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

    # ── бумажный режим: правила один в один с движком бэктеста ────────
    def _paper_manage(self, row) -> None:
        p = self.st.position
        if p:
            buy = p["side"] == Side.BUY.value
            hit_sl = row.low <= p["sl"] if buy else row.high >= p["sl"]
            hit_tp = row.high >= p["tp"] if buy else row.low <= p["tp"]
            exit_price = reason = None
            if hit_sl:                     # SL приоритетнее - пессимистично
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

        q = self.st.pending
        if q and not self.st.position:
            buy = q["side"] == Side.BUY.value
            touched = row.high >= q["price"] if buy else row.low <= q["price"]
            if touched:
                # Ордер согласовывался на баре размещения — новостное окно
                # проверяем заново на баре, где он фактически исполнился.
                nb, nwhy = self.risk.news_blocked(row.time, self.symbol)
                if nb:
                    self.st.pending = None
                    self.st.expired += 1
                    self.st.owner = ""
                    jlog(type="expire", mode="paper", symbol=self.symbol,
                         strategy=q.get("strategy", "?"), bar=str(row.time),
                         price=q["price"], why=f"новость: {nwhy}")
                    return
                spread = (row.spread or 1) * self._point()
                entry = q["price"] + (spread if buy else -spread)
                self.st.position = asdict(PaperPosition(
                    side=q["side"], volume=q["volume"], entry=entry,
                    sl=q["sl"], tp=q["tp"], opened_at=str(row.time),
                    strategy=q.get("strategy", "?")))
                self.st.pending = None
                jlog(type="fill", mode="paper", symbol=self.symbol,
                     strategy=q.get("strategy", "?"), bar=str(row.time),
                     side=q["side"], volume=q["volume"], entry=entry,
                     sl=q["sl"], tp=q["tp"])
            elif str(row.time) >= q["expires_at"]:
                self.st.pending = None
                self.st.expired += 1
                self.st.owner = ""
                jlog(type="expire", mode="paper", symbol=self.symbol,
                     strategy=q.get("strategy", "?"), bar=str(row.time),
                     price=q["price"])

    # ── demo-режим: ордера уходят в терминал ─────────────────────────
    def _place_live_market(self, sig, fill: float) -> None:
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
            "symbol": self.symbol,
            "volume": vol,
            "type": mt5.ORDER_TYPE_BUY if sig.side is Side.BUY else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": round(sig.sl, info.digits),
            "tp": round(sig.tp, info.digits),
            "deviation": 20,
            "magic": MAGIC,
            "comment": "neft-fwd",
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
        self.st.pos_ticket = int(res.order or res.deal or 0)
        self.st.pending = None
        jlog(type="fill", mode="demo", symbol=self.symbol, strategy=self.st.owner,
             ticket=self.st.pos_ticket, side=sig.side.value, volume=vol,
             entry=fill, sl=sig.sl, tp=sig.tp)

    def _place_live(self, sig, expires: str) -> None:
        info = mt5.symbol_info(self.symbol)
        vol = max(info.volume_min,
                  min(round(round(sig.volume / info.volume_step) * info.volume_step, 8),
                      info.volume_max))
        req = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": self.symbol,
            "volume": vol,
            "type": (mt5.ORDER_TYPE_BUY_STOP if sig.side is Side.BUY
                     else mt5.ORDER_TYPE_SELL_STOP),
            "price": round(sig.entry, info.digits),
            "sl": round(sig.sl, info.digits),
            "tp": round(sig.tp, info.digits),
            "magic": MAGIC,
            "comment": "neft-fwd",
            "type_time": mt5.ORDER_TIME_GTC,   # срок ведём сами: надёжнее
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
             ticket=self.st.ticket, side=sig.side.value, volume=vol,
             entry=sig.entry, sl=sig.sl, tp=sig.tp)

    def _sync_live(self, bar_time: str) -> None:
        """Терминал - источник истины: смотрим, что стало с ордером и позицией."""
        if self.st.ticket:
            orders = mt5.orders_get(ticket=self.st.ticket) or ()
            if not orders:                       # исполнился либо снят
                self.st.ticket = 0
                pos = [p for p in (mt5.positions_get(symbol=self.symbol) or ())
                       if p.magic == MAGIC]
                if pos:
                    p = pos[0]
                    self.st.pos_ticket = int(p.ticket)
                    self.st.pending = None
                    jlog(type="fill", mode="demo", symbol=self.symbol,
                         strategy=self.st.owner, ticket=int(p.ticket),
                         side="buy" if p.type == 0 else "sell",
                         volume=p.volume, entry=p.price_open, sl=p.sl, tp=p.tp)
                else:
                    self.st.pending = None
                    self.st.owner = ""
            elif self.st.pending and bar_time >= self.st.pending["expires_at"]:
                mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE,
                                "order": self.st.ticket})
                self.st.ticket = 0
                self.st.pending = None
                self.st.expired += 1
                jlog(type="expire", mode="demo", symbol=self.symbol,
                     strategy=self.st.owner, bar=bar_time)
                self.st.owner = ""
            elif self.st.pending:
                # Ордер стоит у брокера с момента ДО начала новостного окна —
                # approve() его тогда пропустил. Если сейчас окно наступило,
                # снимаем ордер: сам брокер новости не знает и исполнит его
                # ровно в спайк, если цена дойдёт.
                nb, nwhy = self.risk.news_blocked(pd.Timestamp(bar_time), self.symbol)
                if nb:
                    mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE,
                                    "order": self.st.ticket})
                    self.st.ticket = 0
                    self.st.pending = None
                    self.st.expired += 1
                    jlog(type="expire", mode="demo", symbol=self.symbol,
                         strategy=self.st.owner, bar=bar_time,
                         why=f"новость: {nwhy}")
                    self.st.owner = ""

        if self.st.pos_ticket:
            alive = [p for p in (mt5.positions_get(symbol=self.symbol) or ())
                     if int(p.ticket) == self.st.pos_ticket]
            if not alive:                        # позиция закрыта брокером
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
                     pnl=round(pnl, 2), reason=reason,
                     exit=exit_d.price if exit_d else None)
                self.st.pos_ticket = 0
                self.st.owner = ""

    def _strategies_badge(self) -> str:
        h = "H" if self.routes.get("HSS") else "[dim]h[/]"
        l = "L" if self.routes.get("London S/R") else "[dim]l[/]"
        return f"{h}{l}"

    # ── строка таблицы ───────────────────────────────────────────────
    def render_row(self) -> list:
        if self.last_err or self.row is None:
            return [self.symbol, self._strategies_badge(),
                    f"[red]{self.last_err or '...'}[/]"] + [""] * 7
        r = self.row
        tick = mt5.symbol_info_tick(self.symbol)
        above = (r.ha_close > r.ema) if hasattr(r, "ha_close") else (r.close > r.ema)
        col = "clean_bear" if above else "clean_bull"
        run = 0
        d = self.df
        for i in range(len(d) - 2, 0, -1):
            if bool(d.iloc[i][col]):
                run += 1
            else:
                break
        hour = r.time.hour
        sess = self.hss.session
        if not sess or not isinstance(sess, (tuple, list)) or len(sess) != 2:
            hss_active = bool(self.routes.get("HSS"))  # 24/7 — сессия не ограничивает
        else:
            hss_lo, hss_hi = int(sess[0]), int(sess[1])
            hss_active = self.routes.get("HSS") and hss_lo <= hour < hss_hi
        # London S/R формирует зоны в лондонскую сессию, но ВХОДИТ только
        # в окне self.lsr.ny — это и есть её реальное "рабочее" время.
        lsr_lo, lsr_hi = self.lsr.ny
        lsr_active = self.routes.get("London S/R") and lsr_lo <= hour < lsr_hi

        if self.st.position or self.st.pos_ticket:
            who = f"[magenta]{self.st.owner}[/] " if self.st.owner else ""
            p = self.st.position
            if p:
                mark = tick.bid if p["side"] == Side.BUY.value else tick.ask
                float_pnl = self._pnl(p["side"], p["entry"], mark, p["volume"])
                status = f"{who}[bold cyan]в позиции {float_pnl:+.2f}[/]"
            else:
                live_pos = [x for x in (mt5.positions_get(symbol=self.symbol) or ())
                            if int(x.ticket) == self.st.pos_ticket]
                status = (f"{who}[bold cyan]в позиции {live_pos[0].profit:+.2f}[/]"
                          if live_pos else f"{who}[cyan]в позиции[/]")
        elif self.st.pending:
            who = f"[magenta]{self.st.owner}[/] " if self.st.owner else ""
            status = f"{who}[yellow]ордер @ {self.st.pending['price']:.{self.spec.digits}f}[/]"
        elif not (hss_active or lsr_active):
            status = "[dim]вне сессии[/]"
        else:
            parts = []
            if hss_active:
                done = sum([run >= self.hss.pullback_bars, bool(r.is_doji), bool(r.big_doji)])
                parts.append(f"HSS {done}/3")
            if lsr_active:
                parts.append("London S/R")
            status = "[dim]ждём: " + " · ".join(parts) + "[/]"

        n = len(self.st.trades)
        pnl_sum = sum(self.st.trades)
        wins = sum(1 for x in self.st.trades if x > 0)
        return [
            self.symbol,
            self._strategies_badge(),
            f"{tick.bid:.{self.spec.digits}f}" if tick else "-",
            f"{(tick.ask - tick.bid) / self._point():.0f}" if tick else "-",
            "[green]BUY[/]" if above else "[red]SELL[/]",
            (f"[green]{run}[/]" if run >= self.hss.pullback_bars
             else f"[dim]{run}[/]"),
            "[green]да[/]" if bool(r.is_doji) else f"[dim]{r.body_ratio:.2f}[/]",
            f"{n} ({wins}W)" if n else "[dim]0[/]",
            (f"[green]{pnl_sum:+.2f}[/]" if pnl_sum > 0
             else f"[red]{pnl_sum:+.2f}[/]" if pnl_sum < 0 else "[dim]0.00[/]"),
            f"[dim]{self.st.expired}/{self.st.rejected}[/]",
            status,
        ]


def render(watchers: list[Watcher], mode: str, acc, balance: float,
           n: int, args) -> Group:
    t = Table(box=None, pad_edge=False)
    for c, j in (("инструмент", "left"), ("страт.", "left"),
                 ("цена", "right"), ("спред, п", "right"),
                 ("тренд", "right"), ("откат", "right"), ("doji", "right"),
                 ("сделок", "right"), ("итог", "right"), ("проп./отк.", "right"),
                 ("статус", "left")):
        t.add_column(c, justify=j)
    for w in watchers:
        t.add_row(*w.render_row())

    total = sum(sum(w.st.trades) for w in watchers)
    n_tr = sum(len(w.st.trades) for w in watchers)
    wins = sum(1 for w in watchers for x in w.st.trades if x > 0)
    head = Text.assemble(
        ("ФОРВАРД-ТЕСТ", "bold cyan"), ("  ·  ", "dim"),
        ("ЕДИНАЯ МАШИНА: HSS + London S/R", "bold"), ("  ·  ", "dim"),
        (("ДЕМО: ордера уходят в терминал" if mode == "demo"
          else "БУМАГА: ордера не отправляются"),
         "bold yellow" if mode == "demo" else "green"),
        ("  ·  ", "dim"),
        (f"{acc.login} {acc.server}", "dim"), ("  ·  ", "dim"),
        (f"обновление {n}", "dim"),
    )
    foot = Text.assemble(
        (f"баланс {balance:,.2f}   ", "bold"),
        (f"сделок {n_tr}   винрейт "
         f"{(wins / n_tr * 100 if n_tr else 0):.0f}%   итог {total:+.2f}\n", ""),
        (f"риск {args.risk}% на сделку   H = HSS (Kill Zone 16-19)   "
         f"L = London S/R (11-16 / 16-23)\n"
         f"журнал: logs/forward.jsonl   свод: python scripts/forward.py --report",
         "dim"),
    )
    return Group(head, Text(""), t, Text(""), foot)


def _plain(s: str) -> str:
    return _RICH.sub("", str(s))


def _mt5_bar_ts(t) -> int:
    """MT5-сервер Bybit живёт в UTC+3 (та же особенность, что и у новостного
    фильтра — см. utc_offset_hours=3.0 в main()), бар-время в терминале —
    тоже. Панель показывает московское время как UTC+3 от честного UTC,
    поэтому здесь вычитаем 3 часа: иначе график встал бы на 3 часа вперёд
    настоящего момента."""
    ts = pd.Timestamp(t) - pd.Timedelta(hours=3)
    return int(ts.tz_localize("UTC").timestamp())


def _mt5_write_atomic(path: Path, text: str, tries: int = 15) -> bool:
    """Тот же приём, что в crypto_forward.py: панель читает файл, пока мы
    пишем — Windows роняет прямую перезапись PermissionError."""
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
    except OSError:
        return False
    for _ in range(tries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(0.05)
        except OSError:
            break
    try:
        path.write_text(text, encoding="utf-8")
        tmp.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def write_mt5_scan(hits, hot: set[tuple[str, str]]) -> None:
    """Отдельный файл сканера для CFD (logs/mt5_scanner_state.json) — не
    logs/scanner_state.json, тот же пишет crypto_forward.py в отдельном
    процессе одновременно, и два независимых процесса на один файл — гонка
    записи, кто последний, тот и победил. admin_server.py сливает оба файла
    сам при отдаче панели."""
    SCAN_STATE.parent.mkdir(exist_ok=True)
    SCAN_STATE.write_text(json.dumps({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "hot": [{
            "symbol": h.symbol, "tf": h.tf, "strategy": h.strategy,
            "score": round(h.score, 2), "why": h.why,
        } for h in hits if (h.symbol, h.tf) in hot],
        "hits": [{
            "symbol": h.symbol, "tf": h.tf, "strategy": h.strategy,
            "score": round(h.score, 2), "why": h.why,
        } for h in sorted(hits, key=lambda x: -x.score)[:12]],
    }, ensure_ascii=False), encoding="utf-8")


def write_chart_json(watchers: list[Watcher], mode: str, equity: float,
                     start: float, n: int) -> None:
    """dashboard/mt5_chart.json — тот же формат книг, что и у крипты
    (candles/ema/lines/position/pending), чтобы график и левая панель
    пар переиспользовали существующий JS без отдельной ветки на venue.
    Инструменты торгуются теми же стратегиями (HSS, London S/R) — просто
    другой рынок и другой источник котировок (терминал, не биржевой REST).
    """
    books = []
    positions = []
    for w in watchers:
        df = w.df
        candles, ema, last = [], [], 0.0
        if df is not None and len(df):
            tail = df.tail(200)
            candles = [{
                "time": _mt5_bar_ts(r.time), "open": float(r.open),
                "high": float(r.high), "low": float(r.low), "close": float(r.close),
            } for r in tail.itertuples()]
            if "ema" in tail.columns:
                ema = [{"time": _mt5_bar_ts(r.time), "value": float(r.ema)}
                      for r in tail.itertuples() if r.ema == r.ema]
            last = float(tail.close.iloc[-1])
        else:
            tick = mt5.symbol_info_tick(w.symbol)
            if tick and tick.bid:
                last = float(tick.bid)
        lines = []
        setup = None
        p = w.st.position
        q = w.st.pending
        if p:
            setup = {**p, "price": p["entry"], "kind": "position"}
        elif q:
            setup = {**q, "price": q["price"], "kind": "pending"}
        if setup:
            entry = float(setup.get("entry") or setup.get("price") or 0)
            sl = float(setup.get("sl") or 0)
            tp = float(setup.get("tp") or 0)
            if entry:
                lines.append({"price": entry, "color": "#98989f",
                              "title": "вход" if setup["kind"] == "position" else "ордер",
                              "style": 0 if setup["kind"] == "position" else 2})
            if sl:
                lines.append({"price": sl, "color": "#ff453a", "title": "SL1", "style": 2})
            if tp:
                lines.append({"price": tp, "color": "#32d74b", "title": "TP1 · 100%", "style": 2})
        status = "в позиции" if p else ("ордер" if q else (w.last_err or "рынок"))
        try:
            scan = scan_overlay(w) if df is not None and len(df) else {}
        except Exception:  # noqa: BLE001
            scan = {}
        books.append({
            "key": w.symbol, "label": w.symbol, "pair": w.symbol,
            "venue": "mt5", "tf": "1m",
            "last": last,
            "strategies": [k for k, v in (w.routes or {}).items() if v],
            "status": status, "err": w.last_err,
            "pending": q, "position": p, "setup": setup,
            "pnl": sum(w.st.trades), "pnl_open": None,
            "trades": len(w.st.trades), "candles": candles, "ema": ema,
            "lines": lines, "markers": [], "scan": scan,
        })
        if p:
            positions.append({
                "pair": w.symbol, "label": w.symbol, "tf": "1m", "venue": "mt5",
                "side": p["side"], "entry": p["entry"], "sl": p["sl"], "tp": p["tp"],
                "volume": p["volume"], "strategy": p.get("strategy"),
                "contract_size": w.spec.contract_size,
                "opened_at": p.get("opened_at"),
            })
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "n": n, "equity": equity, "start": start, "mode": mode,
        "books": books,
        "blot": {"positions": positions, "orders": [], "deals": []},
    }
    _mt5_write_atomic(CHART_JSON, json.dumps(payload, ensure_ascii=False, default=str))


def write_live_panel(watchers: list[Watcher], mode: str, acc, balance: float,
                     n: int, args) -> None:
    """Самодостаточная HTML-панель: браузер просто обновляет файл."""
    now = datetime.now()
    weekend = now.weekday() >= 5
    rows = []
    for w in watchers:
        cells = [_plain(c) for c in w.render_row()]
        while len(cells) < 11:
            cells.append("—")
        status = cells[10]
        klass = "wait"
        if "позиции" in status:
            klass = "pos"
        elif "ордер" in status:
            klass = "ord"
        elif "вне сессии" in status:
            klass = "off"
        rows.append(
            "<tr class='%s'>" % klass
            + "".join(f"<td>{c}</td>" for c in cells)
            + "</tr>"
        )
    n_tr = sum(len(w.st.trades) for w in watchers)
    total = sum(sum(w.st.trades) for w in watchers)
    wins = sum(1 for w in watchers for x in w.st.trades if x > 0)
    wr = (wins / n_tr * 100) if n_tr else 0
    note = ("Сейчас выходные: форекс/золото почти стоят. Ордера уйдут "
            "в терминал, когда откроется сессия (HSS 16–19, London S/R 16–23)."
            if weekend else
            "Демо: стоп-ордера уходят в MT5. Смотрите вкладку «Торговля».")
    html = f"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="2">
<title>NEFT демо · живой форвард</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
background:#000;color:#f5f5f7;margin:0;padding:32px;line-height:1.5;
-webkit-font-smoothing:antialiased;font-size:14px}}
h1{{font:600 22px/1.3 system-ui,sans-serif;letter-spacing:-.02em;margin:0 0 8px}}
.sub,.foot{{color:#98989f;font-size:14px;margin:0 0 20px}}
.badge{{display:inline-block;padding:4px 12px;border-radius:999px;font-size:13px;
font-weight:600;background:#ff9f0a;color:#000;margin-right:8px}}
table{{border-collapse:collapse;width:100%;background:rgba(28,28,30,.9);border-radius:16px;
overflow:hidden;box-shadow:0 8px 28px -12px rgba(0,0,0,.55);margin-top:12px}}
th,td{{padding:14px 16px;text-align:left;font:14px/1.4 ui-monospace,"SF Mono",Consolas,monospace;
border-bottom:1px solid rgba(255,255,255,.08)}}
th{{color:#98989f;font-weight:600;font-family:system-ui,sans-serif}}
tr.pos td{{background:rgba(48,209,88,.10)}}
tr.ord td{{background:rgba(255,159,10,.10)}}
tr.off td{{color:#8e8e93}}
.kpi{{display:flex;gap:28px;margin:20px 0;flex-wrap:wrap}}
.kpi b{{display:block;font-size:22px;letter-spacing:-.02em}}
.kpi span{{color:#98989f;font-size:13px;font-weight:500}}
.warn{{background:rgba(255,159,10,.12);border:1px solid rgba(255,159,10,.35);border-radius:14px;padding:14px 18px;
margin:0 0 18px;color:#ffd60a;font-size:14px;line-height:1.45}}
</style></head><body>
<p class="sub"><span class="badge">{'ДЕМО · ордера в терминал' if mode=='demo' else 'БУМАГА'}</span>
{acc.login} · {acc.server} · обновление {n} · {now.strftime('%H:%M:%S')}</p>
<h1>Единая машина · HSS + London S/R</h1>
<p class="warn">{note}</p>
<div class="kpi">
<div><span>баланс</span><b>{balance:,.2f}</b></div>
<div><span>сделок</span><b>{n_tr}</b></div>
<div><span>винрейт</span><b>{wr:.0f}%</b></div>
<div><span>итог</span><b>{total:+.2f}</b></div>
<div><span>риск</span><b>{args.risk}%</b></div>
</div>
<table>
<thead><tr><th>инструмент</th><th>страт.</th><th>цена</th><th>спред</th>
<th>тренд</th><th>откат</th><th>doji</th><th>сделок</th><th>итог</th>
<th>проп./отк.</th><th>статус</th></tr></thead>
<tbody>
{''.join(rows) or '<tr><td colspan="11">ждём первый бар…</td></tr>'}
</tbody></table>
<p class="foot">H = HSS (Kill Zone 16–19) · L = London S/R (вход 16–23) ·
риск {args.risk}% на сделку · журнал logs/forward.jsonl</p>
</body></html>
"""
    LIVE_HTML.parent.mkdir(exist_ok=True)
    LIVE_HTML.write_text(html, encoding="utf-8")


# ── свод по журналу ──────────────────────────────────────────────────
def report() -> None:
    if not JOURNAL.exists():
        con.print("[yellow]Журнала ещё нет - форвард-тест не запускался.[/]")
        return
    recs = [json.loads(x) for x in JOURNAL.read_text(encoding="utf-8").splitlines() if x]
    closes = [r for r in recs if r["type"] == "close"]
    if not closes:
        con.print(f"[yellow]Сделок пока нет. Событий в журнале: {len(recs)}[/]")

    t = Table(box=None, pad_edge=False)
    for c in ("инструмент", "сделок", "винрейт", "PF", "матожидание", "итог"):
        t.add_column(c, justify="left" if c == "инструмент" else "right")
    by_sym: dict[str, list] = {}
    for r in closes:
        by_sym.setdefault(r["symbol"], []).append(r["pnl"])
    for sym, pnls in list(by_sym.items()) + [("ВСЕГО", [p for v in by_sym.values() for p in v])]:
        w = [p for p in pnls if p > 0]
        l = [p for p in pnls if p <= 0]
        pf = (sum(w) / abs(sum(l))) if l and sum(l) else float("inf")
        c = "green" if sum(pnls) > 0 else "red"
        t.add_row(sym, str(len(pnls)),
                  f"{len(w) / len(pnls) * 100:.0f}%" if pnls else "-",
                  f"{pf:.2f}", f"{sum(pnls) / len(pnls):+.2f}" if pnls else "-",
                  f"[{c}]{sum(pnls):+.2f}[/]")
    con.print(t)

    by_strat: dict[str, list] = {}
    for r in closes:
        by_strat.setdefault(r.get("strategy", "?"), []).append(r["pnl"])
    if by_strat:
        st = Table(box=None, pad_edge=False)
        for c in ("стратегия", "сделок", "винрейт", "итог"):
            st.add_column(c, justify="left" if c == "стратегия" else "right")
        for name, pnls in by_strat.items():
            w = [p for p in pnls if p > 0]
            c = "green" if sum(pnls) > 0 else "red"
            st.add_row(name, str(len(pnls)),
                      f"{len(w) / len(pnls) * 100:.0f}%" if pnls else "-",
                      f"[{c}]{sum(pnls):+.2f}[/]")
        con.print(st)

    kinds: dict[str, int] = {}
    for r in recs:
        kinds[r["type"]] = kinds.get(r["type"], 0) + 1
    con.print("[dim]события: " + "  ".join(f"{k}={v}" for k, v in kinds.items()) + "[/]")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default=ALL_SYMBOLS,
                   help="по умолчанию — весь набор единой машины")
    p.add_argument("--risk", type=float, default=1.0)
    p.add_argument("--no-route", action="store_true",
                   help="включить обе стратегии на всех инструментах")
    p.add_argument("--balance", type=float, default=1000.0,
                   help="виртуальный депозит для бумажного режима")
    p.add_argument("--leverage", type=int, default=500)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--paper", action="store_true",
                   help="не отправлять ордера даже на демо-счёте")
    p.add_argument("--live", action="store_true",
                   help="боевой счёт: ордера уходят. Нужен DEMO_ONLY=false")
    p.add_argument("--rr", type=float, default=1.0)
    p.add_argument("--pullback", type=int, default=2)
    p.add_argument("--hss-session", default="16-19", dest="hss_session")
    p.add_argument("--hss-24h", action="store_true")
    p.add_argument("--london", default="11-16")
    p.add_argument("--ny", default="16-23")
    p.add_argument("--daily", type=float, default=10.0)
    p.add_argument("--dd", type=float, default=30.0)
    p.add_argument("--reset", action="store_true", help="забыть прошлое состояние")
    p.add_argument("--no-news", action="store_true",
                   help="выключить фильтр по новостям (по умолчанию включён)")
    p.add_argument("--report", action="store_true")
    a = p.parse_args()
    def _pair(s: str) -> tuple[int, int]:
        lo, hi = s.replace("–", "-").split("-", 1)
        return int(lo), int(hi)
    a.hss_session_tuple = None if a.hss_24h else _pair(a.hss_session)
    a.london_tuple = _pair(a.london)
    a.ny_tuple = _pair(a.ny)
    if a.report:
        report()
        return
    if not mt5.initialize():  # только уже открытый терминал, второй не запускаем
        err = f"MT5 не отвечает: {mt5.last_error()}"
        con.print(f"[red]{err}[/]")
        write_mt5_status(ok=False, error=err)
        raise SystemExit(2)
    acc = mt5.account_info()
    if acc is None:
        err = f"Нет данных счёта: {mt5.last_error()}"
        con.print(f"[red]{err}[/]")
        write_mt5_status(ok=False, error=err)
        mt5.shutdown()
        raise SystemExit(2)
    term = mt5.terminal_info()
    is_demo = acc.trade_mode != 2
    from neft.core.config import settings
    if a.live:
        if settings.demo_only:
            err = ("DEMO_ONLY=true — боевые ордера заблокированы. "
                   "Снимите предохранитель в админ-панели или в .env.")
            con.print(f"[red]{err}[/]")
            write_mt5_status(ok=False, error=err, server=acc.server, login=acc.login)
            mt5.shutdown()
            raise SystemExit(2)
        mode = "live" if not is_demo else "demo"
    elif is_demo and not a.paper:
        mode = "demo"
    else:
        mode = "paper"
    send = mode in ("demo", "live")
    if mode == "demo" and not term.trade_allowed:
        con.print("[yellow]Алготрейдинг выключен в терминале - перехожу в бумажный "
                  "режим. Включите кнопку 'Алготрейдинг' и перезапустите.[/]")
        mode, send = "paper", False
    if mode == "live" and not term.trade_allowed:
        err = "Алготрейдинг выключен — боевые ордера не отправляю."
        con.print(f"[red]{err}[/]")
        write_mt5_status(ok=False, error=err, server=acc.server, login=acc.login)
        mt5.shutdown()
        raise SystemExit(2)
    # Bybit CFD: демо-серверов нет. На Live без --live всегда paper —
    # это и есть безопасная проверка бота по NAS100 / XAUUSD+ / ….
    if not is_demo and not a.paper and not a.live:
        con.print(f"[yellow]Счёт {acc.login} ({acc.server}) боевой — ордера "
                  f"заблокированы, бумажный режим на живых CFD-котировках.[/]")
    if mt5sym.bybit_cfd_server(acc.server) and mode == "paper":
        con.print("[dim]Bybit CFD: нет демо-сервера → проверка = paper на Live "
                  "(реальные ордера не уходят).[/]")

    if a.reset and STATE.exists():
        STATE.unlink()
    saved = load_state()
    balance = acc.balance if send else saved.get("balance", a.balance)

    limits = RiskLimits(risk_per_trade_pct=a.risk, max_risk_per_trade_pct=3.0,
                        max_volume=10.0, max_daily_loss_pct=a.daily,
                        max_drawdown_pct=a.dd, max_open_positions=1,
                        min_free_margin_pct=0.0)
    risk = RiskManager(start_balance=balance, limits=limits)

    if not a.no_news:
        try:
            gate = NewsGate(buffer_minutes=5, events=load_calendar())
            # MT5-сервер Bybit живёт в UTC+3 (проверено), бар-время в
            # данных терминала — тоже. См. news.py про горизонт видимости:
            # только текущая неделя, слепая зона в ночь на новую неделю.
            risk.set_news_gate(gate, utc_offset_hours=3.0)
            con.print(f"[dim]Новостной фильтр: {len(gate.events)} событий на "
                      f"неделю, буфер ±5 мин на High-impact[/]")
        except Exception as e:
            con.print(f"[yellow]Календарь новостей недоступен ({e}) - "
                      f"фильтр выключен на этот запуск[/]")

    requested = [x.strip() for x in a.symbols.split(",") if x.strip()]
    if "NAS100" not in requested:
        requested.insert(0, "NAS100")
    resolved, missing = mt5sym.resolve_many(
        requested, tradable_only=(mode == "demo"))
    for s in missing:
        con.print(f"[red]{s}: нет у брокера - пропускаю[/]")
    if not any(req == "NAS100" for req, _ in resolved):
        err = (f"NAS100 нет на {acc.server} (логин {acc.login}). "
               "Откройте MT5 на Bybit-Live-7 — CFD Bybit есть только там, "
               "не MetaQuotes-Demo.")
        con.print(f"[red]{err}[/]")
        write_mt5_status(ok=False, error=err, server=acc.server, login=acc.login,
                         missing=missing)
        mt5.shutdown()
        raise SystemExit(2)
    if missing and not mt5sym.bybit_cfd_server(acc.server):
        con.print(
            f"[yellow]Счёт {acc.login} ({acc.server}) без части CFD. "
            f"Для полного набора (NAS100, XAUUSD+, …) нужен Bybit-Live + paper.[/]"
        )
    watchers = []
    for requested_name, broker_name in resolved:
        if requested_name != broker_name:
            con.print(f"[dim]{requested_name} → {broker_name}[/]")
        st = SymbolState(**saved.get("symbols", {}).get(broker_name, {}))
        if not st.trades and requested_name in saved.get("symbols", {}):
            st = SymbolState(**saved["symbols"][requested_name])
        watchers.append(Watcher(broker_name, a, risk, st))
    if not watchers:
        err = "Не осталось инструментов."
        con.print(f"[red]{err}[/]")
        write_mt5_status(ok=False, error=err, server=acc.server, login=acc.login)
        mt5.shutdown()
        raise SystemExit(2)
    if not any(mt5sym.canonical(w.symbol) == "NAS100" for w in watchers):
        err = "В вотчерах нет NAS100 — останов."
        con.print(f"[red]{err}[/]")
        write_mt5_status(ok=False, error=err, server=acc.server, login=acc.login)
        mt5.shutdown()
        raise SystemExit(2)

    write_mt5_status(
        ok=True, server=acc.server, login=acc.login, mode=mode,
        symbols=[w.symbol for w in watchers],
    )
    # Сразу отдать панели цены (тикер), не дожидаясь первого poll истории.
    try:
        write_chart_json(watchers, mode, balance, balance, 0)
    except Exception as e:  # noqa: BLE001
        jlog(type="error", symbol="chart_json", err=str(e))

    jlog(type="start", mode=mode, account=str(acc.login), server=acc.server,
         symbols=[w.symbol for w in watchers], risk=a.risk,
         routes={w.symbol: w.routes for w in watchers}, balance=balance)

    n = 0
    from rich.live import Live
    try:
        with Live(console=con, refresh_per_second=2, screen=False) as live:
            while True:
                n += 1
                acc_now = mt5.account_info()
                if acc_now is None:
                    if not mt5.initialize():
                        con.print(f"[red]MT5 отвалился: {mt5.last_error()}[/]")
                        break
                    acc_now = mt5.account_info()
                    if acc_now is None:
                        con.print(f"[red]Нет данных счёта: {mt5.last_error()}[/]")
                        break
                equity = acc_now.equity if send else \
                    balance + sum(sum(w.st.trades) for w in watchers)
                risk.update(equity)
                bot = load_bot_cfg()
                sc = bot.get("scanner") or {}
                scope = str(sc.get("scope") or "all")
                scan_enabled = bool(sc.get("enabled", True))
                scan_on = scan_enabled and scanner_applies(scope, "cfd")
                hot_syms: set[str] = set()
                try:
                    if scan_on:
                        hits = []
                        for w in watchers:
                            hits.extend(score_watcher(w))
                        ranked = rank_hits(
                            hits,
                            max(1, int(sc.get("top_k") or 3)),
                            float(sc.get("min_score") or 1.2),
                        )
                        hot_syms = {h.symbol for h in ranked}
                        write_mt5_scan(hits, {(h.symbol, h.tf) for h in ranked})
                    else:
                        write_mt5_scan([], set())
                except Exception as e:  # noqa: BLE001
                    jlog(type="error", symbol="mt5_scan", err=str(e))
                for w in watchers:
                    held = bool(w.st.position or w.st.pending
                                or getattr(w.st, "pos_ticket", 0)
                                or getattr(w.st, "ticket", 0))
                    w.hot = allow_new_entries(
                        scan_enabled, scope, "cfd",
                        held=held, in_hot=w.symbol in hot_syms)
                    try:
                        w.poll(equity, live=send)
                    except Exception as e:                      # noqa: BLE001
                        w.last_err = str(e)[:40]
                        jlog(type="error", symbol=w.symbol, err=str(e))
                cur = (balance + sum(sum(w.st.trades) for w in watchers)
                       if mode == "paper" else mt5.account_info().balance)
                try:
                    live.update(render(watchers, mode, acc, cur, n, a))
                except Exception as e:  # noqa: BLE001
                    jlog(type="error", symbol="render", err=str(e))
                save_state({w.symbol: w.st for w in watchers}, balance)
                try:
                    write_live_panel(watchers, mode, acc, cur, n, a)
                except Exception as e:  # noqa: BLE001
                    jlog(type="error", symbol="live_panel", err=str(e))
                try:
                    write_chart_json(watchers, mode, cur, balance, n)
                except Exception as e:  # noqa: BLE001
                    jlog(type="error", symbol="chart_json", err=str(e))
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
        mt5.shutdown()
    con.print("\n[dim]Остановлено. Состояние сохранено - можно продолжить позже.[/]")


if __name__ == "__main__":
    main()
