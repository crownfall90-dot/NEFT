"""Живой форвард крипто-машины на публичных котировках.

Ордера на биржу не уходят: ключей в .env нет, DEMO_ONLY=true. Считаем
бумагу по тем же правилам, что бэктест (стоп-вход, спред, SL/TP, комиссии
Bybit taker/maker).
Крипта торгуется в выходные — это как раз живой прогон, когда форекс стоит.

    python scripts/crypto_forward.py
    python scripts/crypto_forward.py --symbols ETH/USDT:USDT,SOL/USDT:USDT
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from html import escape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

import ccxt
import pandas as pd
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from neft.backtest.crypto_data import MAKER_FEE, TAKER_FEE, _bps, _point_for
from neft.core.config import ROOT
from neft.core.models import Side
from neft.core.tg_notify import notify as tg_notify
from neft.core.news import NewsGate, load_calendar
from neft.core.portfolio import Portfolio
from neft.core.risk import RiskLimits, RiskManager
from neft.core.machine_config import load as load_bot_cfg, account_for_mode
from neft.core.routing import (
    CRYPTO_ROUTES, CRYPTO_UNIVERSE, STRATEGY_TFS, banned, kit_from_config,
    strategy_on,
)
from neft.core.scanner import (
    allow_new_entries, rank_hits, scan_overlay, score_watcher, scanner_applies,
)
from neft.core.strategy import Bar, ClosedTrade
from neft.strategies.factory import crypto_strategy, label_for
from scripts.compare_crypto import spec_for

con = Console()
JOURNAL = ROOT / "logs" / "crypto_forward.jsonl"
STATE = ROOT / "logs" / "crypto_forward_state.json"
SCAN_STATE = ROOT / "logs" / "scanner_state.json"
PID_FILE = ROOT / "logs" / "crypto_forward.pid"
CONTROL = ROOT / "logs" / "bot_control.json"
BOT_CMD = ROOT / "logs" / "bot_cmd.json"


def bot_control() -> dict:
    if not CONTROL.exists():
        return {"entries": True, "paused": False}
    try:
        data = json.loads(CONTROL.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"entries": True, "paused": False}
    return {"entries": bool(data.get("entries", True)),
            "paused": bool(data.get("paused", False))}


def consume_reset(watchers, risk, args) -> float | None:
    """Сброс бумажного счёта к депозиту — один раз, по файлу от панели."""
    if not BOT_CMD.exists():
        return None
    try:
        data = json.loads(BOT_CMD.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = {}
    try:
        BOT_CMD.unlink()
    except OSError:
        pass
    if not data.get("reset_equity"):
        return None
    if any(w.st.position or w.st.pending for w in watchers):
        return None
    dep = float(data.get("deposit") or args.balance)
    for w in watchers:
        w.st.trades.clear()
    risk.reset_book(dep)
    args.balance = dep
    return dep


def write_pid(mode: str) -> None:
    PID_FILE.parent.mkdir(exist_ok=True)
    PID_FILE.write_text(
        json.dumps({"pid": os.getpid(), "mode": mode}, ensure_ascii=False),
        encoding="utf-8",
    )


def _read_pid_file() -> tuple[int, str | None]:
    if not PID_FILE.exists():
        return 0, None
    try:
        raw = PID_FILE.read_text(encoding="utf-8").strip()
        if raw.startswith("{"):
            data = json.loads(raw)
            return int(data.get("pid") or 0), data.get("mode")
        return int(raw.splitlines()[0]), None
    except (ValueError, json.JSONDecodeError, OSError):
        return 0, None


def clear_pid() -> None:
    try:
        pid, _ = _read_pid_file()
        if pid == os.getpid():
            PID_FILE.unlink()
    except OSError:
        pass
LIVE_HTML = ROOT / "dashboard" / "crypto_live.html"
CHART_JSON = ROOT / "dashboard" / "crypto_chart.json"


def write_atomic(path: Path, text: str, tries: int = 15) -> bool:
    """Windows: os.replace падает с PermissionError, пока панель читает файл.

    Панель опрашивает crypto_chart.json каждые 2 секунды, поэтому коллизия —
    норма, а не сбой. Ждём, пока читатель отпустит файл, и только потом
    сдаёмся. Без этого график молча замирает на последней удачной записи.
    """
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
    try:  # запасной путь: пишем на месте, читатель переживёт короткий разрыв
        path.write_text(text, encoding="utf-8")
        tmp.unlink(missing_ok=True)
        return True
    except OSError as e:
        con.print(f"[red]не записал {path.name}: {e}[/]")
        jlog(type="error", symbol=path.name, err=str(e))
        return False
CHART_PORT = 8788
PREPARE = 800
TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900,
              "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
TICKER_TTL = 3.0  # сек: 1m/5m/15m одной монеты делят один тикер
SHORT = {
    "ETH/USDT:USDT": "ETH", "BNB/USDT:USDT": "BNB",
    "XRP/USDT:USDT": "XRP", "BCH/USDT:USDT": "BCH", "HYPE/USDT:USDT": "HYPE",
    "SOL/USDT:USDT": "SOL", "DOGE/USDT:USDT": "DOGE", "ZEC/USDT:USDT": "ZEC",
    "SUI/USDT:USDT": "SUI", "AVAX/USDT:USDT": "AVAX", "LINK/USDT:USDT": "LINK",
    "ADA/USDT:USDT": "ADA", "LTC/USDT:USDT": "LTC", "NEAR/USDT:USDT": "NEAR",
    "APT/USDT:USDT": "APT", "ARB/USDT:USDT": "ARB",
    "TRX/USDT:USDT": "TRX", "SEI/USDT:USDT": "SEI", "ATOM/USDT:USDT": "ATOM",
    "INJ/USDT:USDT": "INJ", "UNI/USDT:USDT": "UNI",
    # На бирже контракт правда называется 1000PEPE (ребейз 1000x) — обрезать
    "1000PEPE/USDT:USDT": "1000PEPE",
}


def utc_iso(t) -> str:
    """Биржевой бар — UTC. Без суффикса JS считает строку локальным временем."""
    ts = pd.Timestamp(t)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def jlog(**rec) -> None:
    rec["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    JOURNAL.parent.mkdir(exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def sl_why(strategy: str, reason: str = "") -> str:
    name = strategy or ""
    if "HSS" in name:
        return "за фитилём сигнальной doji на этом ТФ"
    if "London" in name:
        return "за экстремумом после касания уровня Лондона"
    if "Flow" in name:
        return "за ORB или баром сессии"
    if "Squeeze" in name:
        return "за противоположным свингом треугольника"
    if "Breakout" in name:
        return "за свечой пробоя лондонского бокса"
    return reason or "по структуре этого сетапа"


def rr_map_from_cfg(bot: dict) -> dict:
    """R:R каждой стратегии из настроек панели.

    У стратегий поле называется по-разному: у HSS/Breakout/Flow это "rr"
    (цель = rr × риск), у London S/R и Squeeze — "min_rr" (порог, ниже
    которого сделка не берётся). Для панели это одно и то же число
    «сколько получаем на единицу риска», поэтому читаем оба имени.
    """
    st = (bot or {}).get("strategies") or {}
    out: dict[str, float] = {}
    for key in ("hss", "london_sr", "breakout", "squeeze", "session_flow"):
        cfg = st.get(key) or {}
        raw = cfg.get("rr", cfg.get("min_rr"))
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val > 0:
            out[key] = val
    return out


def price_ladder(entry: float, sl: float, tp: float, side: str) -> tuple[list[float], list[float]]:
    """Три стопа и три тейка от структурного SL и финального TP."""
    buy = str(side).lower() == "buy"
    sign = 1.0 if buy else -1.0
    entry_f, sl_f = float(entry), float(sl)
    tp_f = float(tp or 0)
    risk = abs(entry_f - sl_f)
    if risk <= 0:
        return [sl_f, sl_f, entry_f], [tp_f or entry_f] * 3
    tps = [entry_f + sign * risk * r for r in (0.6, 1.2, 1.8)]
    # Целевой TP стратегии берём только если он ДАЛЬШЕ второго уровня.
    # Иначе лестница выстраивалась вверх ногами: у сделок с тесной целью
    # (напр. ARB 1m: SL 0.95R против TP 0.36R) TP3 оказывался ближе входа,
    # чем TP1, куски закрывались вплотную к цене входа и после комиссий
    # уходили в минус — в Telegram это выглядело как "🎯 TP1 ... итог −0.45",
    # то есть тейк-профит, закрывшийся убытком.
    if tp_f and (tp_f - tps[1]) * sign > 0:
        tps[2] = tp_f
    sls = [sl_f, entry_f - sign * risk * 0.5, entry_f]
    return sls, tps


def _near_px(a: float, b: float) -> bool:
    if not a or not b:
        return False
    return abs(float(a) - float(b)) <= max(abs(float(b)) * 1e-4, 1e-8)


def sl_label(i: int, px: float, entry: float, sls: list, tps: list,
             taken: list | None) -> str:
    p = float(px)
    entry = float(entry or 0)
    taken = taken or [False, False, False]
    if _near_px(p, entry):
        return "безубыток"
    if tps and len(taken) > 1 and taken[1] and _near_px(p, tps[0]):
        return "замок TP1"
    if i == 1 or (len(sls) > 1 and _near_px(p, sls[1])):
        return "стоп ближе"
    return "SL1"


def tp_part_pct(i: int) -> int:
    return 34 if i == 2 else 33


def tp_label(i: int) -> str:
    return f"TP{i + 1} · {tp_part_pct(i)}%"


def dedupe_chart_lines(lines: list[dict]) -> list[dict]:
    rank = {"ордер": 1, "вход": 2, "SL1": 3, "стоп ближе": 4, "безубыток": 5, "замок TP1": 5}
    for i in range(3):
        rank[tp_label(i)] = 3
    by_px: dict[str, dict] = {}
    for ln in lines:
        px = float(ln.get("price") or 0)
        if not px:
            continue
        k = f"{px:.12f}"
        t = str(ln.get("title") or "")
        ex = by_px.get(k)
        if ex and rank.get(ex.get("title"), 0) >= rank.get(t, 0):
            continue
        by_px[k] = ln
    return list(by_px.values())


def sl_lines_for_setup(setup: dict) -> list[dict]:
    ensure_ladder(setup)
    entry = float(setup.get("entry") or setup.get("price") or 0)
    sls = list(setup.get("sls") or [])
    tps = list(setup.get("tps") or [])
    taken = list(setup.get("taken") or [False, False, False])
    active = float(setup.get("sl") or (sls[0] if sls else 0))
    rank = {"безубыток": 3, "замок TP1": 3, "стоп ближе": 2, "SL1": 1}
    by_price: dict[str, dict] = {}

    def add(px, title: str) -> None:
        p = float(px)
        if not p:
            return
        k = f"{p:.12f}"
        ex = by_price.get(k)
        if ex and rank.get(ex["title"], 0) >= rank.get(title, 0):
            return
        color = "#98989f" if title == "безубыток" else "#ff453a"
        by_price[k] = {"price": p, "color": color, "title": title, "style": 2}

    for i, px in enumerate(sls):
        if px:
            add(px, sl_label(i, px, entry, sls, tps, taken))
    if active and not any(_near_px(active, v["price"]) for v in by_price.values()):
        t = "стоп"
        if _near_px(active, entry):
            t = "безубыток"
        elif tps and _near_px(active, tps[0]):
            t = "замок TP1"
        elif sls and _near_px(active, sls[0]):
            t = "SL1"
        elif len(sls) > 1 and _near_px(active, sls[1]):
            t = "стоп ближе"
        add(active, t)
    return list(by_price.values())


def ensure_ladder(obj: dict | None) -> dict | None:
    if not obj:
        return obj
    if obj.get("sls") and obj.get("tps") and len(obj["sls"]) >= 3 and len(obj["tps"]) >= 3:
        obj.setdefault("taken", [False, False, False])
        return obj
    entry = float(obj.get("entry") or obj.get("price") or 0)
    sls, tps = price_ladder(entry, float(obj.get("sl") or 0),
                            float(obj.get("tp") or 0), obj.get("side", "buy"))
    obj["sls"], obj["tps"] = sls, tps
    obj.setdefault("taken", [False, False, False])
    return obj


@dataclass
class PaperPending:
    side: str
    volume: float
    price: float
    sl: float
    tp: float
    expires_at: str
    strategy: str = "?"
    sl_why: str = ""
    reason: str = ""
    sls: list = field(default_factory=list)
    tps: list = field(default_factory=list)


@dataclass
class PaperPosition:
    side: str
    volume: float
    entry: float
    sl: float
    tp: float
    opened_at: str
    strategy: str = "?"
    sl_why: str = ""
    reason: str = ""
    sls: list = field(default_factory=list)
    tps: list = field(default_factory=list)
    taken: list = field(default_factory=lambda: [False, False, False])
    orig_volume: float = 0.0
    realized_by_tp: list = field(default_factory=lambda: [None, None, None])


@dataclass
class SymbolState:
    last_bar: str = ""
    pending: dict | None = None
    position: dict | None = None
    trades: list = field(default_factory=list)
    expired: int = 0
    rejected: int = 0
    owner: str = ""


def load_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        # save_state() писала не атомарно — принудительный килл (панель
        # шлёт TerminateProcess без grace period при "Стоп") ровно в
        # момент записи оставлял битый/усечённый JSON, и следующий запуск
        # падал тут же на старте, даже не начав торговать, а открытые
        # позиции (если были) оставались вообще без софтверного контроля.
        # Файл теперь пишется атомарно (см. write_atomic), но на случай
        # повреждения по другой причине (диск, антивирус) — не падать,
        # начать с чистого состояния и громко сказать об этом в журнал.
        con.print(f"[red]crypto_forward_state.json повреждён, начинаю "
                  f"с пустого состояния: {e}[/]")
        jlog(type="error", symbol="state", err=f"corrupt state file: {e}")
        return {}


def save_state(states: dict, balance: float) -> None:
    write_atomic(STATE, json.dumps(
        {"balance": balance,
         "symbols": {k: asdict(v) for k, v in states.items()}},
        ensure_ascii=False, indent=1))


def _orphan_fills_from_journal(limit: int = 8000) -> list[dict]:
    """Входы без закрытия в журнале — позиции, потерянные из state."""
    if not JOURNAL.exists():
        return []
    open_fills: dict[str, dict] = {}
    try:
        lines = JOURNAL.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    for line in lines:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = r.get("type")
        sym = r.get("symbol") or ""
        tf = r.get("tf") or ""
        if not sym or not tf:
            coin = r.get("coin") or ""
            if coin and not sym:
                sym = f"{coin}/USDT:USDT"
        key = f"{sym}|{tf}"
        if t == "fill" and r.get("entry") is not None and r.get("volume"):
            open_fills[key] = r
        elif t == "close":
            rem = r.get("remaining")
            if rem is None or float(rem or 0) <= 1e-12:
                open_fills.pop(key, None)
    return list(open_fills.values())


def restore_orphaned_fills(watchers: list) -> int:
    """Вернуть в память позиции, которые есть во входах журнала, но нет в state."""
    by_key = {w.key: w for w in watchers}
    n = 0
    for r in _orphan_fills_from_journal():
        key = f"{r.get('symbol')}|{r.get('tf')}"
        w = by_key.get(key)
        if not w or w.st.position:
            continue
        fill_mode = r.get("mode") or "paper"
        bot_mode = getattr(w.args, "mode", "paper")
        if fill_mode == "live" or bot_mode == "live":
            if fill_mode != bot_mode:
                continue
        elif fill_mode != bot_mode and not (
                fill_mode in ("paper", "demo") and bot_mode in ("paper", "demo")):
            continue
        side = r.get("side") or "buy"
        entry = float(r["entry"])
        vol = float(r["volume"])
        sls = r.get("sls") or [r.get("sl") or entry]
        tps = r.get("tps") or [r.get("tp") or entry]
        sls = [float(x) for x in sls]
        tps = [float(x) for x in tps]
        while len(sls) < 3:
            sls.append(sls[-1])
        while len(tps) < 3:
            tps.append(tps[-1])
        w.st.position = asdict(PaperPosition(
            side=side, volume=vol, entry=entry, sl=sls[0], tp=tps[-1],
            opened_at=r.get("opened_at") or r.get("bar") or "",
            strategy=r.get("strategy") or "?",
            sl_why=r.get("sl_why") or "",
            sls=sls, tps=tps,
            taken=list(r.get("taken") or [False, False, False]),
            orig_volume=vol))
        w.st.pending = None
        w.st.owner = r.get("strategy") or ""
        n += 1
        jlog(type="restore", symbol=w.symbol, tf=w.tf, coin=r.get("coin"),
             side=side, entry=entry, volume=vol, why="вход без закрытия в журнале")
    return n


def open_exchange(prefer: str) -> ccxt.Exchange:
    last = None
    for name in (prefer, "binance", "bybit"):
        try:
            ex = getattr(ccxt, name)({
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            })
            ex.load_markets()
            return ex
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"нет публичного доступа к свопам: {last}")


def to_df(symbol: str, raw: list) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["time"] = pd.to_datetime(df.ts, unit="ms")
    df["tick_volume"] = df.volume
    point = _point_for(float(df.close.median()))
    df["spread"] = (df.close * _bps(symbol) / 10_000 / point).round().astype(int)
    return df[["time", "open", "high", "low", "close", "tick_volume", "spread"]]


class Feed:
    def __init__(self, exchanges: dict[str, ccxt.Exchange], home: str):
        self.exchanges = exchanges
        self.home = home
        self.where: dict[str, str] = {}
        self.cache: dict[tuple[str, str], pd.DataFrame] = {}
        self.fetched: dict[tuple[str, str], float] = {}
        self.ticks: dict[str, tuple[float, tuple[float, float]]] = {}

    def _keep(self, symbol: str, px: tuple[float, float]) -> tuple[float, float]:
        self.ticks[symbol] = (time.time(), px)
        return px

    def place(self, symbol: str) -> str | None:
        if symbol in self.where:
            return self.where[symbol]
        order = [self.home] + [n for n in self.exchanges if n != self.home]
        for name in order:
            ex = self.exchanges.get(name)
            if ex is None:
                continue
            if symbol in ex.markets:
                self.where[symbol] = name
                return name
        return None

    @staticmethod
    def _bar_due(df: pd.DataFrame, tf: str) -> bool:
        """Мог ли с прошлого запроса закрыться новый бар этого таймфрейма.

        Опрашивать 15m каждые пять секунд бессмысленно: 69 наблюдателей дают
        69 REST-запросов на круг, и цикл растягивается на минуту с лишним.
        Пропускаем запрос ТОЛЬКО когда следующий бар ещё не открылся — так
        ни один вход не может опоздать. Запас 2 секунды на рассинхрон часов.
        """
        secs = TF_SECONDS.get(tf)
        if not secs:
            return True  # незнакомый таймфрейм — не рискуем, тянем всегда
        try:
            last = pd.Timestamp(df.time.iloc[-1])
        except (IndexError, ValueError, TypeError):
            return True
        if last.tzinfo is None:  # to_df даёт наивный UTC
            last = last.tz_localize("UTC")
        return time.time() >= last.timestamp() + secs - 2

    def bars(self, symbol: str, tf: str) -> pd.DataFrame | None:
        host = self.place(symbol)
        if host is None:
            return None
        ex = self.exchanges[host]
        key = (symbol, tf)
        cached = self.cache.get(key)
        if cached is not None and len(cached) and not self._bar_due(cached, tf):
            return cached  # новый бар физически ещё не мог закрыться
        try:
            if key not in self.cache:
                raw = ex.fetch_ohlcv(symbol, tf, limit=PREPARE)
                if not raw or len(raw) < 200:
                    return None
                self.cache[key] = to_df(symbol, raw)
            else:
                raw = ex.fetch_ohlcv(symbol, tf, limit=8)
                add = to_df(symbol, raw)
                old = self.cache[key]
                self.cache[key] = (pd.concat([old, add])
                                   .drop_duplicates("time")
                                   .sort_values("time")
                                   .tail(PREPARE)
                                   .reset_index(drop=True))
        except Exception:  # noqa: BLE001
            return self.cache.get(key)
        return self.cache[key]

    def ticker(self, symbol: str, max_age: float = TICKER_TTL) -> tuple[float, float] | None:
        """Текущая цена. Кэш на несколько секунд — это не срез углов.

        Таблица рисует 69 строк за круг, но 1m/5m/15m одной монеты — один и
        тот же тикер, то есть 46 запросов из 69 были дублями. Цена отсюда идёт
        на отображение, переоценку и внутрибаровые TP/SL (пока свеча не закрылась).
        Входы по сигналу — по закрытым барам из bars().
        """
        host = self.place(symbol)
        if host is None:
            return None
        hit = self.ticks.get(symbol)
        if hit is not None and time.time() - hit[0] <= max_age:
            return hit[1]
        try:
            t = self.exchanges[host].fetch_ticker(symbol)
            bid, ask = t.get("bid"), t.get("ask")
            if bid and ask:
                return self._keep(symbol, (float(bid), float(ask)))
            last = t.get("last")
            if last:
                return self._keep(symbol, (float(last), float(last)))
        except Exception:  # noqa: BLE001
            pass
        df = self.cache.get((symbol, "1m"))
        if df is None:
            df = self.cache.get((symbol, "5m"))
        if df is not None and len(df):
            px = float(df.close.iloc[-1])
            return px, px
        return None


class Watcher:
    def __init__(self, symbol: str, tf: str, routes: list[dict], args,
                 risk: RiskManager, state: SymbolState, feed: Feed):
        self.symbol = symbol
        self.tf = tf
        self.key = f"{symbol}|{tf}"
        self.args = args
        self.risk = risk
        self.st = state
        self.feed = feed
        self.last_err = ""
        self.df: pd.DataFrame | None = None
        self.row = None
        self.row_cache: list[str] | None = None
        self.hot = True
        self.book_open = 0
        self.last_mark: float | None = None
        mid = 3_000.0
        tick = feed.ticker(symbol)
        if tick:
            mid = (tick[0] + tick[1]) / 2
        self.spec = spec_for(symbol, mid)
        port = Portfolio()
        self.route_names = []
        flow_cfg = getattr(args, "flow_cfg", None)
        rr = float(getattr(args, "rr", 1.0))
        rr_by = getattr(args, "rr_by_strategy", None)
        pb = int(getattr(args, "pullback", 2))
        play = next((r for r in routes if r["strategy"] in ("Playbook", "All")), None)
        if play is not None:
            self.strat = crypto_strategy(
                play, rr=rr, risk=args.risk, risk_manager=risk, spec=self.spec,
                pullback=pb, flow=flow_cfg, rr_by_strategy=rr_by)
            self.route_names = [label_for({**play, "tf": tf})]
        else:
            for r in routes:
                name = label_for(r)
                self.route_names.append(name)
                strat = crypto_strategy(
                    r, rr=rr, risk=args.risk, risk_manager=risk, spec=self.spec,
                    pullback=pb, flow=flow_cfg, rr_by_strategy=rr_by)
                port.add(strat, name)
            self.strat = port
        self.hss = next((s.strategy for s in getattr(self.strat, "slots", [])
                         if s.name.startswith("HSS")), None)

    def _point(self) -> float:
        return self.spec.point

    def _pnl(self, side: str, entry: float, exit_: float, volume: float,
             reason: str | None = None) -> float:
        """Gross − комиссии Bybit (как в backtest/engine.py).

        reason=None — нереализованный PnL: только тейкер на входе уже «сгорел».
        tp* — выход лимитом (мейкер), sl и прочее — тейкер на выходе.
        """
        cs = self.spec.contract_size
        entry = float(entry)
        exit_ = float(exit_)
        volume = float(volume)
        d = 1 if side == Side.BUY.value else -1
        gross = (exit_ - entry) * d * volume * cs
        taker_lot = TAKER_FEE * entry * cs
        maker_lot = MAKER_FEE * entry * cs
        entry_fee = taker_lot * volume
        if reason is None:
            return gross - entry_fee
        exit_lot = maker_lot if str(reason).startswith("tp") else taker_lot
        return gross - entry_fee - exit_lot * volume

    def _paper_manage_live(self, row) -> None:
        """TP/SL по живой цене внутри текущей свечи.

        На графике фитиль часто касается TP1 до закрытия 5m — раньше
        проверка шла только на закрытом баре, и тейк «висел».
        """
        tick = self.feed.ticker(self.symbol)
        if not tick:
            return
        bid, ask = float(tick[0]), float(tick[1])
        live = row.copy()
        live.high = max(float(row.high), bid, ask)
        live.low = min(float(row.low), bid, ask)
        self._paper_manage(live)

    def poll(self, equity: float) -> None:
        df = self.feed.bars(self.symbol, self.tf)
        if df is None or len(df) < 200:
            self.last_err = "нет истории / нет рынка"
            return
        self.last_err = ""
        prepared = self.strat.prepare(df).reset_index(drop=True)
        self.df = self.hss.df if self.hss is not None else prepared
        if self.hss is None:
            # London S/R не пишет hss.df — берём подготовленный портфелем кадр.
            self.df = prepared
            if hasattr(self.strat.slots[0].strategy, "df") and \
                    self.strat.slots[0].strategy.df is not None:
                self.df = self.strat.slots[0].strategy.df
        closed_i = len(prepared) - 2
        if closed_i < 0:
            return
        # last_bar всегда с prepared — иначе hss.df и портфель расходятся,
        # и один и тот же бар торгуется на каждом опросе.
        self.row = prepared.iloc[closed_i]
        bar_time = utc_iso(self.row.time)
        if self.st.last_bar:
            try:
                self.st.last_bar = utc_iso(self.st.last_bar)
            except (ValueError, TypeError):
                pass
        if bar_time == self.st.last_bar:
            # Позиция/ордер: TP/SL по live-цене текущей (ещё не закрытой) свечи.
            if self.st.position or self.st.pending:
                forming = prepared.iloc[-1]
                self._paper_manage_live(forming)
            return
        # Только новый закрытый бар. Не догоняем историю — иначе тот же
        # Squeeze 15m открывается снова при каждом Тест.
        if self.st.last_bar:
            self._on_bar(prepared, closed_i, equity)
        else:
            self._paper_manage(self.row)
        self.st.last_bar = bar_time

    def _on_bar(self, d: pd.DataFrame, i: int, equity: float) -> None:
        row = d.iloc[i]
        self._paper_manage(row)
        in_pos = bool(self.st.position or self.st.pending)
        self.strat.set_equity(equity)
        bar = Bar(row.time, row.open, row.high, row.low, row.close,
                  int(row.spread), index=i, volume=float(row.tick_volume))
        sig = self.strat.on_bar(bar, in_position=in_pos)
        owner = self.strat._owner.name if self.strat._owner else "?"
        if sig is None or in_pos:
            return
        if not getattr(self, "allow_entries", True):
            return
        if not getattr(self, "hot", True):
            self.st.rejected += 1
            jlog(type="reject", symbol=self.symbol, tf=self.tf, strategy=owner,
                 bar=utc_iso(row.time), why="сканер: монета не в топе сетапов")
            self.strat.on_signal_rejected(sig, "сканер")
            return
        sl_dist = abs((sig.entry or row.close) - sig.sl)
        margin = sig.volume * self.spec.contract_size * abs(sig.entry or row.close) / self.args.leverage
        free = float(getattr(self, "free_margin", equity) or equity)
        ok, why = self.risk.approve(
            sig, equity=equity, free_margin=free, required_margin=margin,
            open_positions=getattr(self, "book_open", 0), sl_distance=sl_dist,
            contract_size=self.spec.contract_size,
            when=row.time, symbol=self.symbol)
        if not ok:
            self.st.rejected += 1
            jlog(type="reject", symbol=self.symbol, tf=self.tf, strategy=owner,
                 bar=utc_iso(row.time), why=why)
            self.strat.on_signal_rejected(sig, why)
            return
        entry_px = float(sig.entry) if sig.entry is not None else float(row.close)
        if sig.entry_type == "market" or sig.entry is None:
            spread = (row.spread or 1) * self._point()
            buy = sig.side is Side.BUY
            fill = entry_px + (spread if buy else -spread)
            if not self._open_fill(sig.side.value, sig.volume, fill, sig.sl, sig.tp,
                                  utc_iso(row.time), owner, sl_why(owner, sig.reason)):
                return
            return
        expires = utc_iso(d.iloc[min(i + sig.expire_bars, len(d) - 1)].time)
        why = sl_why(owner, sig.reason)
        sls, tps = price_ladder(entry_px, sig.sl, sig.tp, sig.side.value)
        jlog(type="signal", symbol=self.symbol, tf=self.tf, strategy=owner,
             bar=utc_iso(row.time), side=sig.side.value, volume=sig.volume,
             entry=entry_px, sl=sig.sl, tp=sig.tp, sls=sls, tps=tps,
             sl_why=why, reason=sig.reason)
        self.st.owner = owner
        self.st.pending = asdict(PaperPending(
            side=sig.side.value, volume=sig.volume, price=entry_px,
            sl=sig.sl, tp=sig.tp, expires_at=expires, strategy=owner,
            sl_why=why, reason=sig.reason, sls=sls, tps=tps))

    def _open_fill(self, side: str, volume: float, fill: float,
                   sl: float, tp: float, opened_at: str, owner: str,
                   why: str = "") -> bool:
        mode = getattr(self.args, "mode", "paper")
        ticket = ""
        broker = getattr(self.args, "broker", None)
        if mode == "live" and broker is not None:
            try:
                ticket = broker.market_order(
                    self.symbol, Side(side), volume, sl=sl, tp=tp, comment=owner[:24])
            except Exception as e:  # noqa: BLE001
                self.st.rejected += 1
                jlog(type="reject", symbol=self.symbol, tf=self.tf, strategy=owner,
                     why=f"биржа: {e}")
                return False
        why = why or sl_why(owner)
        sls, tps = price_ladder(fill, sl, tp, side)
        self.st.position = asdict(PaperPosition(
            side=side, volume=volume, entry=fill, sl=sls[0], tp=tps[2],
            opened_at=opened_at, strategy=owner, sl_why=why,
            sls=sls, tps=tps, taken=[False, False, False], orig_volume=volume))
        self.st.pending = None
        self.st.owner = owner
        coin = SHORT.get(self.symbol, self.symbol.split("/")[0])
        jlog(type="fill", mode=mode, symbol=self.symbol, tf=self.tf,
             coin=coin,
             strategy=owner, bar=opened_at, side=side,
             entry=fill, sl=sls[0], tp=tps[2], sls=sls, tps=tps,
             volume=volume, ticket=ticket or None, sl_why=why,
             opened_at=opened_at)
        if mode in ("demo", "live"):
            side_ru = "покупка" if side == "buy" else "продажа"
            emoji = "🟢" if side == "buy" else "🔴"
            risk_d = abs(fill - sls[0])
            rr = (abs(tps[2] - fill) / risk_d) if risk_d > 0 else 0
            tg_notify(
                f"{emoji} <b>Вход · {coin} {self.tf}</b>\n"
                f"{side_ru} · {owner}\n"
                f"цена {fill:g} · объём {volume:g}\n"
                f"SL {sls[0]:g} · TP {tps[0]:g} / {tps[1]:g} / {tps[2]:g}\n"
                f"R:R {rr:.2f}"
            )
        return True

    def _close_part(self, p: dict, volume: float, exit_price: float,
                    reason: str, row, keep: bool) -> None:
        pnl = self._pnl(p["side"], p["entry"], exit_price, volume, reason)
        cs = self.spec.contract_size
        entry_px = float(p["entry"])
        taker_lot = TAKER_FEE * entry_px * cs
        maker_lot = MAKER_FEE * entry_px * cs
        exit_lot = maker_lot if str(reason).startswith("tp") else taker_lot
        fee = round((taker_lot + exit_lot) * volume, 4)
        self.st.trades.append(pnl)
        if str(reason).startswith("tp"):
            idx = int(str(reason)[2:] or 1) - 1
            rb = p.get("realized_by_tp")
            if not isinstance(rb, list) or len(rb) < 3:
                rb = [None, None, None]
            if 0 <= idx < len(rb):
                rb[idx] = round(pnl, 2)
            p["realized_by_tp"] = rb
        left = float(p["volume"]) - volume
        mode = getattr(self.args, "mode", "paper")
        coin = SHORT.get(self.symbol, self.symbol.split("/")[0])
        jlog(type="close", mode=mode,
             symbol=self.symbol, tf=self.tf,
             coin=coin,
             strategy=p.get("strategy", "?"), bar=utc_iso(row.time),
             side=p["side"], entry=p["entry"], exit=exit_price,
             volume=volume, pnl=round(pnl, 2), fee=fee, reason=reason,
             opened_at=p.get("opened_at"), closed_at=utc_iso(row.time),
             remaining=round(left, 8) if keep and left > 1e-12 else 0)
        if mode in ("demo", "live"):
            label = _close_mark_label(reason, float(p["entry"]), exit_price)
            # Эмодзи — по фактическому результату, а не по номеру уровня:
            # "🎯 TP1 ... итог −0.45 $" читалось как успех, хотя это убыток.
            if pnl > 0.005:
                emoji = "🟩"
            elif pnl < -0.005:
                emoji = "🟥"
            else:
                emoji = "⬜"
            pnl_s = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
            side_ru = "продажа" if p["side"] == "sell" else "покупка"
            left_txt = (f"\nостаток {left:g}" if keep and left > 1e-12
                        else "\nпозиция закрыта")
            tg_notify(
                f"{emoji} <b>{label} · {coin} {self.tf}</b>\n"
                f"{side_ru} · {p.get('strategy','?')}\n"
                f"{float(p['entry']):g} → {float(exit_price):g} · объём {volume:g}\n"
                f"<b>итог {pnl_s} $</b> · комиссия {fee:.2f} $"
                f"{left_txt}"
            )
        self.strat.on_trade_closed(ClosedTrade(
            side=Side(p["side"]), volume=volume, entry=p["entry"],
            exit=exit_price, pnl=pnl, reason=reason, bars_held=0))
        if keep and left > 1e-12:
            p["volume"] = left
            taken = p.get("taken") or [False, False, False]
            sls, tps = p.get("sls") or [p["sl"]], p.get("tps") or [p["tp"]]
            if taken[1]:
                p["sl"] = tps[0]
            elif taken[0]:
                p["sl"] = sls[2] if len(sls) > 2 else p["entry"]
            nxt = next((tps[i] for i in range(len(tps)) if not taken[i]), tps[-1])
            p["tp"] = nxt
        else:
            self.st.position = None
            self.st.owner = ""

    def _paper_manage(self, row) -> None:
        p = self.st.position
        if p:
            ensure_ladder(p)
            buy = p["side"] == Side.BUY.value
            sls, tps = p["sls"], p["tps"]
            taken = list(p.get("taken") or [False, False, False])
            entry = float(p["entry"])
            # Лестница стопа: исходный → после 0.5R подтянуть ближе →
            # после TP1 в безубыток → после TP2 на TP1 (закрываем риск).
            working_sl = float(sls[0])
            risk = abs(entry - working_sl)
            if not taken[0] and risk > 0:
                mfe = (float(row.high) - entry) if buy else (entry - float(row.low))
                if mfe >= 0.5 * risk and len(sls) > 1:
                    working_sl = float(sls[1])  # ближе к входу, часть риска снята
            if taken[0]:
                working_sl = float(sls[2] if len(sls) > 2 else entry)  # BE
            if taken[1]:
                working_sl = float(tps[0])  # замок на TP1
            # Стоп только ужесточаем, никогда не отдаём назад.
            cur = float(p.get("sl") or working_sl)
            if buy:
                working_sl = max(cur, working_sl)
            else:
                working_sl = min(cur, working_sl)
            p["sl"] = working_sl
            nxt = next((tps[i] for i in range(3) if not taken[i]), tps[-1])
            p["tp"] = nxt
            hit_sl = row.low <= working_sl if buy else row.high >= working_sl
            if hit_sl and float(p["volume"]) > 0:
                self._close_part(p, float(p["volume"]), working_sl, "sl", row, keep=False)
            else:
                remaining = float(p["volume"])
                for i in range(3):
                    if taken[i] or remaining <= 0 or not self.st.position:
                        continue
                    tp_i = tps[i]
                    hit = row.high >= tp_i if buy else row.low <= tp_i
                    if not hit:
                        continue
                    part = remaining / (3 - i)
                    taken[i] = True
                    p["taken"] = taken
                    keep = remaining - part > 1e-12
                    self._close_part(p, part, tp_i, f"tp{i + 1}", row, keep=keep)
                    remaining = float(self.st.position["volume"]) if self.st.position else 0
        q = self.st.pending
        if q and not self.st.position:
            buy = q["side"] == Side.BUY.value
            touched = row.high >= q["price"] if buy else row.low <= q["price"]
            if touched:
                nb, nwhy = self.risk.news_blocked(row.time, self.symbol)
                if nb:
                    self.st.pending = None
                    self.st.expired += 1
                    self.st.owner = ""
                    jlog(type="expire", symbol=self.symbol, tf=self.tf,
                         why=f"новость: {nwhy}")
                    return
                spread = (row.spread or 1) * self._point()
                entry = q["price"] + (spread if buy else -spread)
                if not self._open_fill(q["side"], q["volume"], entry, q["sl"], q["tp"],
                                      utc_iso(row.time), q.get("strategy", "?"),
                                      q.get("sl_why") or sl_why(q.get("strategy", "?"))):
                    self.st.pending = None
                    return
            elif utc_iso(row.time) >= utc_iso(q["expires_at"]):
                self.st.pending = None
                self.st.expired += 1
                self.st.owner = ""
                jlog(type="expire", symbol=self.symbol, tf=self.tf,
                     price=q["price"])

    def row_cells(self, rebuild: bool = False) -> list[str]:
        """Строка таблицы, посчитанная один раз за круг.

        Её просят три раза: живая таблица, HTML-панель и данные графика.
        Раньше каждый спрашивал заново — 207 пересчётов вместо 69, и три
        прохода по всем наблюдателям вместо одного. Пересобирает только
        render(), остальные берут готовое.
        """
        if rebuild or self.row_cache is None:
            self.row_cache = self.render_row()
        return self.row_cache

    def render_row(self) -> list[str]:
        label = f"{SHORT.get(self.symbol, self.symbol)} {self.tf}"
        badge = " ".join("H" if n.startswith("HSS") else "L" for n in self.route_names)
        if self.last_err or self.row is None:
            return [label, badge, self.last_err or "…", "", "", "", "",
                    "0", "0.00", f"{self.st.expired}/{self.st.rejected}", "нет данных"]
        tick = self.feed.ticker(self.symbol)
        r = self.row
        above = True
        try:
            px = float(r.ha_close) if "ha_close" in getattr(r, "index", []) else float(r.close)
            above = bool(px > float(r.ema))
            is_doji = bool(r.is_doji)
            body = float(r.body_ratio)
        except Exception:  # noqa: BLE001
            is_doji, body = False, 0.0
        n = len(self.st.trades)
        pnl = sum(self.st.trades)
        wins = sum(1 for x in self.st.trades if x > 0)
        if self.st.position:
            p = self.st.position
            mark = tick[0] if tick else p["entry"]
            if p["side"] != Side.BUY.value and tick:
                mark = tick[1]
            fp = self._pnl(p["side"], p["entry"], mark, p["volume"])
            status = f"{self.st.owner} в позиции {fp:+.2f}"
        elif self.st.pending:
            status = f"{self.st.owner} ордер @ {self.st.pending['price']:.4g}"
        else:
            status = "ждём сетап"
        px = f"{tick[0]:.4g}" if tick else "-"
        spr = f"{(tick[1]-tick[0]):.4g}" if tick else "-"
        return [
            label, badge, px, spr,
            "BUY" if above else "SELL",
            "да" if is_doji else f"{body:.2f}",
            f"{n} ({wins}W)" if n else "0",
            f"{pnl:+.2f}",
            f"{self.st.expired}/{self.st.rejected}",
            status,
        ]


def render(watchers, n, args, equity, start) -> Group:
    t = Table(box=None, pad_edge=False)
    for c in ("пара", "страт.", "цена", "спред", "тренд", "doji",
              "сделок", "итог", "проп./отк.", "статус"):
        t.add_column(c)
    for w in watchers:
        t.add_row(*w.row_cells(rebuild=True))
    total = sum(sum(w.st.trades) for w in watchers)
    n_tr = sum(len(w.st.trades) for w in watchers)
    head = Text.assemble(
        ("КРИПТО-ФОРВАРД", "bold cyan"), ("  ·  ", "dim"),
        ("БУМАГА, живые свопы", "green"), ("  ·  ", "dim"),
        (f"обновление {n}", "dim"),
    )
    foot = Text.assemble(
        (f"equity {equity:,.2f} USDT   старт {start:,.2f}   "
         f"сделок {n_tr}   итог {total:+.2f}   риск {args.risk}%\n", ""),
        ("журнал logs/crypto_forward.jsonl   панель dashboard/crypto_live.html",
         "dim"),
    )
    return Group(head, Text(""), t, Text(""), foot)


def _bar_ts(t) -> int:
    ts = pd.Timestamp(t)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.timestamp())


def _bar_ts_many(col) -> list[int]:
    """Целая колонка времён в epoch-секунды разом, без Timestamp на строку."""
    s = pd.to_datetime(col, utc=True, errors="coerce")
    # Единицу задаём явно: разрешение datetime64 у pandas менялось (ns/us),
    # и деление на 10^9 вслепую молча даёт мусорные времена на графике.
    return s.to_numpy(dtype="datetime64[s]").astype("int64").tolist()


def _journal_all(tail_lines: int = 1000) -> list[dict]:
    """Хвост журнала, разобранный один раз за цикл.

    Раньше _journal_marks() читал и парсил весь файл заново для КАЖДОГО из 69
    наблюдателей — 69 чтений и десятки тысяч json.loads за круг, до 18 секунд
    на запись панели. Читаем один раз за цикл — но и это разбирало ВЕСЬ
    журнал целиком, а его никто не чистит: за недели "всегда включён" файл
    растёт без ограничений, и json.loads на каждой строке истории повторялся
    каждые 5-8 секунд бесконечно. Ни один потребитель (_journal_marks — до
    400 строк, _recent_events — до 200) не смотрит дальше последних ~400
    записей, так что парсим только хвост — стоимость больше не растёт с
    возрастом бота.
    """
    if not JOURNAL.exists():
        return []
    lines = JOURNAL.read_text(encoding="utf-8").splitlines()[-tail_lines:]
    rows: list[dict] = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _mark_bar_ts(r: dict, kind: str) -> int:
    """Время для оси X — начало свечи, на которой событие зафиксировано."""
    bar = r.get("bar")
    if not bar:
        bar = r.get("opened_at") if kind == "fill" else (r.get("closed_at") or r.get("ts"))
    return _bar_ts(bar) if bar else 0


def _close_mark_label(reason: str, entry: float, exit_: float) -> str:
    r = str(reason or "").lower()
    if r.startswith("tp"):
        return f"TP{r[2:] or '1'}"
    if r == "sl" and _near_px(float(exit_ or 0), float(entry or 0)):
        return "BE"
    if r == "sl":
        return "SL"
    return (r or "out").upper()


def _book_tf(key: str) -> str:
    return key.rsplit("|", 1)[-1] if "|" in key else ""


def _journal_row_matches_key(r: dict, key: str) -> bool:
    if r.get("symbol") and r["symbol"] not in key:
        return False
    book_tf = _book_tf(key)
    if r.get("tf") and book_tf and r["tf"] != book_tf:
        return False
    return True


def _journal_marks(key: str, rows: list[dict]) -> list[dict]:
    marks = []
    for r in rows[-400:]:
        if not _journal_row_matches_key(r, key):
            continue
        kind = r.get("type")
        if kind not in ("fill", "close"):
            continue
        try:
            t = _mark_bar_ts(r, kind)
        except Exception:
            continue
        if not t:
            continue
        if kind == "fill":
            entry = float(r.get("entry") or 0)
            if entry <= 0:
                continue
            marks.append({
                "time": t,
                "price": entry,
                "kind": "fill",
                "side": r.get("side"),
                "label": "вход",
                "at": r.get("opened_at") or r.get("bar") or r.get("ts"),
            })
        else:
            exit_ = float(r.get("exit") or 0)
            entry = float(r.get("entry") or 0)
            if exit_ <= 0:
                continue
            reason = str(r.get("reason") or "")
            marks.append({
                "time": t,
                "price": exit_,
                "kind": "close",
                "side": r.get("side"),
                "reason": reason,
                "label": _close_mark_label(reason, entry, exit_),
                "entry": entry,
                "pnl": float(r.get("pnl") or 0),
                "fee": float(r.get("fee") or 0),
                "at": r.get("closed_at") or r.get("bar") or r.get("ts"),
            })
    return marks[-40:]


def _last_setup(key: str) -> dict | None:
    if not JOURNAL.exists():
        return None
    for line in reversed(JOURNAL.read_text(encoding="utf-8").splitlines()[-200:]):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("type") != "signal":
            continue
        if not _journal_row_matches_key(r, key):
            continue
        return {"kind": "last_signal", "side": r.get("side"),
                "price": r.get("entry"), "entry": r.get("entry"),
                "sl": r.get("sl"), "tp": r.get("tp"),
                "strategy": r.get("strategy"), "volume": r.get("volume")}
    return None


def _recent_events(n: int = 50, journal: list[dict] | None = None) -> list[dict]:
    rows = []
    started = ""
    for r in (_journal_all() if journal is None else journal):
        if r.get("type") == "start":
            started = r.get("ts") or ""
            rows = []
            continue
        if started and r.get("type") in {"fill", "close", "signal"}:
            # reason tp1/tp2/tp3/sl — для ленты справа «тейк 1 забран» и т.п.
            rows.append(r)
    return rows[-n:]


def _pos_margin(watchers, leverage: float) -> tuple[float, float]:
    """Оценка маржи и нотионала по открытым позициям (volume×price / lev)."""
    lev = max(1.0, float(leverage or 1))
    notional = 0.0
    for w in watchers:
        p = w.st.position
        if not p:
            continue
        cs = float(getattr(w.spec, "contract_size", 1.0) or 1.0)
        notional += abs(float(p["volume"]) * cs * float(p["entry"]))
    return notional / lev, notional


def _pos_size_fields(position: dict, spec, leverage: float) -> dict:
    """Нотионал и маржа позиции для панели (без пересчёта на фронте)."""
    lev = max(1.0, float(leverage or 1))
    cs = float(getattr(spec, "contract_size", 1.0) or 1.0)
    vol = float(position.get("volume") or 0)
    px = float(position.get("entry") or 0)
    notional = abs(vol * cs * px) if vol > 0 and px > 0 else 0.0
    margin = notional / lev if notional > 0 else 0.0
    return {"notional": round(notional, 2), "margin": round(margin, 2)}


def _max_trade_estimate(watchers, equity: float, risk_pct: float,
                        leverage: float) -> dict:
    """Сколько $ займёт следующий вход: риск + оценка объёма по pending/SL."""
    eq = float(equity or 0)
    rp = float(risk_pct or 0)
    lev = max(1.0, float(leverage or 1))
    risk_usd = eq * rp / 100 if eq > 0 and rp > 0 else 0.0
    best: dict | None = None
    for w in watchers:
        pending = w.st.pending
        if not pending:
            continue
        q = ensure_ladder(dict(pending))
        entry = float(q.get("price") or q.get("entry") or 0)
        sl = float(q.get("sl") or (q.get("sls") or [0])[0] or 0)
        cs = float(getattr(w.spec, "contract_size", 1.0) or 1.0)
        vol = float(q.get("volume") or 0)
        if vol <= 0 and entry and sl:
            sl_dist = abs(entry - sl)
            if sl_dist > 0:
                raw = risk_usd / (sl_dist * cs)
                vol = max(0.01, round(round(raw / 0.01) * 0.01, 2))
        if vol <= 0 or entry <= 0:
            continue
        notional = abs(vol * cs * entry)
        margin = notional / lev
        cand = {
            "risk_usd": round(risk_usd, 2),
            "notional": round(notional, 2),
            "margin": round(margin, 2),
            "volume": vol,
            "pair": SHORT.get(w.symbol, w.symbol.split("/")[0]) + "USDT",
            "tf": w.tf,
        }
        if best is None or cand["notional"] > best["notional"]:
            best = cand
    if best:
        return best
    return {"risk_usd": round(risk_usd, 2), "notional": 0.0, "margin": 0.0}


def write_chart_data(watchers, n, args, equity, start, *,
                     available: float | None = None,
                     used_margin: float | None = None,
                     wallet: float | None = None) -> None:
    journal = _journal_all()  # один раз на цикл, а не на каждого наблюдателя
    books = []
    positions, orders = [], []
    est_margin, notional = _pos_margin(watchers, getattr(args, "leverage", 5))
    if used_margin is None or used_margin <= 0:
        used_margin = est_margin
    if wallet is None:
        wallet = start
    if available is None:
        available = max(0.0, float(equity) - float(used_margin or 0))
    eq = float(equity or 0)
    margin_pct = (float(used_margin) / eq * 100) if eq > 1e-9 else 0.0
    # --- остальное тело без изменений до payload ---
    for w in watchers:
        held = bool(w.st.position or w.st.pending)
        # Позицию/ордер всегда отдаём в панель, даже если бары временно
        # короче 30 — иначе в blot «мигает» сделка (пропала → появилась).
        if w.df is None:
            if not held:
                continue
        elif len(w.df) < 30 and not held:
            continue
        if w.df is None or len(w.df) < 2:
            # Только состояние позиции без свечей — blot живёт, график ждёт.
            coin = SHORT.get(w.symbol, w.symbol.split("/")[0])
            venue = w.feed.place(w.symbol) or "bybit"
            setup = None
            if w.st.pending:
                setup = {**ensure_ladder(dict(w.st.pending)), "kind": "pending"}
            elif w.st.position:
                p = ensure_ladder(dict(w.st.position))
                setup = {**p, "price": p["entry"], "kind": "position"}
            books.append({
                "key": w.key, "label": f"{coin} {w.tf}", "pair": f"{coin}USDT",
                "venue": venue, "tf": w.tf, "last": None,
                "strategies": w.route_names, "status": "в позиции" if w.st.position else "ордер",
                "err": w.last_err, "pending": w.st.pending, "position": w.st.position,
                "setup": setup, "pnl": sum(w.st.trades), "pnl_open": None,
                "trades": len(w.st.trades), "candles": [], "ema": [],
                "lines": [], "markers": [], "scan": {},
            })
            if w.st.position:
                p = ensure_ladder(dict(w.st.position))
                sz = _pos_size_fields(p, w.spec, getattr(args, "leverage", 5))
                positions.append({
                    "pair": f"{coin}USDT", "label": f"{coin} {w.tf}", "tf": w.tf,
                    "venue": venue, "side": p["side"],
                    "entry": p["entry"], "sl": p["sl"], "tp": p["tp"],
                    "sls": p.get("sls"), "tps": p.get("tps"),
                    "taken": list(p.get("taken") or [False, False, False]),
                    "realized_by_tp": list(p.get("realized_by_tp") or [None, None, None]),
                    "volume": p["volume"], "strategy": p.get("strategy"),
                    "contract_size": getattr(w.spec, "contract_size", 1.0),
                    "notional": sz["notional"], "margin": sz["margin"],
                    "pnl": None, "opened_at": p.get("opened_at"),
                })
            if w.st.pending:
                q = ensure_ladder(dict(w.st.pending))
                orders.append({
                    "pair": f"{coin}USDT", "label": f"{coin} {w.tf}", "tf": w.tf,
                    "venue": venue, "side": q["side"], "entry": q.get("price") or q.get("entry"),
                    "sl": q.get("sl"), "tp": q.get("tp"),
                    "sls": q.get("sls"), "tps": q.get("tps"),
                    "volume": q.get("volume"), "strategy": q.get("strategy"),
                })
            continue
        tail = w.df.tail(200)
        # iterrows() лепит объект Series на каждую строку: 200 баров × 69
        # наблюдателей = 13 800 таких объектов за круг, около 10 секунд.
        # Тянем колонки разом и собираем словари по спискам.
        times = _bar_ts_many(tail.time)
        o = tail.open.to_numpy(dtype=float)
        h = tail.high.to_numpy(dtype=float)
        lo = tail.low.to_numpy(dtype=float)
        c = tail.close.to_numpy(dtype=float)
        v = tail.volume.to_numpy(dtype=float) if "volume" in tail.columns else None
        e = tail.ema.to_numpy(dtype=float) if "ema" in tail.columns else None
        candles, ema = [], []
        for i, t in enumerate(times):
            bar = {"time": t, "open": o[i], "high": h[i],
                   "low": lo[i], "close": c[i]}
            if v is not None and v[i] == v[i]:  # NaN не равен сам себе
                bar["volume"] = v[i]
            candles.append(bar)
            if e is not None and e[i] == e[i]:
                ema.append({"time": t, "value": e[i]})
        lines = []
        setup = None
        if w.st.pending:
            q = w.st.pending
            setup = {**q, "kind": "pending"}
        elif w.st.position:
            p = w.st.position
            setup = {**p, "price": p["entry"], "kind": "position"}
        else:
            setup = None
        if setup:
            ensure_ladder(setup)
            entry = float(setup.get("entry") or setup.get("price") or 0)
            if entry:
                lines.append({"price": entry, "color": "#98989f",
                              "title": "вход" if setup.get("kind") == "position" else "ордер",
                              "style": 0 if setup.get("kind") == "position" else 2})
            lines.extend(sl_lines_for_setup(setup))
            taken = setup.get("taken") or [False, False, False]
            realized = setup.get("realized_by_tp") or [None, None, None]
            for i, px in enumerate(setup.get("tps") or []):
                if px:
                    lines.append({"price": float(px), "color": "#32d74b",
                                  "title": tp_label(i), "style": 2,
                                  "taken": bool(taken[i]) if i < len(taken) else False,
                                  "tp_pnl": realized[i] if i < len(realized) else None})
            lines = dedupe_chart_lines(lines)
        scan = scan_overlay(w) if getattr(args, "scanner_on", True) else {}
        marks = _journal_marks(w.key, journal)
        coin = SHORT.get(w.symbol, w.symbol.split("/")[0])
        # Реальная биржа, где бот СЕЙЧАС берёт данные по этой паре (Feed.place
        # кэширует выбор при первом опросе). Bybit — основная (bot_config.
        # exchange), но не всё на ней есть: например TON торгуется только на
        # Binance как запасной бирже. Считать всё Bybit наугад означало бы
        # врать панели про то, откуда на самом деле идёт цена.
        venue = w.feed.place(w.symbol) or "binance"
        books.append({
            "key": w.key,
            "label": f"{coin} {w.tf}",
            "pair": f"{coin}USDT",
            "venue": venue,
            "tf": w.tf,
            "last": float(tail.close.iloc[-1]),
            "strategies": w.route_names,
            "status": w.row_cells()[-1],
            "err": w.last_err,
            "pending": w.st.pending,
            "position": w.st.position,
            "setup": setup,
            "pnl": sum(w.st.trades),
            "pnl_open": (w._pnl(w.st.position["side"], w.st.position["entry"],
                                float(w.last_mark or tail.close.iloc[-1]),
                                w.st.position["volume"])
                         if w.st.position else None),
            "trades": len(w.st.trades),
            "candles": candles,
            "ema": ema,
            "lines": lines,
            "markers": marks,
            "scan": scan,
        })
        if w.st.position:
            p = ensure_ladder(dict(w.st.position))
            sz = _pos_size_fields(p, w.spec, getattr(args, "leverage", 5))
            positions.append({
                "pair": f"{coin}USDT", "label": f"{coin} {w.tf}", "tf": w.tf,
                "venue": venue, "side": p["side"],
                "entry": p["entry"], "sl": p["sl"], "tp": p["tp"],
                "sls": p.get("sls"), "tps": p.get("tps"),
                "taken": list(p.get("taken") or [False, False, False]),
                "volume": p["volume"], "strategy": p.get("strategy"),
                "contract_size": getattr(w.spec, "contract_size", 1.0),
                "notional": sz["notional"], "margin": sz["margin"],
                "pnl": books[-1].get("pnl_open"), "opened_at": p.get("opened_at"),
            })
        if w.st.pending:
            q = ensure_ladder(dict(w.st.pending))
            orders.append({
                "pair": f"{coin}USDT", "label": f"{coin} {w.tf}", "tf": w.tf,
                "venue": venue, "side": q["side"],
                "entry": q.get("price") or q.get("entry"),
                "sl": q.get("sl"), "tp": q.get("tp"),
                "sls": q.get("sls"), "tps": q.get("tps"),
                "volume": q.get("volume"), "strategy": q.get("strategy"),
                "expires": q.get("expires_at"),
            })
    events = _recent_events(50, journal)
    for p in positions:
        coin = (p.get("label") or "").split(" ")[0]
        if any(e.get("type") == "fill" and e.get("coin") == coin and e.get("tf") == p.get("tf")
               for e in events):
            continue
        events.append({
            "type": "fill", "coin": coin, "tf": p.get("tf"), "side": p.get("side"),
            "entry": p.get("entry"), "sl": p.get("sl"), "tp": p.get("tp"),
            "strategy": p.get("strategy"), "bar": p.get("opened_at"),
            "opened_at": p.get("opened_at"), "source": "open",
        })
    max_trade = _max_trade_estimate(
        watchers, eq, float(getattr(args, "risk", 1) or 1),
        int(getattr(args, "leverage", 5) or 5))
    mode = getattr(args, "mode", "paper")
    # В бою baseline P&L = кошелёк Bybit, не бумажный депозит из настроек.
    start_val = float(wallet) if mode == "live" else float(start)
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "n": n, "equity": equity, "start": start_val, "risk": args.risk,
        "mode": mode,
        "paused": bool(bot_control().get("paused")),
        "wallet": float(wallet),
        "available": float(available),
        "used_margin": float(used_margin),
        "margin_pct": round(margin_pct, 2),
        "notional": float(notional),
        "leverage": int(getattr(args, "leverage", 5) or 5),
        "max_trade": max_trade,
        "books": books,
        "events": events,
        "blot": {
            "positions": positions,
            "orders": orders,
            "deals": [e for e in events
                      if e.get("type") in {"signal", "fill", "close"}][-20:],
        },
    }
    write_atomic(CHART_JSON, json.dumps(payload, ensure_ascii=False))


def start_chart_server() -> None:
    root = ROOT / "dashboard"

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, fmt, *args):
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", CHART_PORT), H)
    except OSError:
        con.print(f"[dim]график уже слушает :{CHART_PORT}[/]")
        return
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    con.print("[green]график: http://127.0.0.1:8787/[/]")


def write_panel(watchers, n, args, equity, start) -> None:
    now = datetime.now()
    rows = []
    for w in watchers:
        cells = [escape(str(c)) for c in w.row_cells()]
        status = cells[-1]
        klass = "wait"
        if "позиции" in status:
            klass = "pos"
        elif "ордер" in status:
            klass = "ord"
        rows.append("<tr class='%s'>%s</tr>" % (
            klass, "".join(f"<td>{c}</td>" for c in cells)))
    n_tr = sum(len(w.st.trades) for w in watchers)
    total = sum(sum(w.st.trades) for w in watchers)
    html = f"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8"><meta http-equiv="refresh" content="3">
<title>NEFT крипто · живой форвард</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
background:#000;color:#f5f5f7;margin:0;padding:32px;line-height:1.5;
-webkit-font-smoothing:antialiased;font-size:14px}}
h1{{font:600 22px/1.3 system-ui,sans-serif;letter-spacing:-.02em;margin:0 0 10px}}
.sub,.foot{{color:#98989f;font-size:14px;margin:0 0 16px}}
.badge{{background:#32d74b;color:#003d14;border-radius:999px;padding:4px 12px;font-size:13px;font-weight:600;margin-right:8px}}
table{{border-collapse:collapse;width:100%;background:rgba(28,28,30,.9);border-radius:16px;overflow:hidden;margin-top:16px;
box-shadow:0 8px 28px -12px rgba(0,0,0,.55)}}
th,td{{padding:14px 16px;font:14px/1.4 ui-monospace,"SF Mono",Consolas,monospace;border-bottom:1px solid rgba(255,255,255,.08);text-align:left}}
th{{color:#98989f;font-weight:600;font-family:system-ui,sans-serif}}
tr.pos td{{background:rgba(48,209,88,.10)}}
tr.ord td{{background:rgba(255,159,10,.10)}}
tr.off td{{color:#8e8e93}}
.kpi{{display:flex;gap:28px;margin:18px 0;flex-wrap:wrap}}
.kpi b{{display:block;font-size:22px;letter-spacing:-.02em}}
.kpi span{{color:#98989f;font-size:13px;font-weight:500}}
.warn{{background:rgba(48,209,88,.12);border:1px solid rgba(48,209,88,.35);border-radius:14px;padding:14px 18px;margin:14px 0;color:#32d74b;font-size:14px;line-height:1.45}}
</style></head><body>
<p class="sub"><span class="badge">БУМАГА · живые свопы</span>
обновление {n} · {now.strftime('%H:%M:%S')} · 24/7, в т.ч. выходные</p>
<h1>Крипто-машина · маршруты из routing.py</h1>
<p class="warn">Ордера на биржу не отправляются (DEMO_ONLY, без ключей).
Цена и бары — публичный Binance/Bybit swap. Исполнение как в бэктесте.</p>
<div class="kpi">
<div><span>equity</span><b>{equity:,.2f}</b></div>
<div><span>старт</span><b>{start:,.2f}</b></div>
<div><span>сделок</span><b>{n_tr}</b></div>
<div><span>итог</span><b>{total:+.2f}</b></div>
<div><span>риск</span><b>{args.risk}%</b></div>
</div>
<table><thead><tr>
<th>пара</th><th>страт.</th><th>цена</th><th>спред</th><th>тренд</th>
<th>doji</th><th>сделок</th><th>итог</th><th>проп./отк.</th><th>статус</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
<p class="foot">H = HSS M1 24/7 · L = London S/R · сессия 16–19 где так в маршруте</p>
</body></html>
"""
    write_atomic(LIVE_HTML, html)


def refresh_panel(args, watchers, risk) -> None:
    bot = load_bot_cfg()
    mode = getattr(args, "mode", "paper")
    acc = account_for_mode(bot, mode)
    sc = bot.get("scanner") or {}
    args.scan_scope = str(sc.get("scope") or "all")
    args.scanner_enabled = bool(sc.get("enabled", True))
    args.scanner_on = (args.scanner_enabled
                       and scanner_applies(args.scan_scope, "crypto"))
    args.scan_k = max(1, int(sc.get("top_k") or 2))
    args.scan_min = float(sc.get("min_score") or 1.2)
    args.risk = float(acc.get("risk_pct") or args.risk)
    args.leverage = int(acc.get("leverage_crypto") or args.leverage or 5)
    args.flow_cfg = bot.get("strategies", {}).get("session_flow") or args.flow_cfg
    args.rr_by_strategy = rr_map_from_cfg(bot)
    st = bot.get("strategies") or {}
    if risk is not None:
        risk.limits.risk_per_trade_pct = args.risk
        risk.limits.max_risk_per_trade_pct = max(args.risk, 1.0)
        risk.limits.max_daily_loss_pct = float(acc.get("max_daily_loss_pct") or 4.0)
        risk.limits.max_drawdown_pct = float(acc.get("max_drawdown_pct") or 12.0)
        risk.limits.max_open_positions = int(acc.get("max_open_positions") or 1)
    for w in watchers or []:
        slots = getattr(w.strat, "slots", None)
        if not slots:
            continue
        for slot in slots:
            slot.enabled = strategy_on(slot.name, st)


def write_scan(hits, hot: set[str], args) -> None:
    SCAN_STATE.parent.mkdir(exist_ok=True)
    SCAN_STATE.write_text(json.dumps({
        "enabled": bool(args.scanner_on),
        "top_k": args.scan_k,
        "min_score": args.scan_min,
        "scope": getattr(args, "scan_scope", "all"),
        "tfs": ["1m", "5m", "15m"],
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


def grouped_routes(wanted: set[str] | None,
                   table: dict | None = None,
                   strategies: dict | None = None,
                   hss_24h: bool = False) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    src = table or CRYPTO_ROUTES
    hss_cfg = (strategies or {}).get("hss") or {}
    around = bool(hss_24h or hss_cfg.get("all_day"))
    for sym, routes in src.items():
        if banned(sym):
            continue
        if wanted and sym not in wanted:
            continue
        for r in routes:
            if not strategy_on(r.get("strategy", ""), strategies):
                continue
            sess = r.get("session")
            if isinstance(sess, list):
                r = {**r, "session": (int(sess[0]), int(sess[1]))}
            name = r.get("strategy", "")
            if name in ("Playbook", "All"):
                continue
            allowed = STRATEGY_TFS.get(name)
            if allowed and r.get("tf") not in allowed:
                continue
            if name == "HSS" and around:
                r = {**r, "session": None}
            out[(sym, r["tf"])].append(r)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default=",".join(CRYPTO_UNIVERSE))
    p.add_argument("--risk", type=float, default=0.5)
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--leverage", type=int, default=10)
    p.add_argument("--interval", type=float, default=8.0)
    p.add_argument("--exchange", default="binance")
    p.add_argument("--mode", default="paper", choices=("paper", "demo", "live"))
    p.add_argument("--reset", action="store_true")
    p.add_argument("--no-news", action="store_true")
    p.add_argument("--rr", type=float, default=None)
    p.add_argument("--pullback", type=int, default=None)
    a = p.parse_args()
    write_pid(a.mode)
    bot = load_bot_cfg()
    if a.rr is None:
        a.rr = float(bot.get("strategies", {}).get("hss", {}).get("rr", bot.get("rr", 1.0)))
    if a.pullback is None:
        a.pullback = int(bot.get("strategies", {}).get("hss", {}).get("pullback", 2))
    lon = bot.get("strategies", {}).get("london_sr", {}).get("london") or bot.get("london") or [11, 16]
    a.london_tuple = (int(lon[0]), int(lon[1]))
    a.flow_cfg = bot.get("strategies", {}).get("session_flow") or {}
    a.rr_by_strategy = rr_map_from_cfg(bot)
    scan_cfg = bot.get("scanner") or {"enabled": True, "top_k": 2, "min_score": 1.2}
    a.scan_scope = str(scan_cfg.get("scope") or "all")
    a.scanner_enabled = bool(scan_cfg.get("enabled", True))
    a.scanner_on = (a.scanner_enabled
                    and scanner_applies(a.scan_scope, "crypto"))
    a.scan_k = int(scan_cfg.get("top_k", 2))
    a.scan_min = float(scan_cfg.get("min_score", 1.2))
    default_join = ",".join(CRYPTO_UNIVERSE)
    if a.symbols in (default_join, ",".join(CRYPTO_ROUTES)):
        a.symbols = ",".join(bot.get("crypto_symbols") or CRYPTO_UNIVERSE)
    wanted = {x.strip() for x in a.symbols.split(",") if x.strip() and not banned(x.strip())}
    kit = kit_from_config(bot.get("strategies") or {}, hss_24h=bool(bot.get("hss_24h")))
    routes_table = {s: [dict(r) for r in kit] for s in wanted}
    groups = grouped_routes(wanted, routes_table, bot.get("strategies") or {},
                            hss_24h=bool(bot.get("hss_24h")))

    exchanges: dict[str, ccxt.Exchange] = {}
    for name in (a.exchange, "binance", "bybit"):
        if name in exchanges:
            continue
        try:
            exchanges[name] = open_exchange(name)
            con.print(f"[dim]{name}: {len(exchanges[name].markets)} рынков[/]")
        except Exception as e:  # noqa: BLE001
            con.print(f"[yellow]{name}: {e}[/]")
    if not exchanges:
        con.print("[red]Ни одна биржа не ответила.[/]")
        return
    feed = Feed(exchanges, a.exchange if a.exchange in exchanges else next(iter(exchanges)))

    if a.reset and STATE.exists():
        STATE.unlink()
    saved = load_state()
    acc = account_for_mode(bot, a.mode)
    if a.mode == "live":
        balance = saved.get("balance", a.balance) if not a.reset else a.balance
    else:
        dep = float(acc.get("deposit") or a.balance)
        balance = dep if a.reset else saved.get("balance", dep)

    if "--risk" not in sys.argv:
        a.risk = float(acc.get("risk_pct") or a.risk)
    a.leverage = int(acc.get("leverage_crypto") or a.leverage or 5)
    if "--interval" not in sys.argv:
        a.interval = max(5.0, float(acc.get("interval_sec") or a.interval or 8))
    daily = float(acc.get("max_daily_loss_pct") or 4.0)
    risk = RiskManager(start_balance=balance, limits=RiskLimits(
        risk_per_trade_pct=a.risk, max_risk_per_trade_pct=max(a.risk, 1.0),
        max_volume=1e6,
        max_daily_loss_pct=daily,
        max_drawdown_pct=float(acc.get("max_drawdown_pct") or 12.0),
        max_open_positions=int(acc.get("max_open_positions") or 1),
        min_free_margin_pct=20.0))
    if not a.no_news:
        try:
            gate = NewsGate(buffer_minutes=5, events=load_calendar())
            risk.set_news_gate(gate, utc_offset_hours=0.0)
            con.print(f"[dim]новости: {len(gate.events)} событий, крипта=USD, UTC[/]")
        except Exception as e:  # noqa: BLE001
            con.print(f"[yellow]календарь недоступен ({e})[/]")

    watchers: list[Watcher] = []
    for (sym, tf), routes in groups.items():
        if feed.place(sym) is None:
            con.print(f"[red]{SHORT.get(sym, sym)}: нет на binance/bybit — пропуск[/]")
            continue
        raw = saved.get("symbols", {}).get(f"{sym}|{tf}", {}) or {}
        allowed = {f.name for f in fields(SymbolState)}
        st = SymbolState(**{k: v for k, v in raw.items() if k in allowed})
        watchers.append(Watcher(sym, tf, routes, a, risk, st, feed))
        time.sleep(0.15)
    if not watchers:
        con.print("[red]Не осталось пар.[/]")
        return

    restored = restore_orphaned_fills(watchers)
    if restored:
        con.print(f"[yellow]восстановил {restored} поз. из журнала (вход без закрытия)[/]")
        save_state({w.key: w.st for w in watchers}, balance)

    a.broker = None
    if a.mode == "live":
        from neft.adapters.crypto_broker import CryptoBroker
        from neft.core.config import settings as stg
        if stg.demo_only:
            con.print("[red]DEMO_ONLY=true — бой не стартую.[/]")
            return
        if stg.crypto_testnet:
            con.print("[red]CRYPTO_TESTNET=true — бой не стартую.[/]")
            return
        if not (stg.binance_api_key or stg.bybit_api_key):
            con.print("[red]нет ключей биржи в .env — бой не стартую.[/]")
            return
        try:
            a.broker = CryptoBroker(
                a.exchange, testnet=False, leverage=int(a.leverage or 5))
            acc = a.broker.connect()
            # В бою счёт = реальный Bybit, не бумажный депозит из настроек.
            balance = float(acc.wallet or acc.equity)
            equity0 = float(acc.equity)
            risk.reset_book(equity0)
            a._live_wallet = {
                "wallet": float(acc.wallet or acc.equity),
                "available": float(acc.available or acc.balance),
                "used_margin": float(acc.used_margin or 0),
            }
            con.print(
                f"[yellow]БОЙ {a.exchange} · {acc.server} · "
                f"{acc.equity:.2f} · lev {a.broker.leverage}x[/]")
        except Exception as e:  # noqa: BLE001
            con.print(f"[red]биржа не открылась: {e}[/]")
            return

    jlog(type="start", mode=a.mode, symbols=[w.key for w in watchers],
         risk=a.risk, balance=balance)
    start_chart_server()
    n = 0
    try:
        with Live(console=con, refresh_per_second=1, screen=False) as live:
            while True:
                n += 1
                new_bal = consume_reset(watchers, risk, a)
                if new_bal is not None and a.mode != "live":
                    balance = new_bal
                live_wallet = getattr(a, "_live_wallet", None) or {}
                if a.mode == "live" and a.broker is not None:
                    # В бою эквити всегда с Bybit (не бумажные 1000).
                    try:
                        acc = a.broker.account()
                        live_wallet = {
                            "wallet": float(acc.wallet or acc.equity),
                            "available": float(acc.available or acc.balance),
                            "used_margin": float(acc.used_margin or 0),
                            "equity": float(acc.equity),
                        }
                        a._live_wallet = live_wallet
                        equity = float(acc.equity)
                        balance = float(acc.wallet or acc.equity)
                    except Exception as e:  # noqa: BLE001
                        jlog(type="error", symbol="account", err=str(e))
                        equity = float(live_wallet.get("equity") or balance)
                        for w in watchers:
                            if w.st.position:
                                tick = w.feed.ticker(w.symbol)
                                if tick:
                                    p = w.st.position
                                    mark = tick[0] if p["side"] == Side.BUY.value else tick[1]
                                    w.last_mark = mark
                                    equity += w._pnl(p["side"], p["entry"], mark, p["volume"])
                else:
                    closed = sum(sum(w.st.trades) for w in watchers)
                    equity = balance + closed
                    for w in watchers:
                        if w.st.position:
                            tick = w.feed.ticker(w.symbol)
                            if tick:
                                p = w.st.position
                                mark = tick[0] if p["side"] == Side.BUY.value else tick[1]
                                w.last_mark = mark
                                equity += w._pnl(p["side"], p["entry"], mark, p["volume"])
                risk.update(equity)
                t_mark = time.perf_counter()
                refresh_panel(a, watchers, risk)
                open_n = sum(1 for w in watchers if w.st.position or w.st.pending)
                hits = []
                hot: set[str] = set()
                if a.scanner_on:
                    for w in watchers:
                        hits.extend(score_watcher(w))
                    ranked = rank_hits(hits, a.scan_k, a.scan_min)
                    hot = {(h.symbol, h.tf) for h in ranked}
                    write_scan(hits, hot, a)
                else:
                    write_scan([], set(), a)
                t_scan = time.perf_counter()
                allow = bool(bot_control().get("entries", True))
                used_m, _ = _pos_margin(watchers, getattr(a, "leverage", 5))
                for w in watchers:
                    held = bool(w.st.position or w.st.pending)
                    w.book_open = open_n
                    w.free_margin = max(0.0, float(equity) - float(used_m))
                    w.allow_entries = allow
                    w.hot = allow_new_entries(
                        a.scanner_enabled, a.scan_scope, "crypto",
                        held=held, in_hot=(w.symbol, w.tf) in hot)
                    try:
                        w.poll(equity)
                    except Exception as e:  # noqa: BLE001
                        w.last_err = str(e)[:48]
                        jlog(type="error", symbol=w.symbol, err=str(e))
                    now_held = bool(w.st.position or w.st.pending)
                    if now_held and not held:
                        open_n += 1
                        used_m, _ = _pos_margin(watchers, getattr(a, "leverage", 5))
                    elif held and not now_held:
                        open_n = max(0, open_n - 1)
                        used_m, _ = _pos_margin(watchers, getattr(a, "leverage", 5))
                t_poll = time.perf_counter()
                live.update(render(watchers, n, a, equity, balance))
                # В бою не затираем бумажный STATE балансом биржи.
                if a.mode != "live":
                    save_state({w.key: w.st for w in watchers}, balance)
                else:
                    save_state({w.key: w.st for w in watchers},
                               float(live_wallet.get("wallet") or balance))
                t_render = time.perf_counter()
                t_panel = t_render
                try:
                    write_panel(watchers, n, a, equity, balance)
                    t_panel = time.perf_counter()
                    write_chart_data(
                        watchers, n, a, equity, balance,
                        available=live_wallet.get("available") if a.mode == "live" else None,
                        used_margin=live_wallet.get("used_margin") if a.mode == "live" else None,
                        wallet=live_wallet.get("wallet") if a.mode == "live" else balance,
                    )
                except OSError as e:  # не молчим: иначе график замирает незаметно
                    con.print(f"[red]панель не записалась: {e}[/]")
                    jlog(type="error", symbol="panel", err=str(e))
                # Задержка цикла = задержка реакции на закрытый бар. В бою это
                # важнее любой другой телеметрии, поэтому пишем всегда.
                t_end = time.perf_counter()
                jlog(type="cycle", n=n,
                     total=round(t_end - t_mark, 2),
                     scan=round(t_scan - t_mark, 2),
                     poll=round(t_poll - t_scan, 2),
                     render=round(t_render - t_poll, 2),
                     panel=round(t_panel - t_render, 2),
                     chart=round(t_end - t_panel, 2),
                     write=round(t_end - t_render, 2))
                if risk.halted:
                    con.print(f"[red]KILL-SWITCH: {risk.halt_reason}[/]")
                    break
                time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    finally:
        save_state({w.key: w.st for w in watchers}, balance)
        jlog(type="stop", updates=n)
        clear_pid()
    con.print("\n[dim]Остановлено. Состояние в logs/crypto_forward_state.json[/]")


if __name__ == "__main__":
    main()
