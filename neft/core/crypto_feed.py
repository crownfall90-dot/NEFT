"""Публичные ленты Telegram для брифинга, не для риск-гейта.

Источники — официальные веб-ленты (t.me/s/...), те же страницы, что
открываются в браузере без логина. Это не архив и не API: последние посты,
без ключа. В риск-слой это не идёт — у постов нет планового времени релиза,
±5 минут вокруг них бессмысленны.

Реклама и фарм (#реклама, дропы) отбрасываются. Остальное размечается по
нашим монетам теми же правилами, что валюта→инструмент в news.py:
статичный словарь, без вызова модели на каждый пост.

Coin Post часто форвардит в Crypto Daily — одинаковые тексты схлопываются.
Dashi Finance — третий источник (web3/AI идеи), тот же фильтр рекламы.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser

import pandas as pd

from neft.core.config import ROOT
from neft.core.routing import CRYPTO_ROUTES, CRYPTO_WATCHLIST

log = logging.getLogger(__name__)
CACHE = ROOT / "data" / "tg_feeds_cache.json"
CHANNELS = {
    "cryptodaily": "https://t.me/s/cryptodaily",
    "coin_post": "https://t.me/s/Coin_Post",
    "dashi_finance": "https://t.me/s/dashi_finance",
}
SKIP_TAGS = ("реклама", "криптоактивности", "15миндень", "партнёрка", "партнерка")

# Тикер в тексте / сленг → наш символ. Только то, чем машина реально торгует
# или держит в watchlist — чужие альты в брифинге помечаются, но не торгуются.
COIN_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC/USDT:USDT": ("btc", "bitcoin", "биток", "биткоин", "битка"),
    "ETH/USDT:USDT": ("eth", "ethereum", "эфир", "эфириум"),
    "BNB/USDT:USDT": ("bnb", "binance coin"),
    "XRP/USDT:USDT": ("xrp", "рипл"),
    "BCH/USDT:USDT": ("bch", "bitcoin cash"),
    "HYPE/USDT:USDT": ("hype", "hyperliquid"),
    "SOL/USDT:USDT": ("sol", "solana", "солана"),
    "DOGE/USDT:USDT": ("doge", "dogecoin", "доги"),
    "ZEC/USDT:USDT": ("zec", "zcash"),
    "ENA/USDT:USDT": ("ena", "ethena"),
    "SUI/USDT:USDT": ("sui",),
    "1000PEPE/USDT:USDT": ("pepe", "1000pepe"),
}

_WORD = re.compile(r"[a-zа-я0-9$]+", re.IGNORECASE)


@dataclass(frozen=True)
class Post:
    time: pd.Timestamp | None
    text: str
    coins: tuple[str, ...]
    tags: tuple[str, ...]
    kind: str  # signal | noise | ad
    source: str = ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class _TgParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.posts: list[dict] = []
        self._in_text = False
        self._depth = 0
        self._buf: list[str] = []
        self._dt: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "")
        if "tgme_widget_message_text" in cls.split():
            self._in_text = True
            self._depth = 1
            self._buf = []
            return
        if self._in_text:
            self._depth += 1
            if tag == "br":
                self._buf.append("\n")
        if tag == "time" and "datetime" in attrs:
            self._dt = attrs["datetime"]

    def handle_endtag(self, tag):
        if not self._in_text:
            return
        self._depth -= 1
        if self._depth <= 0:
            self._in_text = False
            text = _norm("".join(self._buf))
            if text:
                self.posts.append({"time": self._dt, "text": text})

    def handle_data(self, data):
        if self._in_text:
            self._buf.append(data)


def _coins_in(text: str) -> tuple[str, ...]:
    blob = text.lower().replace("$", "")
    words = set(_WORD.findall(blob))
    found = []
    for sym, aliases in COIN_ALIASES.items():
        hit = False
        for a in aliases:
            if " " in a:
                hit = a in blob
            else:
                hit = a in words
            if hit:
                break
        if hit:
            found.append(sym)
    return tuple(found)


def _tags(text: str) -> tuple[str, ...]:
    return tuple(t.lower() for t in re.findall(r"#([A-Za-zА-Яа-я0-9_]+)", text))


def _kind(text: str, tags: tuple[str, ...]) -> str:
    low = text.lower()
    if any(t in SKIP_TAGS for t in tags) or "#реклама" in low:
        return "ad"
    if any(w in low for w in ("фармим", "дроп", "поинты", "дейлик")):
        return "ad"
    return "signal" if _coins_in(text) else "noise"


def _parse_time(raw: str | None) -> pd.Timestamp | None:
    if not raw:
        return None
    try:
        return pd.Timestamp(raw).tz_convert("UTC").tz_localize(None)
    except Exception:
        return None


def _fetch_channel(source: str, url: str) -> list[Post]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", errors="replace")
    parser = _TgParser()
    parser.feed(html)
    posts = []
    for item in parser.posts:
        text = item["text"]
        tags = _tags(text)
        posts.append(Post(
            time=_parse_time(item.get("time")),
            text=text,
            coins=_coins_in(text),
            tags=tags,
            kind=_kind(text, tags),
            source=source,
        ))
    return posts


def _dedupe(posts: list[Post]) -> list[Post]:
    """Один текст из двух каналов (форвард) оставляем один раз."""
    seen: set[str] = set()
    out: list[Post] = []
    for p in posts:
        key = re.sub(r"\s+", " ", p.text.lower())[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def fetch_posts(refresh: bool = False, max_age_minutes: float = 30.0) -> list[Post]:
    if CACHE.exists() and not refresh:
        age = (time.time() - CACHE.stat().st_mtime) / 60
        if age < max_age_minutes:
            raw = json.loads(CACHE.read_text(encoding="utf-8"))
            return [_from_raw(x) for x in raw]

    posts: list[Post] = []
    for source, url in CHANNELS.items():
        try:
            posts += _fetch_channel(source, url)
        except Exception as e:
            log.warning("лента %s недоступна: %s", source, e)
    posts = _dedupe(posts)
    posts.sort(key=lambda p: p.time or pd.Timestamp.min)
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps([
        {"time": str(p.time) if p.time is not None else None,
         "text": p.text, "coins": list(p.coins),
         "tags": list(p.tags), "kind": p.kind, "source": p.source}
        for p in posts
    ], ensure_ascii=False), encoding="utf-8")
    return posts


def _from_raw(x: dict) -> Post:
    t = x.get("time")
    return Post(
        time=pd.Timestamp(t) if t else None,
        text=x["text"],
        coins=tuple(x.get("coins") or ()),
        tags=tuple(x.get("tags") or ()),
        kind=x.get("kind", "noise"),
        source=x.get("source", ""),
    )


def relevant(posts: list[Post] | None = None, *, ours_only: bool = True) -> list[Post]:
    posts = posts if posts is not None else fetch_posts()
    ours = set(CRYPTO_ROUTES) | set(CRYPTO_WATCHLIST)
    out = []
    for p in posts:
        if p.kind == "ad":
            continue
        if ours_only and p.coins and not (set(p.coins) & ours):
            continue
        if ours_only and not p.coins and p.kind != "signal":
            continue
        out.append(p)
    return out


def label(symbol: str) -> str:
    return symbol.replace("/USDT:USDT", "")


# Слова, после которых по этой монете новые входы режем. Не сигнал на шорт:
# пост почти всегда опаздывает к движению. Только стоп-кран на новые сделки.
SHOCK_WORDS = (
    "hack", "взлом", "exploit", "эксплойт", "drained", "украли",
    "delist", "делист", "делистинг", "halt", "остановлен торг",
    "paused withdrawals", "вывод приостанов", "выводы заморож",
    "insolvent", "банкрот", "rug", "rugpull", "скам", "scam",
    "bridge exploit", "compromised", "compromis",
)


@dataclass(frozen=True)
class Shock:
    symbol: str
    reason: str
    time: pd.Timestamp | None
    source: str
    text: str


def detect_shocks(posts: list[Post] | None = None, *,
                  max_age_minutes: float = 60.0) -> list[Shock]:
    """Свежие посты с шок-словами по нашим монетам. Не ставит ордера."""
    posts = posts if posts is not None else fetch_posts()
    now = pd.Timestamp.utcnow().tz_localize(None)
    out: list[Shock] = []
    seen: set[str] = set()
    for p in posts:
        if p.kind == "ad" or not p.coins:
            continue
        if p.time is not None:
            age = (now - p.time).total_seconds() / 60.0
            if age > max_age_minutes or age < -5:
                continue
        low = p.text.lower()
        hit = next((w for w in SHOCK_WORDS if w in low), None)
        if not hit:
            continue
        snippet = p.text.replace("\n", " ").strip()[:120]
        for coin in p.coins:
            if coin in seen:
                continue
            seen.add(coin)
            out.append(Shock(coin, hit, p.time, p.source, snippet))
    return out
