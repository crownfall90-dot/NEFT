"""Вход в веб-панель по подтверждению в Telegram."""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from neft.core.config import ROOT, settings

CHALLENGES_PATH = ROOT / "logs" / "panel_login.json"
COOLDOWN_PATH = ROOT / "logs" / "panel_login_cooldown.json"
TTL = 300
DENY_COOLDOWN = 60
TG_API = "https://api.telegram.org/bot{token}/{method}"


def allowed_ids() -> set[int]:
    raw = " ,".join(
        x for x in (settings.telegram_allowed_ids, settings.telegram_chat_id) if x
    )
    out: set[int] = set()
    for chunk in raw.replace(" ", "").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.add(int(chunk))
    return out


def tg_enabled() -> bool:
    return bool(settings.telegram_bot_token) and bool(allowed_ids())


def _load() -> dict:
    if not CHALLENGES_PATH.exists():
        return {}
    try:
        return json.loads(CHALLENGES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    CHALLENGES_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    data = {k: v for k, v in data.items() if float(v.get("exp") or 0) > now - 120}
    CHALLENGES_PATH.write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _load_cooldown() -> dict:
    if not COOLDOWN_PATH.exists():
        return {}
    try:
        return json.loads(COOLDOWN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cooldown(data: dict) -> None:
    COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    data = {k: float(v) for k, v in data.items() if float(v) > now}
    COOLDOWN_PATH.write_text(json.dumps(data), encoding="utf-8")


def deny_cooldown_left(ip: str) -> int:
    until = float(_load_cooldown().get(ip or "127.0.0.1") or 0)
    return max(0, int(until - time.time()))


def mark_denied(ip: str) -> None:
    data = _load_cooldown()
    data[ip or "127.0.0.1"] = time.time() + DENY_COOLDOWN
    _save_cooldown(data)


def tg_call(method: str, **params) -> dict:
    token = settings.telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    url = TG_API.format(token=token, method=method)
    data = urlencode(params).encode("utf-8")
    req = Request(url, data=data, headers={"User-Agent": "NEFT"})
    with urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def notify_login(cid: str, ip: str) -> bool:
    text = (
        "🔐 <b>Вход в панель NEFT</b>\n"
        f"Запрос с компьютера <code>{ip}</code>.\n"
        "Разрешить доступ к панели?"
    )
    kb = json.dumps({
        "inline_keyboard": [[
            {"text": "✅ Разрешить", "callback_data": f"login:ok:{cid}"},
            {"text": "❌ Отклонить", "callback_data": f"login:no:{cid}"},
        ]],
    })
    ok = False
    for chat_id in allowed_ids():
        try:
            tg_call(
                "sendMessage",
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview="true",
            )
            ok = True
        except (URLError, HTTPError, RuntimeError, json.JSONDecodeError):
            pass
    return ok


def create_challenge(ip: str) -> tuple[str | None, str | None]:
    if not tg_enabled():
        return None, "Telegram не настроен — задайте TELEGRAM_BOT_TOKEN и TELEGRAM_ALLOWED_IDS"
    ip = ip or "127.0.0.1"
    left = deny_cooldown_left(ip)
    if left > 0:
        return None, f"повтор через {left} сек — вход был отклонён"
    cid = secrets.token_urlsafe(16)
    now = time.time()
    data = _load()
    data[cid] = {
        "created": now,
        "exp": now + TTL,
        "ip": ip or "127.0.0.1",
        "status": "pending",
    }
    _save(data)
    if not notify_login(cid, ip or "127.0.0.1"):
        data.pop(cid, None)
        _save(data)
        return None, "не удалось отправить запрос в Telegram — бот запущен?"
    return cid, None


def poll_challenge(cid: str) -> dict:
    ch = _load().get(str(cid or "").strip())
    if not ch:
        return {"status": "expired"}
    if float(ch.get("exp") or 0) < time.time():
        return {"status": "expired"}
    status = ch.get("status") or "pending"
    out: dict = {"status": status}
    if status == "denied":
        out["retry_after"] = deny_cooldown_left(str(ch.get("ip") or "127.0.0.1"))
    return out


def decide(cid: str, approve: bool, tg_user: int) -> tuple[bool, str]:
    if tg_user not in allowed_ids():
        return False, "нет доступа"
    data = _load()
    ch = data.get(cid)
    if not ch:
        return False, "запрос не найден"
    if float(ch.get("exp") or 0) < time.time():
        return False, "истекло"
    if ch.get("status") != "pending":
        return False, "уже обработано"
    ch["status"] = "approved" if approve else "denied"
    ch["by"] = tg_user
    if not approve:
        mark_denied(str(ch.get("ip") or "127.0.0.1"))
    data[cid] = ch
    _save(data)
    return True, "approved" if approve else "denied"


def consume(cid: str) -> bool:
    data = _load()
    ch = data.get(str(cid or "").strip())
    if not ch or ch.get("status") != "approved":
        return False
    if float(ch.get("exp") or 0) < time.time():
        return False
    ch["status"] = "used"
    data[str(cid).strip()] = ch
    _save(data)
    return True
