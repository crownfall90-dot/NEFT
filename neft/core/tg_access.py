"""Приглашения в Telegram-бота и права приглашённых.

Владелец (TELEGRAM_ALLOWED_IDS / TELEGRAM_CHAT_ID в .env) может выдать
одноразовую ссылку-приглашение. Перешедший по ней получает доступ к боту
с той ролью, которая была выбрана при создании ссылки:

    viewer — только чтение: дашборд, сделки, уведомления;
    trader — плюс запуск/остановка тест- и демо-режима и вход в веб-панель.

LIVE (боевой счёт) недоступен приглашённым НИ ПРИ КАКОЙ роли: включить
торговлю реальными деньгами может только владелец — Telegram id из .env
(TELEGRAM_ALLOWED_IDS / TELEGRAM_CHAT_ID). Это проверяется и в боте, и в
веб-панели, а не только прячется в интерфейсе.
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path

from neft.core.config import ROOT

STORE = ROOT / "data" / "tg_access.json"
INVITE_TTL = 24 * 3600  # сутки: ссылка не должна жить вечно


def _load() -> dict:
    if not STORE.exists():
        return {"invites": {}, "users": {}}
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"invites": {}, "users": {}}
    data.setdefault("invites", {})
    data.setdefault("users", {})
    return data


def _save(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STORE)


def create_invite(role: str, by: int) -> str:
    """Одноразовый код приглашения. Роль: viewer | admin."""
    role = role if role in ("viewer", "trader") else "viewer"
    code = secrets.token_urlsafe(9)
    data = _load()
    # Чистим протухшие, чтобы файл не рос без предела.
    now = time.time()
    data["invites"] = {k: v for k, v in data["invites"].items()
                       if now - float(v.get("ts") or 0) < INVITE_TTL}
    data["invites"][code] = {"role": role, "ts": now, "by": by}
    _save(data)
    return code


def redeem(code: str, uid: int, name: str = "") -> str | None:
    """Активировать приглашение. Возвращает роль или None."""
    if not code:
        return None
    data = _load()
    inv = data["invites"].get(code)
    if not inv:
        return None
    if time.time() - float(inv.get("ts") or 0) >= INVITE_TTL:
        data["invites"].pop(code, None)
        _save(data)
        return None
    role = inv.get("role", "viewer")
    data["invites"].pop(code, None)  # одноразовая
    data["users"][str(uid)] = {"role": role, "name": name, "ts": time.time()}
    _save(data)
    return role


def role_of(uid: int) -> str | None:
    """Роль приглашённого пользователя, либо None."""
    rec = _load()["users"].get(str(uid))
    return rec.get("role") if rec else None


def guests() -> list[dict]:
    out = []
    for uid, rec in _load()["users"].items():
        out.append({"id": int(uid), "role": rec.get("role", "viewer"),
                    "name": rec.get("name", "")})
    return out


def revoke(uid: int) -> bool:
    data = _load()
    if data["users"].pop(str(uid), None) is None:
        return False
    _save(data)
    return True


def set_role(uid: int, role: str) -> bool:
    """Сменить права уже приглашённого без новой ссылки."""
    role = role if role in ("viewer", "trader") else "viewer"
    data = _load()
    rec = data["users"].get(str(uid))
    if rec is None:
        return False
    rec["role"] = role
    data["users"][str(uid)] = rec
    _save(data)
    return True


def revoke_all() -> int:
    data = _load()
    n = len(data["users"])
    data["users"] = {}
    data["invites"] = {}
    _save(data)
    return n
