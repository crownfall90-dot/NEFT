"""Фильтр по новостям: не входить в сделку рядом с плановым релизом.

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
import json
import logging
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
# Крипта: коррелирует с USD-ликвидностью (ставки ФРС, CPI, NFP двигают
# аппетит к риску), реакция на данные отдельных стран (GBP CPI, AUD
# занятость) статистически не отличима от нуля. Разметка по нюансам
# отдельных монет не нужна — фильтр по USD один на всю крипту.
INSTRUMENT_CURRENCIES: dict[str, list[str]] = {
    # индексы
    "NAS100": ["USD"], "DJ30": ["USD"], "US500": ["USD"],
    "GER40": ["EUR", "USD"], "FRA40": ["EUR", "USD"], "ES35": ["EUR", "USD"],
    "UK100": ["GBP", "USD"], "CHINA50": ["CNY", "USD"],
    # металл
    "XAUUSD+": ["USD"], "XAUUSD": ["USD"],
    # форекс — обе ноги пары
    "EURUSD+": ["EUR", "USD"], "GBPUSD+": ["GBP", "USD"],
    "USDJPY+": ["USD", "JPY"], "AUDUSD+": ["AUD", "USD"],
    "USDCAD+": ["USD", "CAD"], "USDCHF+": ["USD", "CHF"],
    "NZDUSD+": ["NZD", "USD"], "EURJPY+": ["EUR", "JPY"],
    "GBPJPY+": ["GBP", "JPY"], "EURGBP+": ["EUR", "GBP"],
}

# Крипта: единая привязка к USD для всех пар вида "*/USDT:USDT".
CRYPTO_CURRENCIES = ["USD"]


def currencies_for(instrument: str) -> list[str]:
    if instrument in INSTRUMENT_CURRENCIES:
        return INSTRUMENT_CURRENCIES[instrument]
    if "/USDT" in instrument or instrument.endswith("USDT"):
        return CRYPTO_CURRENCIES
    return []


@dataclass(frozen=True)
class Event:
    time: pd.Timestamp   # naive, UTC
    currency: str
    impact: str
    title: str


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
    """Календарь этой и следующей недели, с кэшем на диск.

    Источник обновляется нечасто (расписание известно заранее), поэтому
    кэш на несколько часов не теряет актуальности, а живых запросов
    в торговом цикле не требует вовсе.
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
        out.append(Event(time=t, currency=e.get("country", ""),
                         impact=e.get("impact", "Low"), title=e.get("title", "")))
    return sorted(out, key=lambda x: x.time)


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
