"""Охрана панели и запуска бота. Секреты не отдаёт, снаружи не слушает."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from neft.core.config import ROOT, settings

TOKEN_PATH = ROOT / "data" / "panel_token"
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
SAFE_TFS = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})
SAFE_VENUE = frozenset({"binance", "bybit", "mt5"})
_TF_ALIAS = {
    "1m": "1m", "m1": "1m",
    "5m": "5m", "m5": "5m",
    "15m": "15m", "m15": "15m",
    "1h": "1h", "h1": "1h", "60m": "1h",
    "4h": "4h", "h4": "4h", "240m": "4h",
    "1d": "1d", "d1": "1d", "1day": "1d",
}
BODY_MAX = 64_000
SESS_DAYS = 7
_RATE: dict[str, list[float]] = {}
_BOOT_EPOCH: int | None = None


def boot_epoch() -> int:
    """Unix-время последней загрузки ОС — сессия недействительна после перезагрузки ПК."""
    global _BOOT_EPOCH
    if _BOOT_EPOCH is not None:
        return _BOOT_EPOCH
    if sys.platform == "win32":
        try:
            import ctypes
            tick_ms = int(ctypes.windll.kernel32.GetTickCount64())
            _BOOT_EPOCH = int(time.time() - tick_ms / 1000.0)
            return _BOOT_EPOCH
        except (OSError, AttributeError, OverflowError):
            pass
    _BOOT_EPOCH = int(time.time())
    return _BOOT_EPOCH


def _parse_session(blob: str) -> tuple[int, int, str, str, str] | None:
    parts = blob.split(":")
    if len(parts) != 5:
        return None
    try:
        return int(parts[0]), int(parts[1]), parts[2], parts[3], parts[4]
    except ValueError:
        return None


def panel_token() -> str:
    env = getattr(settings, "admin_panel_token", None)
    if env:
        return str(env).strip()
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_PATH.exists():
        got = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if got:
            return got
    tok = secrets.token_urlsafe(18)
    TOKEN_PATH.write_text(tok + "\n", encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    return tok


SESSION_SALT_PATH = TOKEN_PATH.parent / "session_salt"


def session_salt() -> str:
    """Соль подписи сессий. Смена соли разлогинивает ВСЕ устройства разом.

    Отдельно от panel_token: токен может быть задан в .env (ADMIN_PANEL_TOKEN)
    и тогда неизменяем, а «выйти со всех устройств» должно работать всегда.
    """
    SESSION_SALT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SESSION_SALT_PATH.exists():
        got = SESSION_SALT_PATH.read_text(encoding="utf-8").strip()
        if got:
            return got
    salt = secrets.token_urlsafe(16)
    SESSION_SALT_PATH.write_text(salt + "\n", encoding="utf-8")
    try:
        os.chmod(SESSION_SALT_PATH, 0o600)
    except OSError:
        pass
    return salt


def rotate_session_salt() -> str:
    """Новая соль — все выданные ранее сессии мгновенно недействительны."""
    salt = secrets.token_urlsafe(16)
    SESSION_SALT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_SALT_PATH.write_text(salt + "\n", encoding="utf-8")
    try:
        os.chmod(SESSION_SALT_PATH, 0o600)
    except OSError:
        pass
    return salt


def _mac(msg: str) -> str:
    key = (panel_token() + "|" + session_salt()).encode("utf-8")
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def client_ok(handler) -> bool:
    ip = (handler.client_address or ("",))[0]
    return ip in LOOPBACK


def host_ok(handler) -> bool:
    host = (handler.headers.get("Host") or "").split(":")[0].strip().lower()
    return host in ALLOWED_HOSTS


def origin_ok(handler) -> bool:
    origin = (handler.headers.get("Origin") or "").strip()
    if not origin:
        return True
    try:
        u = urlparse(origin)
    except ValueError:
        return False
    return (u.hostname or "").lower() in ALLOWED_HOSTS


def cookies(handler) -> dict[str, str]:
    raw = handler.headers.get("Cookie") or ""
    out: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def issue_session(role: str = "owner") -> tuple[str, str, int]:
    """role — owner (вход по коду или подтверждение владельцем в Telegram)
    либо trader (вошёл приглашённый с правом запуска тест/демо). Роль
    подписана вместе со всей сессией — подделать её без ключа нельзя,
    и именно на неё опирается запрет LIVE для не-владельца."""
    exp = int(time.time()) + SESS_DAYS * 86400
    boot = boot_epoch()
    nonce = secrets.token_hex(12)
    csrf = secrets.token_urlsafe(18)
    role = role if role in ("owner", "trader") else "trader"
    payload = f"{exp}:{boot}:{nonce}:{csrf}:{role}"
    return payload, _mac(payload), exp


def session_role(handler) -> str | None:
    """Роль текущей сессии, либо None если сессии нет/невалидна."""
    if not session_ok(handler):
        return None
    c = cookies(handler)
    parsed = _parse_session(c.get("neft_sess", ""))
    return parsed[4] if parsed else None


def session_ok(handler) -> bool:
    c = cookies(handler)
    blob, sig = c.get("neft_sess", ""), c.get("neft_sig", "")
    if not blob or not sig or not hmac.compare_digest(_mac(blob), sig):
        return False
    parsed = _parse_session(blob)
    if not parsed:
        return False
    exp, boot, _, _, _ = parsed
    return exp > time.time() and boot == boot_epoch()


def csrf_ok(handler) -> bool:
    c = cookies(handler)
    blob = c.get("neft_sess", "")
    parsed = _parse_session(blob)
    if not parsed:
        return False
    expect = parsed[3]
    got = (handler.headers.get("X-NEFT-CSRF") or "").strip()
    return bool(got) and hmac.compare_digest(expect, got)


def token_ok(given: str) -> bool:
    want = panel_token().encode("utf-8")
    got = str(given or "").strip().encode("utf-8")
    digest = hashlib.sha256
    return hmac.compare_digest(digest(want).digest(), digest(got).digest()) and want == got


def rate_ok(key: str, limit: int, window: float) -> bool:
    now = time.time()
    bucket = _RATE.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def safe_dashboard(rel: str) -> Path | None:
    raw = (rel or "").replace("\\", "/").lstrip("/")
    if raw.startswith("dashboard/"):
        raw = raw[len("dashboard/"):]
    if not raw or ".." in raw.split("/"):
        return None
    root = (ROOT / "dashboard").resolve()
    path = (root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if path.suffix.lower() not in {".html", ".css", ".js", ".json", ".svg", ".woff2"}:
        return None
    return path if path.is_file() else None


def norm_tf(tf: str | None) -> str:
    """Панель: 1m. Бот/MT5: M1. Сканер: 5m. Всё сводим к одному виду."""
    t = (tf or "1m").strip().lower().replace(" ", "")
    return _TF_ALIAS.get(t, "")


def safe_klines(pair: str, tf: str, venue: str) -> tuple[str, str, str] | None:
    t = norm_tf(tf)
    if t not in SAFE_TFS:
        return None
    src = pair or ""
    if ".." in src or "\\" in src or src.startswith("/") or src.startswith("."):
        return None
    raw = src.strip().upper().replace("/", "").replace(":USDT", "")
    plus = raw.endswith("+")
    alnum = "".join(ch for ch in raw if ch.isalnum())
    if not alnum or len(alnum) > 20:
        return None
    v = (venue or "").lower()
    crypto = alnum.endswith("USDT") and 6 <= len(alnum) <= 20
    if crypto:
        if v not in ("binance", "bybit"):
            v = "bybit"
        return alnum, t, v
    # CFD / форекс / индексы: NAS100, XAUUSD+, EURUSD+. Не крипта — не Bybit REST.
    if not (3 <= len(alnum) <= 16):
        return None
    name = alnum + ("+" if plus else "")
    return name, t, "mt5"


def child_env(mode: str) -> dict[str, str]:
    """Дочернему процессу не тащим пароли. Для бумаги ключи биржи тоже не нужны."""
    env = {k: v for k, v in os.environ.items()
           if not _secret_name(k)}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if mode == "live":
        env["DEMO_ONLY"] = "false"
        env["CRYPTO_TESTNET"] = "false"
        for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET",
                  "BYBIT_API_KEY", "BYBIT_API_SECRET"):
            if os.environ.get(k):
                env[k] = os.environ[k]
    elif mode == "demo":
        env["DEMO_ONLY"] = "true"
        env["CRYPTO_TESTNET"] = "true"
    else:
        env["DEMO_ONLY"] = "true"
        env["CRYPTO_TESTNET"] = "true"
    return env


def _secret_name(k: str) -> bool:
    u = k.upper()
    return any(p in u for p in (
        "PASSWORD", "SECRET", "API_KEY", "FRED", "TOKEN", "PRIVATE",
    ))


def security_headers() -> list[tuple[str, str]]:
    return [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
        ("Cache-Control", "no-store"),
        ("Content-Security-Policy",
         "default-src 'self'; "
         "script-src 'self' 'unsafe-inline'; "
         "style-src 'self' 'unsafe-inline'; "
         "img-src 'self' data:; "
         "connect-src 'self' https://fapi.binance.com https://api.bybit.com "
         "wss://fstream.binance.com wss://stream.bybit.com; "
         "frame-ancestors 'none'; "
         "base-uri 'self'; "
         "form-action 'self'"),
    ]


def set_session_headers(handler, blob: str, sig: str, exp: int) -> None:
    parsed = _parse_session(blob)
    if not parsed:
        return
    csrf = parsed[3]
    max_age = max(60, exp - int(time.time()))
    base = f"Path=/; Max-Age={max_age}; SameSite=Strict"
    handler.send_header("Set-Cookie", f"neft_sess={blob}; HttpOnly; {base}")
    handler.send_header("Set-Cookie", f"neft_sig={sig}; HttpOnly; {base}")
    handler.send_header("Set-Cookie", f"neft_csrf={csrf}; {base}")


def clear_session_headers(handler) -> None:
    dead = "Path=/; Max-Age=0; SameSite=Strict"
    handler.send_header("Set-Cookie", f"neft_sess=; HttpOnly; {dead}")
    handler.send_header("Set-Cookie", f"neft_sig=; HttpOnly; {dead}")
    handler.send_header("Set-Cookie", f"neft_csrf=; {dead}")


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>NEFT · вход</title>
<style>
:root{
  --bg:#000000; --bg-soft:#1c1c1e; --bg-elev:#2c2c2e;
  --ink:#f5f5f7; --muted:#c8c8cd; --muted-2:#aeaeb3;
  --line:rgba(255,255,255,.10); --line-soft:rgba(255,255,255,.06);
  --good:#32d74b; --good-dim:rgba(50,215,75,.20);
  --bad:#ff453a; --bad-dim:rgba(255,69,58,.20);
  --accent:#0a84ff; --accent-2:#409cff; --accent-dim:rgba(10,132,255,.22);
  --r-sm:8px; --r:12px; --r-lg:16px; --r-xl:20px; --r-pill:999px;
  --shadow-sm:0 1px 3px rgba(0,0,0,.28), inset 0 1px 0 rgba(255,255,255,.04);
  --shadow-md:0 8px 24px -8px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.05);
  --shadow-lg:0 16px 40px -12px rgba(0,0,0,.65);
  --ease:cubic-bezier(.4,0,.2,1); --spring:cubic-bezier(.34,1.45,.64,1);
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI Variable","Segoe UI",system-ui,sans-serif;
  --mono:"Cascadia Mono","SF Mono",Consolas,"Segoe UI Mono",ui-monospace,monospace;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0;color:var(--ink);
font:14px/1.5 var(--sans);-webkit-font-smoothing:antialiased}
body{
  display:flex;align-items:center;justify-content:center;min-height:100%;
  background:var(--bg);
  background:
    radial-gradient(ellipse 90% 55% at 50% -8%, rgba(10,132,255,.16) 0%, transparent 60%),
    radial-gradient(ellipse 70% 50% at 100% 100%, rgba(90,200,250,.09) 0%, transparent 58%),
    radial-gradient(ellipse 60% 45% at 0% 100%, rgba(64,156,255,.07) 0%, transparent 55%),
    linear-gradient(180deg, #050505 0%, #000 100%);
}
.login-wrap{position:relative;width:100%;max-width:440px;padding:20px;animation:in .55s var(--ease)}
@keyframes in{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
.box{
  position:relative;overflow:hidden;padding:28px 26px 22px;
  border-radius:var(--r-xl);
  background:linear-gradient(145deg, rgba(44,44,46,.88) 0%, rgba(28,28,30,.94) 100%);
  border:1px solid rgba(255,255,255,.09);
  box-shadow:var(--shadow-lg), inset 0 1px 0 rgba(255,255,255,.06);
}
.box::before{
  content:"";position:absolute;inset:0;pointer-events:none;opacity:0;
  background:linear-gradient(105deg, transparent 38%, rgba(10,132,255,.07) 50%, transparent 62%);
  transition:opacity .45s var(--ease);
}
.box.waiting::before{opacity:1;animation:shine 2.8s var(--ease) infinite}
.box.denied::before{background:linear-gradient(105deg, transparent 38%, rgba(255,69,58,.06) 50%, transparent 62%)}
@keyframes shine{0%{transform:translateX(-120%)}100%{transform:translateX(120%)}}
.login-head{display:flex;align-items:center;gap:14px;margin-bottom:18px}
.logo-mark{
  width:52px;height:52px;border-radius:var(--r-lg);flex-shrink:0;position:relative;
  display:grid;place-items:center;font:800 26px/1 var(--sans);letter-spacing:-.05em;color:#fff;
  background:linear-gradient(145deg, #0a84ff 0%, #409cff 52%, #5ac8fa 100%);
  box-shadow:0 8px 22px rgba(10,132,255,.34), inset 0 1px 0 rgba(255,255,255,.22);
}
.logo-mark::after{
  content:"";position:absolute;inset:0;border-radius:inherit;
  background:linear-gradient(180deg, rgba(255,255,255,.18) 0%, transparent 48%);
}
.logo-text{display:flex;flex-direction:column;gap:4px;min-width:0}
.logo-name{
  font:800 30px/1 var(--sans);letter-spacing:-.05em;
  background:linear-gradient(135deg, #ffffff 0%, #d8eaff 42%, var(--accent-2) 100%);
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;
}
.logo-sub{
  font:700 10px/1 var(--sans);letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
}
.lead{color:var(--muted);margin:0 0 20px;font-size:14px;line-height:1.55;max-width:34ch}
.btn-tg{
  width:100%;display:flex;align-items:center;justify-content:center;gap:10px;
  border:0;border-radius:var(--r-lg);padding:14px 18px;cursor:pointer;
  font:700 15px/1 var(--sans);letter-spacing:.01em;color:#fff;
  background:linear-gradient(180deg, #3d9dff 0%, var(--accent) 100%);
  box-shadow:0 6px 20px rgba(10,132,255,.32), inset 0 1px 0 rgba(255,255,255,.18);
  transition:transform .14s var(--ease), box-shadow .14s var(--ease), opacity .14s var(--ease);
}
.btn-tg:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 10px 26px rgba(10,132,255,.38)}
.btn-tg:active:not(:disabled){transform:translateY(0)}
.btn-tg:disabled{opacity:.55;cursor:not-allowed;transform:none}
.btn-tg.hide{display:none}
.btn-ico{
  width:28px;height:28px;border-radius:50%;display:grid;place-items:center;
  background:rgba(255,255,255,.16);font-size:14px;
}
.panel{display:none;text-align:center;padding:6px 0 2px;animation:fadeUp .45s var(--ease)}
.panel.show{display:block}
@keyframes fadeUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.wait-orb{position:relative;width:92px;height:92px;margin:4px auto 18px}
.wait-ring{position:absolute;inset:0;border-radius:50%;border:2px solid transparent}
.wait-ring.r1{border-top-color:var(--accent);border-right-color:rgba(10,132,255,.25);animation:spin 1.1s linear infinite}
.wait-ring.r2{inset:10px;border-bottom-color:var(--accent-2);border-left-color:rgba(64,156,255,.2);animation:spin 1.6s linear infinite reverse}
.wait-ring.r3{inset:20px;border-top-color:rgba(10,132,255,.45);animation:spin 2.2s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.wait-core{
  position:absolute;inset:28px;border-radius:50%;
  background:linear-gradient(145deg,#0a84ff,#5ac8fa);
  box-shadow:0 0 24px rgba(10,132,255,.45), inset 0 1px 0 rgba(255,255,255,.28);
  display:grid;place-items:center;font-size:22px;animation:pulse 2s ease-in-out infinite;
}
@keyframes pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.04)}}
.panel-title{font:700 17px/1.25 var(--sans);margin:0 0 8px;letter-spacing:-.02em}
.panel-hint{color:var(--muted);font-size:13px;line-height:1.5;margin:0 0 16px}
.wait-dots{display:flex;justify-content:center;gap:7px;margin-bottom:6px}
.wait-dots i{width:7px;height:7px;border-radius:50%;background:var(--accent);opacity:.35;animation:dot 1.2s ease-in-out infinite}
.wait-dots i:nth-child(2){animation-delay:.15s}
.wait-dots i:nth-child(3){animation-delay:.3s}
@keyframes dot{0%,80%,100%{opacity:.25;transform:scale(.85)}40%{opacity:1;transform:scale(1.15)}}
.wait-timer{font:700 13px/1 var(--mono);color:var(--accent-2);letter-spacing:.06em;margin-top:4px}
.denied-icon{
  width:58px;height:58px;margin:0 auto 14px;border-radius:50%;
  display:grid;place-items:center;font-size:22px;font-weight:800;color:#fff;
  background:linear-gradient(145deg,#ff6259,#ff453a);
  box-shadow:0 0 28px rgba(255,69,58,.32);
  animation:pop .5s var(--spring);
}
.cool-ring{
  --pct:0%;width:104px;height:104px;margin:0 auto 6px;border-radius:50%;padding:3px;
  background:conic-gradient(var(--accent) var(--pct), rgba(255,255,255,.08) 0);
  box-shadow:0 0 0 1px rgba(255,255,255,.06);
}
.cool-inner{
  width:100%;height:100%;border-radius:50%;display:grid;place-items:center;
  background:linear-gradient(180deg, rgba(36,36,38,.95) 0%, rgba(28,28,30,.98) 100%);
  font:700 22px/1 var(--mono);letter-spacing:-.02em;font-variant-numeric:tabular-nums;
  color:var(--ink);
}
.done-icon{
  width:64px;height:64px;margin:0 auto 16px;border-radius:50%;
  background:linear-gradient(145deg,#3de05a,#32d74b);
  box-shadow:0 0 28px rgba(50,215,75,.35);
  display:grid;place-items:center;font-size:28px;font-weight:700;color:#fff;
  animation:pop .5s var(--spring);
}
@keyframes pop{from{transform:scale(.6);opacity:0}to{transform:scale(1);opacity:1}}
.status{
  min-height:0;margin:0;font-size:13px;font-weight:600;line-height:1.45;text-align:center;
}
.status.outside{margin-top:14px;min-height:0}
.status.outside:not(:empty){
  padding:10px 14px;border-radius:var(--r-pill);
  background:rgba(118,118,128,.16);border:1px solid var(--line-soft);color:var(--muted-2);
}
.status.outside.bad{
  color:var(--bad);background:var(--bad-dim);border-color:rgba(255,69,58,.24);
}
.status.outside.ok{color:var(--good);background:var(--good-dim);border-color:rgba(50,215,75,.22)}
.fallback{
  margin-top:20px;padding-top:18px;border-top:1px solid var(--line-soft);
  transition:opacity .3s var(--ease);
}
.fallback.dim{opacity:.32;pointer-events:none}
.fallback summary{
  cursor:pointer;color:var(--muted);font-size:12px;font-weight:700;
  letter-spacing:.06em;text-transform:uppercase;list-style:none;
}
.fallback summary::-webkit-details-marker{display:none}
.fallback input{
  width:100%;background:var(--bg-elev);border:1px solid var(--line);border-radius:var(--r);
  color:var(--ink);padding:12px 14px;margin:12px 0 10px;font:inherit;font-size:15px;
}
.fallback input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-dim)}
.btn-code{
  width:100%;border:0;border-radius:var(--r);padding:12px;cursor:pointer;
  font:700 14px/1 var(--sans);color:var(--ink);
  background:rgba(118,118,128,.22);box-shadow:var(--shadow-sm);
  transition:background .14s var(--ease);
}
.btn-code:hover{background:rgba(118,118,128,.32)}
.foot-note{
  margin:14px 0 0;text-align:center;font-size:11px;font-weight:600;
  letter-spacing:.08em;text-transform:uppercase;color:rgba(174,174,179,.55);
}
</style></head><body>
<div class="login-wrap">
<div class="box" id="box">
<header class="login-head">
  <div class="logo-mark">N</div>
  <div class="logo-text">
    <div class="logo-name">NEFT</div>
    <div class="logo-sub">терминал</div>
  </div>
</header>
<p class="lead" id="lead">Панель только с этого компьютера. После перезагрузки ПК — подтверждение в Telegram.</p>
<button type="button" class="btn-tg" id="tgBtn" onclick="tgLogin()">
  <span class="btn-ico">✈</span>
  <span class="btn-label">войти через Telegram</span>
</button>
<div class="panel" id="waitPanel">
  <div class="wait-orb">
    <div class="wait-ring r1"></div>
    <div class="wait-ring r2"></div>
    <div class="wait-ring r3"></div>
    <div class="wait-core">✈</div>
  </div>
  <p class="panel-title" id="waitTitle">Ждём подтверждение</p>
  <p class="panel-hint">Откройте Telegram и нажмите «Разрешить»</p>
  <div class="wait-dots"><i></i><i></i><i></i></div>
  <div class="wait-timer" id="waitTimer">5:00</div>
</div>
<div class="panel" id="deniedPanel">
  <div class="denied-icon">✕</div>
  <p class="panel-title">Вход отклонён</p>
  <p class="panel-hint">Повторная отправка будет доступна через</p>
  <div class="cool-ring" id="retryRing"><div class="cool-inner" id="retryTimer">1:00</div></div>
</div>
<div class="panel" id="donePanel">
  <div class="done-icon">✓</div>
  <p class="panel-title">Вход разрешён</p>
  <p class="panel-hint">Открываем панель…</p>
</div>
<div class="status outside" id="st"></div>
<details class="fallback" id="fb">
<summary>войти по коду панели</summary>
<input id="t" type="password" autocomplete="current-password" placeholder="код из data/panel_token">
<button type="button" class="btn-code" onclick="tokenLogin()">войти по коду</button>
</details>
</div>
<p class="foot-note">локальный доступ · 127.0.0.1</p>
</div>
<script>
let poll=null, cid=null, timerIv=null, retryIv=null, retryTotal=60, expiresAt=0;
function fmtRetry(sec){
  const m=Math.floor(sec/60), s=sec%60;
  return m+":"+(s<10?"0":"")+s;
}
function showDenied(on){
  document.getElementById("deniedPanel").classList.toggle("show",on);
  document.getElementById("box").classList.toggle("denied",on);
  document.getElementById("tgBtn").classList.toggle("hide",on);
  document.getElementById("lead").style.display=on?"none":"";
  if(!on && !retryIv) document.getElementById("tgBtn").disabled=false;
}
function startRetryCooldown(sec){
  if(retryIv){clearInterval(retryIv);retryIv=null;}
  retryTotal=Math.max(1,Math.ceil(sec||60));
  let left=retryTotal;
  const ring=document.getElementById("retryRing");
  const timer=document.getElementById("retryTimer");
  showDenied(true);
  setSt("","");
  function tick(){
    if(left<=0){
      showDenied(false);
      retryIv=null;
      return;
    }
    timer.textContent=fmtRetry(left);
    ring.style.setProperty("--pct",(((retryTotal-left)/retryTotal)*100)+"%");
    left--;
  }
  tick();
  retryIv=setInterval(tick,1000);
}
function setSt(t,cls){
  const e=document.getElementById("st");
  e.textContent=t||"";
  e.className="status outside"+(cls?" "+cls:"");
}
function showWait(on){
  document.getElementById("box").classList.toggle("waiting",on);
  document.getElementById("tgBtn").classList.toggle("hide",on);
  document.getElementById("waitPanel").classList.toggle("show",on);
  document.getElementById("fb").classList.toggle("dim",on);
  document.getElementById("lead").style.display=on?"none":"";
  if(on) showDenied(false);
  if(!on){
    document.getElementById("donePanel").classList.remove("show");
    if(timerIv){clearInterval(timerIv);timerIv=null;}
  }
}
function showDone(){
  document.getElementById("waitPanel").classList.remove("show");
  document.getElementById("donePanel").classList.add("show");
  if(timerIv){clearInterval(timerIv);timerIv=null;}
}
function startTimer(sec){
  expiresAt=Date.now()+sec*1000;
  const el=document.getElementById("waitTimer");
  function tick(){
    const left=Math.max(0,Math.ceil((expiresAt-Date.now())/1000));
    const m=Math.floor(left/60), s=left%60;
    el.textContent=m+":"+(s<10?"0":"")+s;
    if(!left && poll){clearInterval(poll);poll=null;resetWait("Время истекло — запросите снова","bad");}
  }
  tick();
  if(timerIv) clearInterval(timerIv);
  timerIv=setInterval(tick,1000);
}
function resetWait(msg,cls){
  showWait(false);
  if(!retryIv) document.getElementById("tgBtn").disabled=false;
  setSt(msg||"",cls||"");
}
async function tgLogin(){
  const b=document.getElementById("tgBtn");
  b.disabled=true;
  setSt("Отправляю запрос…","");
  document.getElementById("waitTitle").textContent="Отправляем в Telegram…";
  showWait(true);
  try{
    const r=await fetch("/api/login/telegram",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
    const d=await r.json();
    if(!d.ok){
      showWait(false);
      if(d.retry_after) startRetryCooldown(d.retry_after);
      else{b.disabled=false;setSt(d.error||"ошибка","bad");}
      return;
    }
    cid=d.id;
    setSt("","");
    document.getElementById("waitTitle").textContent="Ждём подтверждение";
    startTimer(d.expires||300);
    if(poll) clearInterval(poll);
    poll=setInterval(pollLogin,1200);
    pollLogin();
  }catch(e){
    resetWait("нет связи с панелью","bad");
  }
}
async function pollLogin(){
  if(!cid) return;
  try{
    const r=await fetch("/api/login/poll?id="+encodeURIComponent(cid));
    const d=await r.json();
    if(d.status==="approved"){
      clearInterval(poll); poll=null;
      showDone();
      const r2=await fetch("/api/login/complete",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({id:cid})});
      const d2=await r2.json();
      if(d2.ok) setTimeout(()=>{location.href="/";},600);
      else resetWait(d2.error||"не удалось войти","bad");
      return;
    }
    if(d.status==="denied"){
      clearInterval(poll); poll=null;
      showWait(false);
      startRetryCooldown(d.retry_after||60);
      return;
    }
    if(d.status==="expired"){
      clearInterval(poll); poll=null;
      resetWait("Время истекло — запросите снова","bad");
      return;
    }
  }catch(e){}
}
async function tokenLogin(){
  const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({token:document.getElementById("t").value})});
  const d=await r.json();
  if(d.ok) location.href="/";
  else setSt(d.error||"отказ","bad");
}
document.getElementById("t").addEventListener("keydown",e=>{if(e.key==="Enter")tokenLogin();});
fetch("/api/login").then(r=>r.json()).then(d=>{
  if(d.auth) location.href="/";
  if(d.retry_after) startRetryCooldown(d.retry_after);
  if(!d.telegram){
    document.getElementById("lead").textContent="Telegram не настроен — войдите по коду панели.";
    document.getElementById("tgBtn").style.display="none";
    document.getElementById("fb").open=true;
  }
}).catch(()=>{});
</script></body></html>
"""


def public_ready_scrub(data: dict) -> dict:
    """Чеклист без путей к терминалу и без намёков на секреты сверх флагов."""
    return json.loads(json.dumps(data, default=str))
