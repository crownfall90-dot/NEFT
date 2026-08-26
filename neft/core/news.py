"""Фильтр по новостям и календарь для новостного бота.

Источник — публичный недельный фид ForexFactory (nfs.faireconomy.media),
годами используемый тысячами MT5/MQL5-индикаторов для этой же задачи.
Отдаёт event.impact уже размеченным (High/Medium/Low) — считать важность
самим не нужно, это делает сам источник.

Единственное, что действительно требует суждения, — какая валюта на какой
НАШ инструмент влияет. Эта таблица размечена один раз вручную (ниже, с
обоснованием по каждой группе), а не запросом к LLM на каждое событие:
сопоставление валюта->инструмент — статичный факт о структуре рынка, он не
меняется от сделки к сделке, и держать его в коде дешевле, быстрее и
воспроизводимее, чем звать модель в реальном времени на каждый бар.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from neft.core.config import ROOT

log = logging.getLogger(__name__)
CACHE = ROOT / "data" / "calendar_cache.json"
# Только thisweek реально существует на этом хосте (nextweek/thismonth
# отдают 404 — проверено). Это ограничивает горизонт видимости концом
# текущей недели по определению ForexFactory (Sun-Sat), но при ежедневном
# опросе бот всегда видит актуальное окно вперёд — для риск-гейта "не
# входить рядом с релизом" этого достаточно, слепая зона только в ночь
# с воскресенья на новую неделю.
FEEDS = {"this": "https://nfs.faireconomy.media/ff_calendar_thisweek.json"}

# ── валюта -> какие из наших инструментов она задевает ──────────────────
#
# Форекс-пары: обе валюты пары — событие по любой из них двигает котировку
# напрямую, это не требует обоснования.
#
# Индексы: их официальная валюта котировки (USD в CFD) — не то же самое,
# что валюта, определяющая макроэкономику базового актива. NAS100/DJ30/ES35
# (ES35=IBEX — CFD в USD, но сам рынок европейский) размечены по РЕАЛЬНОЙ
# экономике: DAX/CAC/IBEX двигают ставки и данные еврозоны, американские
# индексы — данные США. US-данные при этом задевают вообще все рисковые
# активы (глобальный risk-on/off), поэтому USD добавлен вторым для
# европейских индексов, но не наоборот — эффект EUR-данных на NAS100
# практически нулевой, включать его значило бы блокировать половину
# торговых часов без причины.
#
# Золото: почти монопричинно реагирует на USD (ставки ФРС, реальная
# доходность, CPI, NFP) — остальные валюты для него шум.
#
# Нефть (Brent/WTI): USD-актив; High-impact по USD двигает risk/commodity.
#
# Крипта: коррелирует с USD-ликвидностью (ставки ФРС, CPI, NFP двигают
# аппетит к риску), реакция на данные отдельных стран (GBP CPI, AUD
# занятость) статистически не отличима от нуля. Разметка по нюансам
# отдельных монет не нужна — фильтр по USD один на всю крипту.
INSTRUMENT_CURRENCIES: dict[str, list[str]] = {
    # индексы
    "NAS100": ["USD"], "DJ30": ["USD"], "US500": ["USD"],
    "GER40": ["EUR", "USD"], "FRA40": ["EUR", "USD"], "ES35": ["EUR", "USD"],
    "UK100": ["GBP", "USD"], "CHINA50": ["CNY", "USD"],
    # металл / нефть
    "XAUUSD+": ["USD"], "XAUUSD": ["USD"],
    "UKOUSD": ["USD"], "USOUSD": ["USD"],
    # форекс — обе ноги пары
    "EURUSD+": ["EUR", "USD"], "GBPUSD+": ["GBP", "USD"],
    "USDJPY+": ["USD", "JPY"], "AUDUSD+": ["AUD", "USD"],
    "USDCAD+": ["USD", "CAD"], "USDCHF+": ["USD", "CHF"],
    "NZDUSD+": ["NZD", "USD"], "EURJPY+": ["EUR", "JPY"],
    "GBPJPY+": ["GBP", "JPY"], "EURGBP+": ["EUR", "GBP"],
}

# Крипта: единая привязка к USD для всех пар вида "*/USDT:USDT".
CRYPTO_CURRENCIES = ["USD"]

_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def currencies_for(instrument: str) -> list[str]:
    if instrument in INSTRUMENT_CURRENCIES:
        return INSTRUMENT_CURRENCIES[instrument]
    # брокерские суффиксы (.f, .m, …) и канон через алиасы
    base = instrument.split(".")[0]
    if base.endswith("+") or base in INSTRUMENT_CURRENCIES:
        hit = currencies_for(base) if base != instrument else []
        if hit:
            return hit
    base2 = instrument[:-1] if instrument.endswith("+") else instrument
    for key, curs in INSTRUMENT_CURRENCIES.items():
        if key == base2 or key.rstrip("+") == base2 or key == base2 + "+":
            return curs
        if key.split(".")[0] == base or key.rstrip("+") == base:
            return curs
    if "/USDT" in instrument or instrument.endswith("USDT"):
        return CRYPTO_CURRENCIES
    try:
        from neft.core.mt5_symbols import canonical
        can = canonical(instrument)
        if can != instrument and can in INSTRUMENT_CURRENCIES:
            return INSTRUMENT_CURRENCIES[can]
    except Exception:
        pass
    return []


@dataclass(frozen=True)
class Event:
    time: pd.Timestamp   # naive, UTC
    currency: str
    impact: str
    title: str
    forecast: str = ""
    previous: str = ""
    actual: str = ""

    @property
    def key(self) -> str:
        return f"{self.time.isoformat()}|{self.currency}|{self.title}"


def _parse_number(raw: str | float | int | None) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s or s in ("-", "—", "n/a", "N/A", "null"):
        return None
    # "2.5%" / "250K" / "1,234.5" — берём первое число
    m = _NUM_RE.search(s.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def surprise(event: Event) -> float | None:
    """Знак/сила actual−forecast. None — цифр нет, торгуем только по цене."""
    act = _parse_number(event.actual)
    fc = _parse_number(event.forecast)
    if act is None or fc is None:
        return None
    return act - fc


def has_material_surprise(event: Event, min_abs: float = 0.0) -> bool | None:
    """True/False если есть цифры; None — actual/forecast ещё нет."""
    s = surprise(event)
    if s is None:
        return None
    return abs(s) >= min_abs


def _fetch_feed(url: str, retries: int = 3) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:  # источник ограничивает частые прямые запросы
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise last_err


def load_calendar(refresh: bool = False, max_age_hours: float = 12.0) -> list[Event]:
    """Календарь этой недели, с кэшем на диск.

    Для риск-гейта хватает кэша на часы. Новостной бот рядом с релизом
    передаёт max_age_hours≈0.02 (≈1 мин), чтобы подтянуть actual.
    """
    if CACHE.exists() and not refresh:
        age_h = (time.time() - CACHE.stat().st_mtime) / 3600
        if age_h < max_age_hours:
            raw = json.loads(CACHE.read_text(encoding="utf-8"))
            return _parse(raw)

    raw: list[dict] = []
    for label, url in FEEDS.items():
        try:
            raw += _fetch_feed(url)
        except Exception as e:
            log.warning("календарь (%s) недоступен: %s", label, e)
    if raw:
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    elif CACHE.exists():
        raw = json.loads(CACHE.read_text(encoding="utf-8"))  # старый кэш лучше, чем ничего
    return _parse(raw)


def _parse(raw: list[dict]) -> list[Event]:
    out = []
    for e in raw:
        try:
            t = pd.Timestamp(e["date"]).tz_convert("UTC").tz_localize(None)
        except Exception:
            continue
        out.append(Event(
            time=t,
            currency=e.get("country", "") or "",
            impact=e.get("impact", "Low") or "Low",
            title=e.get("title", "") or "",
            forecast=str(e.get("forecast") or ""),
            previous=str(e.get("previous") or ""),
            actual=str(e.get("actual") or ""),
        ))
    return sorted(out, key=lambda x: x.time)


def events_for_instrument(
    events: list[Event],
    instrument: str,
    *,
    impacts: tuple[str, ...] = ("High",),
) -> list[Event]:
    curs = set(currencies_for(instrument))
    if not curs:
        return []
    want = set(impacts)
    return [e for e in events if e.impact in want and e.currency in curs]


def upcoming_high(
    events: list[Event],
    instrument: str,
    when_utc: pd.Timestamp,
    *,
    ahead_minutes: float = 180.0,
    behind_minutes: float = 0.0,
) -> list[Event]:
    """High-impact события по валютам инструмента в окне вокруг now."""
    lo = when_utc - pd.Timedelta(minutes=behind_minutes)
    hi = when_utc + pd.Timedelta(minutes=ahead_minutes)
    return [
        e for e in events_for_instrument(events, instrument)
        if lo <= e.time <= hi
    ]


def near_high_event(
    events: list[Event],
    when_utc: pd.Timestamp,
    *,
    window_minutes: float = 30.0,
) -> bool:
    """Есть ли любой High-релиз в ±window — для частого обновления кэша."""
    w = pd.Timedelta(minutes=window_minutes)
    for e in events:
        if e.impact != "High":
            continue
        if abs(when_utc - e.time) <= w:
            return True
    return False


class NewsGate:
    """Проверка «не идёт ли сейчас окно вокруг важной новости по инструменту»."""

    def __init__(self, buffer_minutes: int = 5, impacts: tuple[str, ...] = ("High",),
                 events: list[Event] | None = None):
        self.buffer = pd.Timedelta(minutes=buffer_minutes)
        self.impacts = set(impacts)
        self.events = events if events is not None else load_calendar()

    def blocked(self, instrument: str, when_utc: pd.Timestamp) -> tuple[bool, str]:
        curs = set(currencies_for(instrument))
        if not curs:
            return False, ""
        for e in self.events:
            if e.impact not in self.impacts or e.currency not in curs:
                continue
            if abs((when_utc - e.time).total_seconds()) <= self.buffer.total_seconds():
                return True, f"{e.currency} {e.title} в {e.time:%H:%M UTC}"
        return False, ""

    def pressure(self, instrument: str, when_utc: pd.Timestamp,
                 soft_minutes: float = 90.0) -> tuple[float, str]:
        """Штраф сканеру до жёсткого окна. 0 — спокойно, 1 — внутри буфера.

        Между soft_minutes и buffer_minutes score режется пропорционально:
        ближе к релизу — сильнее. Ордера режет только blocked(); это лишь
        понижает приоритет пары, пока до новости ещё далеко.
        """
        hard, why = self.blocked(instrument, when_utc)
        if hard:
            return 1.0, why
        curs = set(currencies_for(instrument))
        if not curs or soft_minutes <= 0:
            return 0.0, ""
        soft = pd.Timedelta(minutes=soft_minutes)
        best = None
        for e in self.events:
            if e.impact not in self.impacts or e.currency not in curs:
                continue
            delta = abs(when_utc - e.time)
            if delta > soft:
                continue
            if best is None or delta < best[0]:
                best = (delta, e)
        if best is None:
            return 0.0, ""
        delta, e = best
        # 0 у края soft-окна → ~0.85 у границы buffer.
        buf_s = max(self.buffer.total_seconds(), 1.0)
        soft_s = max(soft.total_seconds(), buf_s)
        t = delta.total_seconds()
        # ближе к событию → выше давление
        frac = 1.0 - (t / soft_s)
        frac = max(0.0, min(0.85, frac))
        mins = int(round(t / 60))
        side = "через" if when_utc <= e.time else "назад"
        return frac, f"{e.currency} {e.title} {side} {mins}м"

    def upcoming(self, instrument: str, from_utc: pd.Timestamp,
                 horizon_hours: float = 168) -> list[Event]:
        curs = set(currencies_for(instrument))
        end = from_utc + pd.Timedelta(hours=horizon_hours)
        return [e for e in self.events if e.currency in curs and e.impact in self.impacts
                and from_utc <= e.time <= end]
