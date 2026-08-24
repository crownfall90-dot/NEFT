"""Мгновенные торговые уведомления в Telegram — вход/выход/TP/SL.

Не путать с scripts/telegram_bot.py (отдельный процесс, поллит команды
пользователя) — это разовая отправка сообщения прямо из движка
(scripts/crypto_forward.py) в момент события. Тихо ничего не делает, если
токен/chat_id не заданы или Telegram недоступен — торговый цикл это никогда
не должно останавливать.
"""
from __future__ import annotations

import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from neft.core.config import settings

log = logging.getLogger("tg_notify")
TG_API = "https://api.telegram.org/bot{token}/sendMessage"


def _chat_ids() -> list[int]:
    raw = ",".join(x for x in (settings.telegram_allowed_ids, settings.telegram_chat_id) if x)
    out: list[int] = []
    for chunk in raw.replace(" ", "").split(","):
        chunk = chunk.strip()
        if chunk.isdigit() and int(chunk) not in out:
            out.append(int(chunk))
    return out


def notify(text: str) -> None:
    token = settings.telegram_bot_token
    if not token:
        return
    ids = _chat_ids()
    if not ids:
        return
    url = TG_API.format(token=token)
    for chat_id in ids:
        data = urlencode({
            "chat_id": chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = Request(url, data=data, headers={"User-Agent": "NEFT"})
        try:
            with urlopen(req, timeout=10):
                pass
        except (URLError, HTTPError, OSError) as e:  # noqa: BLE001
            log.warning("sendMessage: %s", e)
