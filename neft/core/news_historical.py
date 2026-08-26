"""Исторический календарь для ретроспективной проверки новостного фильтра.

Живой фид ForexFactory (neft/core/news.py) даёт только текущую неделю — на
бэктестах его использовать нечем. FRED (Федеральный резерв США) — единственный
подтверждённо бесплатный источник с реальной историей: даты релизов основных
показателей США за любой период, без лимита и без платного тарифа.

Ограничения, о которых нужно знать заранее:
  * только США — валюты EUR/GBP/JPY/AUD/CAD здесь не покрыть;
  * ISM Manufacturing PMI не публикуется через FRED из-за лицензионных
    ограничений правообладателя — этого индикатора в списке ниже нет;
  * даты заседаний FOMC — не "релиз данных" в терминологии FRED (H.15
    обновляется еженедельно, это не то же самое, что дата самого заседания),
    поэтому они здесь фиксированным списком, а не тянутся через API.

Список ID релизов подобран под то же обоснование, что и в news.py: это
показатели, которые реально двигают USD и всё, что от него зависит
(XAUUSD, NAS100/DJ30, вся крипта через доллар-ликвидность).
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import pandas as pd

from neft.core.config import ROOT, settings

log = logging.getLogger(__name__)

# release_id проверены по fred.stlouisfed.org/release?rid=<id> вручную —
# API не отдаёт человекочитаемых названий без отдельного запроса.
FRED_RELEASES: dict[int, str] = {
    10: "CPI",                          # Consumer Price Index
    50: "NFP",                          # Employment Situation (Non-Farm Payrolls)
    53: "GDP",                          # Gross Domestic Product
    46: "PPI",                          # Producer Price Index
    54: "PCE",                          # Personal Income and Outlays (любимый индекс ФРС)
}

# Даты заседаний FOMC публикуются Федрезервом на год-два вперёд заранее —
# фиксированный список надёжнее и точнее, чем пытаться вывести их из H.15.
# Обновлять вручную по https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
FOMC_MEETING_DATES: list[str] = [
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30",
    "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]

BASE = "https://api.stlouisfed.org/fred/release/dates"
CACHE = ROOT / "data" / "fred_cache.json"


@dataclass(frozen=True)
class HistoricalEvent:
    time: pd.Timestamp   # naive, UTC (FRED не даёт точное время суток релиза)
    currency: str
    title: str
    # Курированный список ниже — только показатели, которые двигают рынок по
    # определению их отбора, поэтому impact фиксирован. Поле существует
    # ради совместимости с NewsGate.blocked() (тот же интерфейс, что Event
    # из news.py) — GATE не должен знать, что источник данных другой.
    impact: str = "High"


def _api_key() -> str:
    key = getattr(settings, "fred_api_key", None)
    if not key:
        raise RuntimeError(
            "FRED_API_KEY не задан в .env. Зарегистрируйтесь бесплатно на "
            "https://fredaccount.stlouisfed.org/login/secure/ и добавьте "
            "ключ в .env — это разовая регистрация, картой платить не нужно."
        )
    return key


def _fetch_release_dates(release_id: int, start: str, end: str,
                         retries: int = 3) -> list[str]:
    params = urllib.parse.urlencode({
        "release_id": release_id, "realtime_start": start, "realtime_end": end,
        "api_key": _api_key(), "file_type": "json",
        "include_release_dates_with_no_data": "false",
    })
    url = f"{BASE}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            return [d["date"] for d in data.get("release_dates", [])]
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise last_err


def _et_to_utc(date_str: str, hh_mm: str) -> pd.Timestamp:
    """Местное время США -> UTC с учётом перехода на летнее время.

    Фиксированное смещение (например, всегда UTC-4) даёт ошибку на час в
    зимних релизах — критично при буфере блокировки всего в 5 минут.
    """
    local = pd.Timestamp(f"{date_str} {hh_mm}:00", tz="America/New_York")
    return local.tz_convert("UTC").tz_localize(None)


def load_historical(start: str, end: str,
                    releases: dict[int, str] = FRED_RELEASES) -> list[HistoricalEvent]:
    """Даты релизов + даты заседаний FOMC за произвольный период в прошлом.

    Требует FRED_API_KEY в .env. Время релиза FRED не публикует точнее дня —
    используем стандартное время публикации статистики США, **08:30
    America/New_York** для CPI/NFP/GDP/PPI/PCE (это не приближение "плюс-минус
    час", а официальное фиксированное время публикации BLS/BEA), пересчитанное
    в UTC с учётом DST на КАЖДУЮ дату отдельно. FOMC объявляет решение в 14:00
    America/New_York.
    """
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    cache_key = f"{start}|{end}|{','.join(str(k) for k in releases)}"
    if CACHE.exists():
        try:
            blob = json.loads(CACHE.read_text(encoding="utf-8"))
            if blob.get("key") == cache_key:
                return [
                    HistoricalEvent(
                        time=pd.Timestamp(e["time"]), currency=e["currency"],
                        title=e["title"], impact=e.get("impact", "High"),
                    )
                    for e in blob["events"]
                ]
        except Exception:
            log.warning("кэш FRED повреждён — качаю заново")

    out: list[HistoricalEvent] = []
    for rid, label in releases.items():
        for date_str in _fetch_release_dates(rid, start, end):
            d = pd.Timestamp(date_str)
            if d < start_ts or d > end_ts:
                continue
            out.append(HistoricalEvent(
                time=_et_to_utc(date_str, "08:30"), currency="USD", title=label,
            ))
    for date_str in FOMC_MEETING_DATES:
        d = pd.Timestamp(date_str)
        if start_ts <= d <= end_ts:
            out.append(HistoricalEvent(
                time=_et_to_utc(date_str, "14:00"), currency="USD",
                title="FOMC Rate Decision",
            ))
    out = sorted(out, key=lambda e: e.time)
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps({
        "key": cache_key,
        "events": [
            {"time": str(e.time), "currency": e.currency,
             "title": e.title, "impact": e.impact}
            for e in out
        ],
    }, ensure_ascii=False), encoding="utf-8")
    return out
