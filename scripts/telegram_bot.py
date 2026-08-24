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

def allowed_ids() -> set[int]:
    raw = " ,".join(x for x in (settings.telegram_allowed_ids, settings.telegram_chat_id) if x)
    out = set()
    for chunk in raw.replace(" ", "").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.add(int(chunk))
    return out


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


def set_view(chat_id: int, view: str) -> None:
    _chat_view[chat_id] = view


def get_view(chat_id: int) -> str:
    return _chat_view.get(chat_id, "dashboard")

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
    dot = "🟢" if mode in ("demo", "live") else "⚪"
    lines = [
        f"📊 <b>Дашборд</b>",
        f"{dot} режим: <b>{MODE_RU.get(mode, mode)}</b>",
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
    lines = [
        "⚙️ <b>Настройки</b>",
        "",
        f"режим: <b>{MODE_RU.get(mode, mode or 'выключен')}</b>",
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


def view_content(view: str) -> tuple[str, dict]:
    if view == "settings":
        return settings_text()
    if view == "trades":
        return trades_text()
    return dashboard_text()


def nav_kb(view: str, data: dict) -> str:
    rows = [[
        ("📊 Дашборд" + (" ✅" if view == "dashboard" else ""), "view:dashboard"),
        ("⚙️ Настройки" + (" ✅" if view == "settings" else ""), "view:settings"),
        ("💼 Сделки" + (" ✅" if view == "trades" else ""), "view:trades"),
    ]]
    mode = (data or {}).get("mode") or ""
    if view == "settings":
        rows.append([
            ("🧪 Тест" + (" ✅" if mode == "paper" else ""), "run:paper"),
            ("🎮 Демо" + (" ✅" if mode == "demo" else ""), "run:demo"),
        ])
        if mode in ("paper", "demo", "live"):
            rows.append([("⏹ Стоп", "stop")])
    elif view == "dashboard" and mode in ("paper", "demo", "live"):
        rows.append([("⏹ Стоп", "stop")])
    rows.append([("🔄 Обновить", f"view:{view}")])
    return kb(rows)


def send_view(chat_id: int, view: str = "dashboard", message_id: int | None = None) -> None:
    set_view(chat_id, view)
    text, data = view_content(view)
    markup = nav_kb(view, data)
    if message_id:
        edit(chat_id, message_id, text, markup)
    else:
        send(chat_id, text, markup)


def refresh_view(chat_id: int, message_id: int) -> None:
    send_view(chat_id, get_view(chat_id), message_id)


HELP = (
    "<b>NEFT · телеграм</b>\n\n"
    "📊 <b>Дашборд</b> — эквити, P&amp;L, открытые позиции\n"
    "⚙️ <b>Настройки</b> — риск, плечо, запуск Тест/Демо/Стоп\n"
    "💼 <b>Сделки</b> — история закрытых сделок\n\n"
    "/status или /start — дашборд\n"
    "Вход в веб-панель — «войти через Telegram» на экране входа."
)


# ——— обработка ———

def handle_message(msg: dict) -> None:
    chat_id = msg.get("chat", {}).get("id")
    uid = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip().lower()
    if chat_id is None:
        return
    if uid not in allowed_ids():
        send(chat_id, "🔒 нет доступа к этой панели")
        log.warning("отклонён Telegram id %s", uid)
        return
    if text.startswith("/help"):
        send(chat_id, HELP)
    elif text.startswith("/settings") or text.startswith("/set"):
        send_view(chat_id, "settings")
    elif text.startswith("/trades") or text.startswith("/deals") or text.startswith("/positions"):
        send_view(chat_id, "trades")
    else:
        send_view(chat_id, "dashboard")


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
        if view not in ("dashboard", "settings", "trades"):
            view = "dashboard"
        send_view(chat_id, view, message_id)
        answer_cb(cb_id, "обновлено")
        return

    if data == "refresh":
        refresh_view(chat_id, message_id)
        answer_cb(cb_id, "обновлено")
        return

    if data.startswith("run:"):
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
        set_view(chat_id, "settings")
        refresh_view(chat_id, message_id)
        return

    if data == "stop":
        r = admin_post("/api/stop")
        if not r.get("ok"):
            answer_cb(cb_id, "ошибка")
            send(chat_id, "⚠️ не остановил: " + (r.get("error") or "неизвестная ошибка"))
        elif r.get("paused"):
            answer_cb(cb_id, "вход заблокирован")
            n = r.get("open") or 0
            send(chat_id,
                 f"⏸ Новые входы заблокированы. Открытых позиций: <b>{n}</b> — "
                 "закроются сами по стопу/цели. Либо закрыть прямо сейчас:",
                 kb([[("❗ Закрыть сейчас", "stop:force")],
                     [("📊 Дашборд", "view:dashboard")]]))
        else:
            answer_cb(cb_id, "остановлено")
        refresh_view(chat_id, message_id)
        return

    if data == "stop:force":
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
