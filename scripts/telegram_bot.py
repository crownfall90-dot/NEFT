"""Telegram-бот панели NEFT — статус и минимальное управление.

Токен и список разрешённых Telegram id — в .env:
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_ALLOWED_IDS=123456789,987654321

Статус читает тот же dashboard/crypto_chart.json, что и веб-панель.
Кнопки «Тест» / «Демо» / «Стоп» дёргают ТЕ ЖЕ /api/run и /api/stop, что и
сама панель — тем же токеном, той же сессией, теми же предохранителями
(rate-limit, DEMO_ONLY, мягкий стоп при открытой позиции). Бой ("live")
через Telegram не запускается никогда — только с панели, с явным
подтверждением на месте.

Запускать рядом с admin_server.py:
  ./.venv/Scripts/python.exe scripts/telegram_bot.py
"""
import json
import logging
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from neft.core import tg_access  # noqa: E402
from neft.core.config import settings  # noqa: E402
from neft.core.machine_config import account_for_mode  # noqa: E402
from neft.core.panel_guard import panel_token  # noqa: E402

CHART_JSON = ROOT / "dashboard" / "crypto_chart.json"
TG_API = "https://api.telegram.org/bot{token}/{method}"
ADMIN_BASE = "http://127.0.0.1:8787"

log = logging.getLogger("telegram_bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

MODE_RU = {"paper": "тест", "demo": "демо", "live": "бой", "": "выключен"}


# ——— доступ ———

def owner_ids() -> set[int]:
    """Владельцы — только id из .env. LIVE и выдача доступов только им."""
    raw = " ,".join(x for x in (settings.telegram_allowed_ids, settings.telegram_chat_id) if x)
    out = set()
    for chunk in raw.replace(" ", "").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.add(int(chunk))
    return out


def is_owner(uid: int) -> bool:
    return uid in owner_ids()


def role_for(uid: int) -> str | None:
    """owner | trader | viewer | None."""
    if is_owner(uid):
        return "owner"
    return tg_access.role_of(uid)


def can_trade(uid: int) -> bool:
    """Запуск/остановка тест- и демо-режима."""
    return role_for(uid) in ("owner", "trader")


def can_live(uid: int) -> bool:
    """Боевой счёт — только владелец, приглашённым недоступен никогда."""
    return is_owner(uid)


def allowed_ids() -> set[int]:
    """Кому бот вообще отвечает: владельцы + приглашённые."""
    return owner_ids() | {g["id"] for g in tg_access.guests()}


# ——— Telegram Bot API ———

def tg_call(method: str, **params) -> dict:
    token = settings.telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")
    url = TG_API.format(token=token, method=method)
    data = urlencode(params).encode("utf-8")
    req = Request(url, data=data, headers={"User-Agent": "NEFT"})
    with urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def kb(rows: list[list[tuple[str, str]]]) -> str:
    return json.dumps({"inline_keyboard": [
        [{"text": t, "callback_data": d} for t, d in row] for row in rows
    ]})


def send(chat_id: int, text: str, buttons: str | None = None) -> None:
    kwargs = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"}
    if buttons:
        kwargs["reply_markup"] = buttons
    try:
        tg_call("sendMessage", **kwargs)
    except (URLError, HTTPError) as e:
        log.warning("sendMessage: %s", e)


def edit(chat_id: int, message_id: int, text: str, buttons: str | None = None) -> None:
    kwargs = {"chat_id": chat_id, "message_id": message_id, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": "true"}
    if buttons:
        kwargs["reply_markup"] = buttons
    try:
        tg_call("editMessageText", **kwargs)
    except (URLError, HTTPError) as e:
        log.warning("editMessageText: %s", e)


def answer_cb(cb_id: str, text: str = "") -> None:
    try:
        tg_call("answerCallbackQuery", callback_query_id=cb_id, text=text)
    except (URLError, HTTPError) as e:
        log.warning("answerCallbackQuery: %s", e)


# ——— админ-панель: та же сессия, что у веб-морды ———

_session: dict[str, str | None] = {"cookie": None, "csrf": None}


def _cookie_jar(raw_headers) -> dict[str, str]:
    out = {}
    for line in raw_headers or []:
        first = line.split(";", 1)[0]
        k, _, v = first.partition("=")
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def admin_login() -> bool:
    url = f"{ADMIN_BASE}/api/login"
    data = json.dumps({"token": panel_token()}).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json",
                                            "User-Agent": "NEFT"}, method="POST")
    try:
        with urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
            jar = _cookie_jar(r.headers.get_all("Set-Cookie"))
    except (URLError, HTTPError) as e:
        log.warning("admin login: %s", e)
        return False
    sess, sig, csrf = jar.get("neft_sess"), jar.get("neft_sig"), jar.get("neft_csrf")
    if not (sess and sig and csrf):
        return False
    _session["cookie"] = f"neft_sess={sess}; neft_sig={sig}"
    _session["csrf"] = csrf
    return bool(body.get("ok"))


def admin_post(path: str, **payload) -> dict:
    if not _session["cookie"] and not admin_login():
        return {"ok": False, "error": "нет связи с панелью (admin_server запущен?)"}
    url = f"{ADMIN_BASE}{path}"
    data = json.dumps(payload).encode("utf-8")

    def _do() -> dict | None:
        headers = {"Content-Type": "application/json", "User-Agent": "NEFT",
                   "Cookie": _session["cookie"], "X-NEFT-CSRF": _session["csrf"]}
        req = Request(url, data=data, headers=headers, method="POST")
        with urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    try:
        return _do()
    except HTTPError as e:
        if e.code in (401, 403) and admin_login():
            try:
                return _do()
            except (URLError, HTTPError) as e2:
                return {"ok": False, "error": str(e2)}
        return {"ok": False, "error": f"HTTP {e.code}"}
    except URLError as e:
        return {"ok": False, "error": "нет связи с панелью — " + str(e)}


def admin_get(path: str) -> dict:
    if not _session["cookie"] and not admin_login():
        return {"ok": False, "error": "нет связи с панелью (admin_server запущен?)"}
    url = f"{ADMIN_BASE}{path}"

    def _do() -> dict:
        headers = {"User-Agent": "NEFT", "Cookie": _session["cookie"],
                   "X-NEFT-CSRF": _session["csrf"]}
        req = Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=12) as r:
            return json.loads(r.read())

    try:
        return _do()
    except HTTPError as e:
        if e.code in (401, 403) and admin_login():
            try:
                return _do()
            except (URLError, HTTPError) as e2:
                return {"ok": False, "error": str(e2)}
        return {"ok": False, "error": f"HTTP {e.code}"}
    except URLError as e:
        return {"ok": False, "error": "нет связи с панелью — " + str(e)}


# ——— экраны ———

_chat_view: dict[int, str] = {}
_BOT_USERNAME: str = ""


def set_view(chat_id: int, view: str) -> None:
    _chat_view[chat_id] = view


def get_view(chat_id: int) -> str:
    return _chat_view.get(chat_id, "home")

def money(x) -> str:
    try:
        return f"{float(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def load_state() -> dict:
    if not CHART_JSON.exists():
        return {}
    try:
        return json.loads(CHART_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def open_positions(data: dict) -> list[dict]:
    books = data.get("books") or []
    return [b for b in books
            if b.get("position") or (b.get("setup") or {}).get("kind") == "position"]


def pos_size_info(p: dict, data: dict) -> dict | None:
    """Нотионал и маржа позиции — как posSizeInfo() в веб-панели."""
    try:
        vol = float(p.get("volume") or 0)
        cs = float(p.get("contract_size") or 1)
        entry = float(p.get("entry") or p.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if vol <= 0 or entry <= 0:
        return None
    notional = abs(vol * cs * entry)
    lev = max(1.0, float(data.get("leverage") or 5))
    margin = notional / lev
    eq = float(data.get("equity") or 0)
    return {
        "notional": notional,
        "margin": margin,
        "eq_pct": (notional / eq * 100) if eq > 0 else None,
        "margin_pct": (margin / eq * 100) if eq > 0 else None,
    }


def fmt_pos_size(info: dict | None) -> str:
    if not info or info.get("notional", 0) <= 0:
        return ""
    bits = [f"объём <b>{money(info['notional'])}</b>"]
    mp = info.get("margin_pct")
    if mp is not None:
        bits.append(f"маржа <b>{money(info['margin'])}</b> ({mp:.1f}% экв.)")
    return " · ".join(bits)


def fmt_max_trade(data: dict) -> str:
    """Макс. объём следующего входа — из max_trade в снимке бота."""
    mt = data.get("max_trade") or {}
    risk = mt.get("risk_usd")
    notional = float(mt.get("notional") or 0)
    margin = mt.get("margin")
    eq = float(data.get("equity") or 0)
    if not risk and eq > 0:
        rp = float(data.get("risk") or 1)
        risk = eq * rp / 100
    if notional > 0:
        mp = (float(margin) / eq * 100) if eq > 0 and margin else None
        bits = [f"макс. вход <b>{money(notional)}</b>"]
        if margin is not None:
            mp_txt = f" ({mp:.1f}% экв.)" if mp is not None else ""
            bits.append(f"маржа <b>{money(margin)}</b>{mp_txt}")
        if risk:
            bits.append(f"риск <b>{money(risk)}</b>")
        return " · ".join(bits)
    if risk:
        return f"макс. риск <b>{money(risk)}</b> · ждём сетап"
    return ""


def fmt_pos_line(b: dict, data: dict) -> str:
    p = b.get("position") or (b.get("setup") or {})
    side = "🔴 шорт" if str(p.get("side")) == "sell" else "🟢 лонг"
    entry = p.get("entry") or p.get("price")
    pnl = b.get("pnl_open")
    pnl_txt = ""
    if pnl is not None:
        pnl_txt = f" · P&amp;L {'+' if pnl >= 0 else ''}{money(pnl)}"
    sz = fmt_pos_size(pos_size_info(p, data))
    sz_txt = f"\n   {sz}" if sz else ""
    return (
        f"{side} <b>{b.get('label') or b.get('pair') or ''}</b> · {money(entry)}"
        f"{pnl_txt}{sz_txt}"
    )


def dashboard_text(data: dict | None = None) -> tuple[str, dict]:
    if data is None:
        data = load_state()
    if not data:
        return ("⚪ Панель не отвечает — нет dashboard/crypto_chart.json.\n"
                "Запущен ли admin_server?", data)
    mode = data.get("mode") or ""
    equity, start = data.get("equity"), data.get("start")
    # mode в снимке — это режим ПОСЛЕДНЕГО запуска, он остаётся в файле и
    # после остановки. Живой статус спрашиваем у админки, иначе выключенный
    # бот показывал бы «🟢 режим: демо».
    running = bot_running(data)
    lines = [
        "📊 <b>Дашборд</b>",
        (f"🟢 работает · <b>{MODE_RU.get(mode, mode)}</b>" if running
         else "⚫ <b>бот выключен</b>"),
    ]
    wallet = data.get("wallet")
    if mode == "live" and wallet is not None:
        lines.append(f"🏦 баланс Bybit: <b>{money(wallet)}</b>")
        lines.append(f"💰 эквити: <b>{money(equity)}</b>")
        if isinstance(equity, (int, float)) and isinstance(wallet, (int, float)):
            pnl = equity - wallet
        else:
            pnl = None
    else:
        dep = f" (депозит {money(start)})" if start is not None else ""
        lines.append(f"💰 эквити: <b>{money(equity)}</b>{dep}")
        pnl = ((equity - start) if isinstance(equity, (int, float))
               and isinstance(start, (int, float)) else None)
    if pnl is not None:
        arrow = "📈" if pnl >= 0 else "📉"
        lines.append(f"{arrow} P&amp;L: <b>{'+' if pnl >= 0 else ''}{money(pnl)}</b>")
    avail = data.get("available")
    if avail is not None:
        lines.append(f"💵 свободно: <b>{money(avail)}</b>")
    used = data.get("used_margin")
    margin_pct = data.get("margin_pct")
    if isinstance(used, (int, float)) and used > 0:
        mp = f" ({float(margin_pct):.1f}% экв.)" if isinstance(margin_pct, (int, float)) else ""
        lines.append(f"🔒 в сделке: <b>{money(used)}</b>{mp}")
    max_txt = fmt_max_trade(data)
    if max_txt:
        lines.append(f"📐 {max_txt}")
    pos = open_positions(data)
    lines.append(f"📈 открытых: <b>{len(pos)}</b>")
    if pos:
        lines.append("")
        for b in pos[:5]:
            lines.append(fmt_pos_line(b, data))
    if data.get("paused"):
        lines.append("")
        lines.append("⏸ вход заблокирован (мягкий стоп)")
    return "\n".join(lines), data


def settings_text(data: dict | None = None) -> tuple[str, dict]:
    if data is None:
        data = load_state()
    st = admin_get("/api/state")
    cfg = st.get("config") or {}
    mode = data.get("mode") or cfg.get("account_mode") or ""
    acc = account_for_mode(cfg, mode if mode in ("paper", "demo", "live") else "demo")
    running = bot_running(data)
    lines = [
        "⚙️ <b>Настройки</b>",
        "",
        (f"🟢 работает · <b>{MODE_RU.get(mode, mode)}</b>" if running
         else f"⚫ выключен · профиль <b>{MODE_RU.get(mode, mode or 'демо')}</b>"),
        f"риск на сделку: <b>{data.get('risk', acc.get('risk_pct', '—'))}%</b>",
        f"плечо: <b>{data.get('leverage', acc.get('leverage_crypto', '—'))}x</b>",
    ]
    if mode == "live":
        wallet = data.get("wallet")
        if wallet is not None:
            lines.append(f"баланс Bybit: <b>{money(wallet)}</b>")
    else:
        lines.append(f"депозит: <b>{money(data.get('start') or acc.get('deposit'))}</b>")
    max_txt = fmt_max_trade(data)
    if max_txt:
        lines.append(max_txt)
    if acc.get("max_open_positions") is not None:
        lines.append(f"макс. позиций: <b>{acc['max_open_positions']}</b>")
    if acc.get("max_daily_loss_pct") is not None:
        lines.append(f"стоп дня: <b>{acc['max_daily_loss_pct']}%</b>")
    ctrl = st.get("control") or {}
    if ctrl.get("paused"):
        lines.append("⏸ вход заблокирован")
    lines.extend([
        "",
        "Управление ботом — кнопками ниже.",
        f"Полная панель: {ADMIN_BASE}",
    ])
    return "\n".join(lines), data


def trades_text(data: dict | None = None) -> tuple[str, dict]:
    if data is None:
        data = load_state()
    mode = data.get("mode") or "paper"
    if mode not in ("paper", "demo", "live"):
        mode = "paper"
    hist = admin_get(f"/api/history?mode={mode}")
    closed = (hist.get("positions") or []) if hist.get("ok") else []
    lines = ["💼 <b>Сделки</b>", ""]
    if hist.get("ok") and hist.get("stats"):
        s = hist["stats"]
        lines.append(
            f"всего <b>{s.get('trades', len(closed))}</b> · "
            f"винрейт <b>{s.get('winrate', 0)}%</b> · "
            f"P&amp;L <b>{'+' if (s.get('pnl') or 0) >= 0 else ''}{money(s.get('pnl'))}</b>"
        )
        lines.append("")
    pos = open_positions(data)
    if pos:
        lines.append("<b>Сейчас открыто:</b>")
        for b in pos[:5]:
            lines.append(fmt_pos_line(b, data))
        lines.append("")
    if not closed:
        lines.append("Закрытых сделок пока нет.")
        return "\n".join(lines), data
    lines.append("<b>Последние закрытые:</b>")
    for p in list(reversed(closed))[:10]:
        side = "🟢" if p.get("side") == "buy" else "🔴"
        pnl = float(p.get("pnl") or 0)
        sign = "+" if pnl >= 0 else ""
        coin = p.get("coin") or "?"
        tf = p.get("tf") or ""
        strat = p.get("strategy") or ""
        win = "✓" if p.get("win") else ("✗" if p.get("loss") else "·")
        lines.append(f"{win} {side} <b>{coin}</b> {tf} · {sign}{money(pnl)} · {strat}")
    return "\n".join(lines), data


def bot_running(data: dict | None = None) -> bool:
    """Работает ли бот прямо сейчас — по списку живых задач админки."""
    st = admin_get("/api/state") or {}
    jobs = [j for j in (st.get("jobs") or []) if j.get("running")]
    return bool(jobs)


def home_text(data: dict | None = None) -> tuple[str, dict]:
    """Стартовый экран: включён или нет, что это за бот, куда идти дальше."""
    if data is None:
        data = load_state()
    mode = (data or {}).get("mode") or ""
    running = bot_running(data)
    if running:
        head = f"🟢 <b>Бот работает</b> · режим {MODE_RU.get(mode, mode)}"
    else:
        head = "⚫ <b>Бот выключен</b>"
    lines = [
        "<b>NEFT</b> · торговый терминал",
        "",
        head,
    ]
    if running and (data or {}).get("paused"):
        lines.append("⏸ новые входы заблокированы")
    equity = (data or {}).get("equity")
    start = (data or {}).get("start")
    if equity is not None:
        pnl = (equity - start) if isinstance(equity, (int, float)) and isinstance(start, (int, float)) else None
        tail = ""
        if pnl is not None:
            tail = f" · {'+' if pnl >= 0 else ''}{money(pnl)}"
        lines.append(f"💰 эквити <b>{money(equity)}</b>{tail}")
    pos = open_positions(data or {})
    lines.append(f"📈 открытых позиций: <b>{len(pos)}</b>")
    lines += [
        "",
        "Торгует крипту на Bybit по своим стратегиям: сам ищет вход,",
        "ставит стоп и тейки, ведёт позицию до выхода.",
        "Риск на сделку ограничен настройками — больше не возьмёт.",
        "",
        "👇 <b>Дашборд</b> — состояние счёта и управление",
    ]
    return "\n".join(lines), (data or {})


def info_text(data: dict | None = None) -> tuple[str, dict]:
    """Справка: что умеет бот и что означают уведомления."""
    if data is None:
        data = load_state()
    lines = [
        "ℹ️ <b>Информация</b>",
        "",
        "<b>Режимы</b>",
        "🧪 <b>Тест</b> — бумага на живых ценах, деньги не участвуют",
        "🎮 <b>Демо</b> — то же, но профиль счёта отдельный",
        "🔴 <b>Бой</b> — реальные ордера на Bybit (только из веб-панели)",
        "",
        "<b>Уведомления о сделках</b>",
        "🟢 / 🔴 — вход в позицию (покупка / продажа)",
        "🟩 — часть позиции закрыта в плюс",
        "🟥 — закрыта в минус",
        "⬜ — закрыта в ноль (безубыток)",
        "",
        "<b>Как читать вход</b>",
        "R:R — во сколько раз цель дальше стопа.",
        "R:R 2.0 значит: рискуем 1, целимся в 2.",
        "Позиция закрывается частями по TP1/TP2/TP3,",
        "стоп подтягивается за ценой.",
        "",
        "<b>Команды</b>",
        "/start — это меню",
        "/status — дашборд",
        "/trades — история сделок",
    ]
    return "\n".join(lines), (data or {})


def bot_username() -> str:
    """@имя бота — нужно для ссылки-приглашения t.me/<bot>?start=<код>."""
    global _BOT_USERNAME
    if _BOT_USERNAME:
        return _BOT_USERNAME
    try:
        me = tg_call("getMe")
        _BOT_USERNAME = ((me.get("result") or {}).get("username") or "").strip()
    except (URLError, HTTPError, RuntimeError) as e:
        log.warning("getMe: %s", e)
        _BOT_USERNAME = ""
    return _BOT_USERNAME


def access_text(data: dict | None = None) -> tuple[str, dict]:
    """Кто имеет доступ к боту. Только для владельца."""
    lines = ["🔑 <b>Доступ</b>", ""]
    gs = tg_access.guests()
    if not gs:
        lines.append("Приглашённых пока нет.")
    else:
        for i, g in enumerate(gs, 1):
            tag = "🎛 запуск тест/демо" if g["role"] == "trader" else "👀 только просмотр"
            who = ("@" + g["name"]) if g["name"] else f"id {g['id']}"
            lines.append(f"{i}. <b>{who}</b> — {tag}")
    lines += [
        "",
        "Новая ссылка — одноразовая, живёт сутки.",
        "🔴 Боевой режим (LIVE) приглашённым недоступен —",
        "включить реальную торговлю можете только вы.",
    ]
    return "\n".join(lines), (data or {})


def access_manage_kb(gs: list[dict]) -> str:
    """Кнопка на каждого гостя — под ним отдельно откроется управление."""
    rows: list[list[tuple[str, str]]] = []
    for i, g in enumerate(gs, 1):
        who = ("@" + g["name"]) if g["name"] else f"id {g['id']}"
        rows.append([(f"{i}. {who}", f"guest:{g['id']}")])
    rows.append([("👀 Ссылка · только просмотр", "invite:viewer")])
    rows.append([("🎛 Ссылка · тест/демо", "invite:trader")])
    rows.append([("⬅️ Назад", "view:settings")])
    return kb(rows)


def guest_card_text(uid: int) -> str | None:
    who = next((g for g in tg_access.guests() if g["id"] == uid), None)
    if who is None:
        return None
    tag = "🎛 запуск тест/демо" if who["role"] == "trader" else "👀 только просмотр"
    name = ("@" + who["name"]) if who["name"] else f"id {who['id']}"
    return f"<b>{name}</b>\nправа: {tag}\n\nЧто сделать?"


def guest_card_kb(uid: int, role: str) -> str:
    rows: list[list[tuple[str, str]]] = []
    if role != "viewer":
        rows.append([("👀 Сделать «только просмотр»", f"guestrole:{uid}:viewer")])
    if role != "trader":
        rows.append([("🎛 Разрешить тест/демо", f"guestrole:{uid}:trader")])
    rows.append([("🗑 Забрать доступ", f"guestkick:{uid}")])
    rows.append([("⬅️ Назад", "view:access")])
    return kb(rows)


def view_content(view: str) -> tuple[str, dict]:
    if view == "access":
        return access_text()
    if view == "settings":
        return settings_text()
    if view == "trades":
        return trades_text()
    if view == "info":
        return info_text()
    if view == "dashboard":
        return dashboard_text()
    return home_text()


def nav_kb(view: str, data: dict, uid: int | None = None) -> str:
    """Клавиатура под сообщением.

    Дом — одна крупная кнопка «Дашборд» и под ней «Информация»/«Настройки».
    Управление ботом (Тест/Демо/Стоп) живёт внутри Дашборда, чтобы кнопки
    запуска не висели на стартовом экране под рукой. Запуск и остановка —
    только у владельца и приглашённых с ролью trader; у viewer этих кнопок
    просто нет (не только текст меняется, сама кнопка не выводится).
    """
    running = bot_running(data)
    trade_ok = uid is None or can_trade(uid)
    owner = uid is None or is_owner(uid)
    rows: list[list[tuple[str, str]]] = []

    if view == "home":
        rows.append([("📊  Д А Ш Б О Р Д", "view:dashboard")])
        row2 = [("ℹ️ Информация", "view:info"), ("⚙️ Настройки", "view:settings")]
        rows.append(row2)
        return kb(rows)

    if view == "dashboard":
        if trade_ok:
            if running:
                rows.append([("⏹ Остановить бота", "stop")])
            else:
                rows.append([("🧪 Тест", "run:paper"), ("🎮 Демо", "run:demo")])
        rows.append([("💼 Сделки", "view:trades"),
                     ("🔄 Обновить", "view:dashboard")])
        rows.append([("⬅️ Назад", "view:home")])
        return kb(rows)

    if view == "settings":
        if trade_ok:
            if running:
                rows.append([("⏹ Остановить бота", "stop")])
            else:
                rows.append([("🧪 Запустить тест", "run:paper"),
                             ("🎮 Запустить демо", "run:demo")])
        if owner:
            rows.append([("🔑 Доступ для других", "view:access")])
        rows.append([("🔄 Обновить", "view:settings")])
        rows.append([("⬅️ Назад", "view:home")])
        return kb(rows)

    if view == "access":
        return access_manage_kb(tg_access.guests())

    if view == "trades":
        rows.append([("🔄 Обновить", "view:trades"),
                     ("📊 Дашборд", "view:dashboard")])
        rows.append([("⬅️ Назад", "view:home")])
        return kb(rows)

    # info и всё остальное
    rows.append([("📊 Дашборд", "view:dashboard"),
                 ("⚙️ Настройки", "view:settings")])
    rows.append([("⬅️ Назад", "view:home")])
    return kb(rows)


def send_view(chat_id: int, view: str = "dashboard", message_id: int | None = None) -> None:
    if view == "access" and not is_owner(chat_id):
        view = "settings"  # доступ к выдаче приглашений — только владельцу
    set_view(chat_id, view)
    text, data = view_content(view)
    # В приватном чате с ботом chat_id == id пользователя.
    markup = nav_kb(view, data, uid=chat_id)
    if message_id:
        edit(chat_id, message_id, text, markup)
    else:
        send(chat_id, text, markup)


def refresh_view(chat_id: int, message_id: int) -> None:
    send_view(chat_id, get_view(chat_id), message_id)


# ——— обработка ———

def handle_message(msg: dict) -> None:
    chat_id = msg.get("chat", {}).get("id")
    uid = msg.get("from", {}).get("id")
    raw_text = (msg.get("text") or "").strip()
    text = raw_text.lower()
    if chat_id is None:
        return

    # /start <код> — активация приглашения. Проверяем ДО доступа: смысл
    # ссылки в том, что её открывает тот, у кого доступа ещё нет.
    if text.startswith("/start ") and len(raw_text.split()) > 1:
        code = raw_text.split(maxsplit=1)[1].strip()
        if not is_owner(uid) and tg_access.role_of(uid) is None:
            who = msg.get("from") or {}
            name = (who.get("username") or who.get("first_name") or "").strip()
            role = tg_access.redeem(code, uid, name)
            if role:
                log.info("приглашение активировано: id=%s role=%s", uid, role)
                send(chat_id,
                     "✅ <b>Доступ выдан</b>\n\n"
                     + ("Вы можете смотреть состояние счёта, сделки и получать "
                        "уведомления.\nЗапуск торговли недоступен."
                        if role == "viewer" else
                        "Вы можете смотреть состояние счёта и запускать "
                        "тест/демо.\nБоевой режим доступен только владельцу."))
                send_view(chat_id, "home")
                return
            send(chat_id, "🔒 Ссылка недействительна или уже использована.")
            return

    if uid not in allowed_ids():
        send(chat_id, "🔒 нет доступа к этой панели")
        log.warning("отклонён Telegram id %s", uid)
        return
    if text.startswith("/help") or text.startswith("/info"):
        send_view(chat_id, "info")
    elif text.startswith("/settings") or text.startswith("/set"):
        send_view(chat_id, "settings")
    elif text.startswith("/trades") or text.startswith("/deals") or text.startswith("/positions"):
        send_view(chat_id, "trades")
    elif text.startswith("/status") or text.startswith("/dash"):
        send_view(chat_id, "dashboard")
    else:
        # /start и всё непонятное — на стартовый экран
        send_view(chat_id, "home")


def handle_callback(cb: dict) -> None:
    uid = cb.get("from", {}).get("id")
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    data = cb.get("data") or ""
    cb_id = cb.get("id")
    if uid not in allowed_ids():
        answer_cb(cb_id, "нет доступа")
        return
    if chat_id is None or message_id is None:
        answer_cb(cb_id)
        return

    if data.startswith("view:"):
        view = data.split(":", 1)[1]
        if view not in ("home", "dashboard", "settings", "trades", "info", "access"):
            view = "home"
        if view == "access" and not is_owner(uid):
            answer_cb(cb_id, "доступно только владельцу")
            return
        send_view(chat_id, view, message_id)
        answer_cb(cb_id)
        return

    if data.startswith("invite:"):
        if not is_owner(uid):
            answer_cb(cb_id, "доступно только владельцу")
            return
        role = data.split(":", 1)[1]
        if role not in ("viewer", "trader"):
            answer_cb(cb_id, "неизвестная роль")
            return
        code = tg_access.create_invite(role, by=uid)
        uname = bot_username()
        link = f"https://t.me/{uname}?start={code}" if uname else f"код: {code}"
        what = "только просмотр" if role == "viewer" else "запуск тест/демо, без LIVE"
        send(chat_id,
             f"🔗 <b>Ссылка готова</b> ({what})\n\n{link}\n\n"
             "Разовая, действует 24 часа. Перешлите тому, кому доверяете —"
             " по ней сразу получит доступ к боту.")
        answer_cb(cb_id, "ссылка создана")
        return

    if data.startswith("guest:"):
        if not is_owner(uid):
            answer_cb(cb_id, "доступно только владельцу")
            return
        gid = int(data.split(":", 1)[1])
        who = next((g for g in tg_access.guests() if g["id"] == gid), None)
        if who is None:
            answer_cb(cb_id, "уже нет в списке")
            send_view(chat_id, "access", message_id)
            return
        edit(chat_id, message_id, guest_card_text(gid), guest_card_kb(gid, who["role"]))
        answer_cb(cb_id)
        return

    if data.startswith("guestrole:"):
        if not is_owner(uid):
            answer_cb(cb_id, "доступно только владельцу")
            return
        _, gid_s, role = data.split(":", 2)
        gid = int(gid_s)
        if tg_access.set_role(gid, role):
            answer_cb(cb_id, "права изменены")
        else:
            answer_cb(cb_id, "не найден")
        edit(chat_id, message_id, guest_card_text(gid) or "Гость отозван.",
             guest_card_kb(gid, role) if guest_card_text(gid) else access_manage_kb(tg_access.guests()))
        return

    if data.startswith("guestkick:"):
        if not is_owner(uid):
            answer_cb(cb_id, "доступно только владельцу")
            return
        gid = int(data.split(":", 1)[1])
        tg_access.revoke(gid)
        answer_cb(cb_id, "доступ забран")
        send_view(chat_id, "access", message_id)
        return

    if data == "refresh":
        refresh_view(chat_id, message_id)
        answer_cb(cb_id, "обновлено")
        return

    if data.startswith("run:"):
        if not can_trade(uid):
            answer_cb(cb_id, "нет прав на запуск")
            return
        mode = data.split(":", 1)[1]
        if mode not in ("paper", "demo"):
            answer_cb(cb_id, "недоступно")
            return
        r = admin_post("/api/run", mode=mode)
        if r.get("ok"):
            answer_cb(cb_id, "запущено · " + MODE_RU.get(mode, mode))
        else:
            answer_cb(cb_id, "ошибка")
            send(chat_id, "⚠️ не запустил: " + (r.get("error") or "неизвестная ошибка"))
        send_view(chat_id, "dashboard", message_id)
        return

    if data == "stop":
        if not can_trade(uid):
            answer_cb(cb_id, "нет прав на остановку")
            return
        # Панель останавливает бота одним запросом: тест/демо снимают
        # бумажные позиции вместе с ним, боевые остаются под своими SL/TP
        # на бирже. Промежуточной «паузы» больше нет.
        r = admin_post("/api/stop", force=True)
        if not r.get("ok"):
            answer_cb(cb_id, "ошибка")
            send(chat_id, "⚠️ не остановил: " + (r.get("error") or "неизвестная ошибка"))
        else:
            dropped = r.get("dropped") or 0
            answer_cb(cb_id, f"остановлено · снято {dropped}" if dropped else "остановлено")
        send_view(chat_id, "dashboard", message_id)
        return

    if data == "stop:force":
        if not can_trade(uid):
            answer_cb(cb_id, "нет прав на остановку")
            return
        r = admin_post("/api/stop", force=True)
        answer_cb(cb_id, "остановлено" if r.get("ok") else "ошибка")
        if not r.get("ok"):
            send(chat_id, "⚠️ не остановил: " + (r.get("error") or "неизвестная ошибка"))
        else:
            send_view(chat_id, "dashboard")
        return

    if data.startswith("login:"):
        from neft.core.tg_login import decide
        parts = data.split(":", 2)
        if len(parts) != 3:
            answer_cb(cb_id)
            return
        action, cid = parts[1], parts[2]
        ok, msg = decide(cid, action == "ok", uid)
        if not ok:
            answer_cb(cb_id, msg[:180])
            return
        if action == "ok":
            answer_cb(cb_id, "вход разрешён")
            edit(chat_id, message_id,
                 "✅ <b>Вход в панель разрешён</b>\nМожно вернуться в браузер.",
                 None)
        else:
            answer_cb(cb_id, "отклонено")
            edit(chat_id, message_id, "❌ Вход в панель отклонён.", None)
        return

    answer_cb(cb_id)


def main() -> None:
    if not settings.telegram_bot_token:
        print("нет TELEGRAM_BOT_TOKEN в .env — телеграм-бот не запущен")
        raise SystemExit(1)
    ids = allowed_ids()
    if not ids:
        print("нет TELEGRAM_ALLOWED_IDS в .env — некому отвечать, бот не запущен")
        raise SystemExit(1)
    print("NEFT телеграм-бот запущен, разрешённые id:", ids)
    offset = 0
    while True:
        try:
            r = tg_call("getUpdates", offset=offset, timeout=25,
                        allowed_updates=json.dumps(["message", "callback_query"]))
        except (URLError, HTTPError) as e:
            log.warning("getUpdates: %s — жду 5с", e)
            time.sleep(5)
            continue
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            try:
                if upd.get("message"):
                    handle_message(upd["message"])
                elif upd.get("callback_query"):
                    handle_callback(upd["callback_query"])
            except Exception as e:  # noqa: BLE001
                log.exception("update handling: %s", e)


if __name__ == "__main__":
    main()
