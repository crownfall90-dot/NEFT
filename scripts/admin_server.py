"""Локальная админ-панель NEFT: настройки, новости, запуск тест/демо/бой.

Слушает только 127.0.0.1 — снаружи не торчит. Секреты из .env не отдаёт.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

from neft.core import machine_config
from neft.core.machine_config import account_for_mode
from neft.core.live_ready import checklist as live_checklist
from neft.core.config import ROOT, settings
from neft.core.panel_guard import (
    BODY_MAX, LOGIN_HTML, child_env, clear_session_headers, client_ok,
    csrf_ok, host_ok, issue_session, origin_ok, panel_token, rate_ok,
    safe_dashboard, safe_klines, security_headers, session_ok,
    set_session_headers, token_ok,
)
from neft.core.tg_login import (
    consume as login_consume,
    create_challenge as login_create,
    deny_cooldown_left as login_cooldown_left,
    poll_challenge as login_poll,
    tg_enabled as login_tg_enabled,
)
from neft.core.crypto_feed import CHANNELS, fetch_posts
from neft.core.news import INSTRUMENT_CURRENCIES, load_calendar
from neft.core.news_historical import FOMC_MEETING_DATES, FRED_RELEASES
from neft.core.routing import (
    CRYPTO_ROUTES, CRYPTO_STARTER, CRYPTO_UNIVERSE, CRYPTO_WATCHLIST, ROUTES,
)

HOST, PORT = "127.0.0.1", 8787
PY = ROOT / ".venv" / "Scripts" / "python.exe"
ADMIN_HTML = ROOT / "dashboard" / "admin.html"
LOG_DIR = ROOT / "logs"
CONTROL = LOG_DIR / "bot_control.json"
JOBS: dict[str, dict] = {}
JOB_LOCK = threading.Lock()


def read_control() -> dict:
    if not CONTROL.exists():
        return {"entries": True, "paused": False}
    try:
        data = json.loads(CONTROL.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"entries": True, "paused": False}
    return {"entries": bool(data.get("entries", True)),
            "paused": bool(data.get("paused", False))}


def write_control(*, entries: bool, paused: bool) -> dict:
    rec = {"entries": entries, "paused": paused}
    LOG_DIR.mkdir(exist_ok=True)
    CONTROL.write_text(json.dumps(rec), encoding="utf-8")
    return rec


def state_open_count() -> int:
    path = LOG_DIR / "crypto_forward_state.json"
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    n = 0
    for rec in (data.get("symbols") or {}).values():
        if rec.get("position") or rec.get("pending"):
            n += 1
    return n

NEWS_SECTIONS = [
    {
        "id": "usd",
        "title": "США · макро и фонды",
        "role": "gate",
        "currencies": ["USD"],
        "blocks": "NAS100, DJ30, US500, золото, все USD-пары, вся крипта USDT",
        "why": "CPI, NFP, FOMC, ставки ФРС двигают доллар-ликвидность. "
               "Индексы США, XAUUSD и крипта режутся одним USD-гейтом.",
        "source": "ForexFactory thisweek (nfs.faireconomy.media)",
    },
    {
        "id": "eu",
        "title": "Еврозона · DAX / EUR",
        "role": "gate",
        "currencies": ["EUR"],
        "blocks": "GER40, FRA40, ES35, EURUSD, EURJPY, EURGBP",
        "why": "Ставки ECB, инфляция и PMI еврозоны. На NAS100 EUR-новости "
               "намеренно не вешаются — иначе резали бы полдня без причины.",
        "source": "ForexFactory thisweek",
    },
    {
        "id": "uk",
        "title": "Британия · FTSE / GBP",
        "role": "gate",
        "currencies": ["GBP"],
        "blocks": "UK100, GBPUSD, GBPJPY, EURGBP",
        "why": "BoE, UK CPI/GDP. Только стерлинговые инструменты и UK100.",
        "source": "ForexFactory thisweek",
    },
    {
        "id": "fx_other",
        "title": "Форекс · прочие валюты",
        "role": "gate",
        "currencies": ["JPY", "AUD", "CAD", "CHF", "NZD"],
        "blocks": "USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD и кроссы с этими ногами",
        "why": "Событие по валюте пары двигает котировку напрямую. "
               "На индексы США и крипту эти релизы не ставятся.",
        "source": "ForexFactory thisweek",
    },
    {
        "id": "cn",
        "title": "Китай · индекс",
        "role": "gate",
        "currencies": ["CNY"],
        "blocks": "CHINA50",
        "why": "Данные КНР плюс USD как глобальный risk-on/off.",
        "source": "ForexFactory thisweek",
    },
    {
        "id": "crypto_tg",
        "title": "Крипта · Telegram-брифинг",
        "role": "briefing",
        "currencies": [],
        "blocks": "не блокирует ордера",
        "why": "Crypto Daily, Coin Post, Dashi Finance — контекст, не календарь. "
               "У постов нет планового времени, ±5 минут вокруг них бессмысленны. "
               "Реклама отбрасывается, дубли схлопываются.",
        "source": "t.me/s/cryptodaily, t.me/s/Coin_Post, t.me/s/dashi_finance",
    },
    {
        "id": "fred",
        "title": "История США · FRED (только бэктест)",
        "role": "backtest",
        "currencies": ["USD"],
        "blocks": "ретро-проверка фильтра, не live",
        "why": "ForexFactory отдаёт только текущую неделю. На истории — FRED: "
               "CPI, NFP, GDP, PPI, PCE + фиксированные даты FOMC. Только США.",
        "source": "api.stlouisfed.org + federalreserve.gov FOMC calendar",
    },
]


def _json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


def jobs_snapshot() -> list[dict]:
    out = []
    with JOB_LOCK:
        dead = []
        for name, job in JOBS.items():
            proc: subprocess.Popen = job["proc"]
            code = proc.poll()
            if code is not None:
                dead.append(name)
                out.append({**{k: v for k, v in job.items() if k != "proc"},
                            "name": name, "running": False, "exit": code})
            else:
                out.append({**{k: v for k, v in job.items() if k != "proc"},
                            "name": name, "running": True, "pid": proc.pid})
        for n in dead:
            JOBS.pop(n, None)
    return out


def _kill_script(name: str) -> None:
    """Гасим хвосты после рестарта админки (Windows)."""
    if os.name != "nt":
        return
    # taskkill надёжнее, чем PowerShell $_ в некоторых оболочках.
    subprocess.run(
        ["taskkill", "/F", "/FI", f"WINDOWTITLE eq *{name}*", "/T"],
        capture_output=True, text=True)
    # По командной строке процесса (основной путь).
    ps = (
        "$n='" + name.replace("'", "") + "'; "
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Name -match 'python' -and $_.CommandLine -like ('*'+$n+'*') "
        "-and $_.CommandLine -notlike '*admin_server*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-Command", ps], capture_output=True, text=True)
    # На всякий случай — pid-файл бота.
    for pid_name in ("crypto_forward.pid", "forward.pid"):
        p = LOG_DIR / pid_name
        try:
            pid = int(p.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"],
                       capture_output=True, text=True)


def stop_jobs() -> None:
    with JOB_LOCK:
        procs = [job["proc"] for job in list(JOBS.values())]
        JOBS.clear()
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    # На Windows terminate() == TerminateProcess — мгновенно и без шанса на
    # graceful shutdown, ждать долго нет смысла (раньше стоял потолок 4с).
    until = time.time() + 1.5
    for proc in procs:
        while proc.poll() is None and time.time() < until:
            time.sleep(0.05)
        if proc.poll() is None:
            proc.kill()
    try:
        (LOG_DIR / "crypto_forward.pid").unlink()
    except OSError:
        pass
    # Подчистка "осиротевших" процессов (после рестарта админки JOBS пуст,
    # а старый crypto_forward.py мог остаться жив) — это 3-4 вызова
    # taskkill/PowerShell, реально ощутимая задержка (до пары секунд).
    # Клиенту это ждать незачем: ответ уже ушёл по факту terminate() выше,
    # подчистка донагоняет в фоне.
    threading.Thread(target=_kill_script, args=("crypto_forward.py",), daemon=True).start()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)
        if handle:
            k32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_bot_pid() -> tuple[int, str | None]:
    pid_path = LOG_DIR / "crypto_forward.pid"
    if not pid_path.exists():
        return 0, None
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
        if raw.startswith("{"):
            data = json.loads(raw)
            return int(data.get("pid") or 0), data.get("mode")
        return int(raw.splitlines()[0]), None
    except (ValueError, json.JSONDecodeError, OSError):
        return 0, None


def _mode_from_cmdline(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\""
                 ").CommandLine"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            line = (out.stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            line = ""
    else:
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(
                "utf-8", errors="replace").replace("\x00", " ")
            line = cmdline
        except OSError:
            line = ""
    m = re.search(r"--mode\s+(paper|demo|live)\b", line)
    return m.group(1) if m else None


def _norm_bot_mode(mode: str | None) -> str:
    return mode if mode in ("paper", "demo", "live") else "paper"


def bot_alive() -> dict | None:
    pid_path = LOG_DIR / "crypto_forward.pid"
    pid, mode = _read_bot_pid()
    if pid and _pid_alive(pid):
        mode = _norm_bot_mode(mode or _mode_from_cmdline(pid))
        return {"name": "crypto", "running": True, "pid": pid,
                "mode": mode, "inferred": True}
    if pid_path.exists():
        try:
            pid_path.unlink()
        except OSError:
            pass
    return None


def inferred_jobs(owned: list[dict]) -> list[dict]:
    running = [j for j in owned if j.get("running")]
    crypto = [j for j in running if j.get("name") == "crypto"]
    if crypto:
        return crypto + [j for j in running if j.get("name") != "crypto"]
    if running:
        return running
    extra = bot_alive()
    return [extra] if extra else []


def _flags_from_cfg(cfg: dict) -> list[str]:
    hss = cfg.get("strategies", {}).get("hss", {})
    lsr = cfg.get("strategies", {}).get("london_sr", {})
    news = cfg.get("news", {})
    sess = hss.get("session") or cfg.get("hss_session") or [16, 19]
    lon = lsr.get("london") or cfg.get("london") or [11, 16]
    ny = lsr.get("ny") or cfg.get("ny") or [16, 23]
    flags = [
        "--risk", str(cfg["risk_pct"]),
        "--balance", str(cfg["deposit"]),
        "--rr", str(hss.get("rr", cfg.get("rr", 1.0))),
        "--pullback", str(hss.get("pullback", 2)),
        "--hss-session", f"{sess[0]}-{sess[1]}",
        "--london", f"{lon[0]}-{lon[1]}",
        "--ny", f"{ny[0]}-{ny[1]}",
        "--daily", str(cfg["max_daily_loss_pct"]),
        "--dd", str(cfg["max_drawdown_pct"]),
        "--interval", str(cfg.get("interval_sec", 5)),
    ]
    if hss.get("all_day") or cfg.get("hss_24h"):
        flags.append("--hss-24h")
    if not news.get("gate", cfg.get("news_filter", True)):
        flags.append("--no-news")
    return flags


def risk_for_mode(mode: str, cfg: dict) -> tuple[float, float]:
    """Риск и дневной стоп — из профиля счёта выбранного режима."""
    acc = account_for_mode(cfg, mode)
    return (
        float(acc.get("risk_pct") or 0.5),
        float(acc.get("max_daily_loss_pct") or 4),
    )


def start_jobs(mode: str, cfg: dict) -> list[str]:
    stop_jobs()
    _kill_script("crypto_forward.py")
    write_control(entries=True, paused=False)
    env = child_env(mode)
    acc = account_for_mode(cfg, mode)

    LOG_DIR.mkdir(exist_ok=True)
    kw = dict(env=env, cwd=str(ROOT), stdout=subprocess.PIPE,
              stderr=subprocess.STDOUT)
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW

    started = []
    venue = cfg.get("venue") or "crypto"
    flags = _flags_from_cfg(cfg)
    py = str(PY)

    def spawn(name: str, args: list[str]) -> None:
        logf = (LOG_DIR / f"admin_{name}.log").open("ab")
        proc = subprocess.Popen([py, *args], **kw)
        threading.Thread(target=_pump, args=(proc, logf), daemon=True).start()
        with JOB_LOCK:
            JOBS[name] = {"proc": proc, "mode": mode, "cmd": args,
                          "started": datetime.now().isoformat(timespec="seconds")}
        started.append(name)

    risk_pct, _daily = risk_for_mode(mode, cfg)
    if venue in ("crypto", "both"):
        bal = acc.get("deposit", 1000) if mode != "live" else 1000
        cargs = ["scripts/crypto_forward.py",
                 "--mode", mode,
                 "--symbols", ",".join(cfg.get("crypto_symbols") or CRYPTO_ROUTES),
                 "--leverage", str(acc.get("leverage_crypto", 5)),
                 "--exchange", "bybit",
                 "--risk", str(risk_pct),
                 "--balance", str(bal),
                 "--interval", str(max(5, float(acc.get("interval_sec", 8)))),
                 "--rr", str(cfg.get("strategies", {}).get("hss", {}).get("rr", 1)),
                 "--pullback", str(cfg.get("strategies", {}).get("hss", {}).get("pullback", 2))]
        if not cfg.get("news", {}).get("gate", True):
            cargs.append("--no-news")
        spawn("crypto", cargs)

    if venue in ("mt5", "both"):
        margs = ["scripts/forward.py",
                 "--symbols", ",".join(cfg.get("mt5_symbols") or ["EURUSD"]),
                 "--leverage", str(cfg.get("leverage_mt5", 500)),
                 *flags]
        if mode == "paper":
            margs.append("--paper")
        elif mode == "live":
            margs.append("--live")
        spawn("mt5", margs)
    return started


def _pump(proc: subprocess.Popen, logf) -> None:
    try:
        for line in proc.stdout:
            logf.write(line)
            logf.flush()
    except Exception:
        pass
    finally:
        logf.close()


def tail_events(n: int = 60) -> list[dict]:
    path = LOG_DIR / "crypto_forward.jsonl"
    if not path.exists():
        return []
    rows = []
    started = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("type") == "start":
            started = r.get("ts") or ""
            rows = []
            continue
        if started and r.get("type") in {"fill", "close", "signal"} and r.get("reason") != "drop":
            rows.append(r)
    return rows[-n:]


def aggregate_positions(trades: list[dict]) -> list[dict]:
    """Собрать частичные выходы (TP1, BE, SL…) в одну сделку.

    Винрейт считаем по факту позиции: TP1 + безубыток = чистый плюс → победа.
    """
    groups: dict[str, dict] = {}
    for i, t in enumerate(trades):
        opened = str(t.get("opened_at") or "")
        if opened:
            key = "|".join([
                opened,
                str(t.get("coin") or ""),
                str(t.get("tf") or ""),
                str(t.get("side") or ""),
                str(t.get("entry") or ""),
            ])
        else:
            key = f"#{i}|{t.get('ts') or ''}"
        g = groups.setdefault(key, {
            "coin": t.get("coin"),
            "tf": t.get("tf"),
            "strategy": t.get("strategy"),
            "mode": t.get("mode"),
            "side": t.get("side"),
            "entry": t.get("entry"),
            "opened_at": t.get("opened_at"),
            "pnl": 0.0,
            "parts": 0,
            "hit_tp": False,
        })
        g["pnl"] = round(g["pnl"] + float(t.get("pnl") or 0), 2)
        g["parts"] += 1
        reason = str(t.get("reason") or "").lower()
        if reason.startswith("tp"):
            g["hit_tp"] = True
    out = []
    for g in groups.values():
        net = g["pnl"]
        # Плюс по позиции или взяли хотя бы один TP и не ушли в минус.
        g["win"] = net > 0 or (g["hit_tp"] and net >= 0)
        g["loss"] = net < 0
        out.append(g)
    return out


def _norm_hist_mode(m) -> str:
    x = str(m or "paper").lower()
    if x in ("live", "бой"):
        return "live"
    if x == "demo":
        return "demo"
    return "paper"


def _history_cleared() -> dict:
    path = LOG_DIR / "history_cleared.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _keep_hist(rec: dict, mode: str, cut: str | None) -> bool:
    if _norm_hist_mode(rec.get("mode")) != mode:
        return False
    ts = str(rec.get("ts") or rec.get("opened_at") or rec.get("stopped") or "")
    if cut and ts and ts < cut:
        return False
    return True


def history_payload(mode: str | None = None) -> dict:
    """Сделки выбранного режима — тест, демо и LIVE живут отдельно."""
    path = LOG_DIR / "crypto_forward.jsonl"
    runs: list[dict] = []
    trades: list[dict] = []
    signals: list[dict] = []
    events: list[dict] = []
    cur: dict | None = None

    def close_run() -> None:
        nonlocal cur
        if cur:
            runs.append(cur)
            cur = None

    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = r.get("type")
            if kind == "start":
                close_run()
                cur = {
                    "ts": r.get("ts"), "mode": _norm_hist_mode(r.get("mode") or "paper"),
                    "risk": r.get("risk"), "balance": r.get("balance"),
                    "symbols": len(r.get("symbols") or []),
                    "trades": 0, "pnl": 0.0, "stopped": None,
                }
            elif kind == "stop":
                if cur:
                    cur["stopped"] = r.get("ts")
                    cur["updates"] = r.get("updates")
                close_run()
            elif kind == "close":
                # reason="drop" — пользователь снял бумагу вручную (тест/демо,
                # кнопка «снять бумагу» или принудительный «стоп»), это не
                # исход сделки и не должно засорять винрейт/счётчики.
                if r.get("reason") == "drop":
                    continue
                coin = r.get("coin") or str(r.get("symbol") or "").split("/")[0]
                rec = {
                    "ts": r.get("ts"), "bar": r.get("bar"),
                    "mode": _norm_hist_mode(r.get("mode") or (cur or {}).get("mode") or "paper"),
                    "coin": coin, "tf": r.get("tf"),
                    "strategy": r.get("strategy") or "?",
                    "side": r.get("side"), "entry": r.get("entry"),
                    "exit": r.get("exit"), "volume": r.get("volume"),
                    "pnl": float(r.get("pnl") or 0),
                    "fee": float(r.get("fee") or 0),
                    "reason": r.get("reason") or "",
                    "opened_at": r.get("opened_at"),
                    "remaining": r.get("remaining"),
                }
                trades.append(rec)
                if cur:
                    cur["pnl"] = round(cur["pnl"] + rec["pnl"], 2)
            elif kind == "signal":
                signals.append({
                    "ts": r.get("ts"), "bar": r.get("bar"),
                    "mode": _norm_hist_mode(r.get("mode") or (cur or {}).get("mode") or "paper"),
                    "coin": r.get("coin") or str(r.get("symbol") or "").split("/")[0],
                    "tf": r.get("tf"), "strategy": r.get("strategy"),
                    "side": r.get("side"), "entry": r.get("entry"),
                })
            if kind in {"signal", "fill", "close"}:
                ev = dict(r)
                ev["coin"] = ev.get("coin") or str(ev.get("symbol") or "").split("/")[0]
                ev["mode"] = _norm_hist_mode(ev.get("mode") or (cur or {}).get("mode") or "paper")
                events.append(ev)
        close_run()

    want = _norm_hist_mode(mode or "paper")
    cut = _history_cleared().get(want)
    runs = [r for r in runs if _keep_hist(r, want, cut)]
    trades = [t for t in trades if _keep_hist(t, want, cut)]
    signals = [s for s in signals if _keep_hist(s, want, cut)]
    events = [e for e in events if _keep_hist(e, want, cut)]
    positions = aggregate_positions(trades)
    for run in runs:
        start = str(run.get("ts") or "")
        stop = str(run.get("stopped") or "z")
        rp = [p for p in positions
              if start <= str(p.get("opened_at") or "") <= stop]
        run["trades"] = len(rp)

    def bucket(key: str, rows: list[dict]) -> list[dict]:
        groups: dict[str, dict] = {}
        for t in rows:
            k = str(t.get(key) or "?")
            g = groups.setdefault(k, {"name": k, "n": 0, "wins": 0, "pnl": 0.0})
            g["n"] += 1
            g["pnl"] = round(g["pnl"] + t["pnl"], 2)
            if t.get("win"):
                g["wins"] += 1
        out = list(groups.values())
        for g in out:
            g["winrate"] = round(100 * g["wins"] / g["n"], 1) if g["n"] else 0
        out.sort(key=lambda x: -x["pnl"])
        return out

    n_pos = len(positions)
    wins = sum(1 for p in positions if p.get("win"))
    losses = sum(1 for p in positions if p.get("loss"))
    pnl = round(sum(t["pnl"] for t in trades), 2)
    payload = {
        "ok": True,
        "mode": want,
        "file": "logs/crypto_forward.jsonl",
        "snapshot": f"logs/history_{want}.json",
        "runs": runs[-40:],
        "trades": trades[-200:],
        "positions": positions[-100:],
        "signals": signals[-80:],
        "events": events[-300:],
        "stats": {
            "runs": len(runs),
            "trades": n_pos,
            "exits": len(trades),
            "wins": wins,
            "losses": losses,
            "winrate": round(100 * wins / n_pos, 1) if n_pos else 0,
            "pnl": pnl,
            "avg": round(pnl / n_pos, 2) if n_pos else 0,
            "by_coin": bucket("coin", positions),
            "by_strategy": bucket("strategy", positions),
            "by_mode": bucket("mode", positions),
        },
    }
    LOG_DIR.mkdir(exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    (LOG_DIR / f"history_{want}.json").write_text(text, encoding="utf-8")
    (LOG_DIR / "history.json").write_text(text, encoding="utf-8")
    return payload


def drop_paper_book() -> dict:
    """Снять бумажные позиции. На биржу ордер не уходит."""
    chart_p = ROOT / "dashboard" / "crypto_chart.json"
    if chart_p.exists():
        try:
            if json.loads(chart_p.read_text(encoding="utf-8")).get("mode") == "live":
                return {"ok": False,
                        "error": "бой: бумагой не снимать. Закройте позицию на бирже или через flatten."}
        except (json.JSONDecodeError, OSError):
            pass
    write_control(entries=True, paused=False)
    stop_jobs()
    state_p = LOG_DIR / "crypto_forward_state.json"
    chart_p = ROOT / "dashboard" / "crypto_chart.json"
    journal_p = LOG_DIR / "crypto_forward.jsonl"
    n = 0
    dropped: list[dict] = []
    if state_p.exists():
        try:
            data = json.loads(state_p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        for key, rec in (data.get("symbols") or {}).items():
            pos = rec.get("position")
            if pos or rec.get("pending"):
                n += 1
            if pos:
                parts = str(key).split("|")
                sym = parts[0] if parts else ""
                tf = parts[1] if len(parts) > 1 else ""
                coin = sym.split("/")[0] if sym else ""
                dropped.append({
                    "type": "close", "mode": "paper", "symbol": sym, "tf": tf,
                    "coin": coin, "strategy": pos.get("strategy") or "?",
                    "side": pos.get("side"), "entry": pos.get("entry"),
                    "exit": pos.get("entry"), "volume": pos.get("volume"),
                    "pnl": 0, "fee": 0, "reason": "drop",
                    "remaining": 0, "source": "drop",
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
            rec["position"] = None
            rec["pending"] = None
            rec["owner"] = ""
        state_p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if dropped:
        try:
            with journal_p.open("a", encoding="utf-8") as f:
                for row in dropped:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass
    if chart_p.exists():
        try:
            chart = json.loads(chart_p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            chart = {}
        blot = chart.setdefault("blot", {})
        blot["positions"] = []
        blot["orders"] = []
        for b in chart.get("books") or []:
            b["position"] = None
            b["pending"] = None
            b["setup"] = None
            b["pnl_open"] = None
        chart_p.write_text(json.dumps(chart, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "dropped": n}


def reset_paper_equity(mode: str | None = None) -> dict:
    """Бумажный итог к депозиту профиля режима. Бой не трогаем."""
    cfg = machine_config.load()
    want = mode if mode in ("paper", "demo", "live") else (cfg.get("account_mode") or "paper")
    acc = account_for_mode(cfg, want)
    deposit = float(acc.get("deposit") or 1000)
    chart_p = ROOT / "dashboard" / "crypto_chart.json"
    if chart_p.exists():
        try:
            if json.loads(chart_p.read_text(encoding="utf-8")).get("mode") == "live":
                return {"ok": False, "error": "бой: счёт на бирже не сбрасывается"}
        except (json.JSONDecodeError, OSError):
            pass
    if state_open_count():
        return {"ok": False,
                "error": "есть открытая бумага — сначала снимите, потом сброс"}
    cmd = LOG_DIR / "bot_cmd.json"
    LOG_DIR.mkdir(exist_ok=True)
    cmd.write_text(json.dumps({"reset_equity": True, "deposit": deposit}),
                   encoding="utf-8")
    state_p = LOG_DIR / "crypto_forward_state.json"
    if state_p.exists() and not bot_alive():
        try:
            data = json.loads(state_p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        data["balance"] = deposit
        for rec in (data.get("symbols") or {}).values():
            rec["trades"] = []
        state_p.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    if chart_p.exists():
        try:
            chart = json.loads(chart_p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            chart = {}
        chart["equity"] = deposit
        chart["start"] = deposit
        for b in chart.get("books") or []:
            b["pnl"] = 0
            b["trades"] = 0
        chart_p.write_text(json.dumps(chart, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "equity": deposit, "deposit": deposit}


def clear_history(mode: str | None = None) -> dict:
    """Очищает историю выбранного режима. Журнал других режимов не трогаем."""
    want = _norm_hist_mode(mode or "paper")
    marks = _history_cleared()
    marks[want] = datetime.now().isoformat(timespec="seconds")
    LOG_DIR.mkdir(exist_ok=True)
    (LOG_DIR / "history_cleared.json").write_text(
        json.dumps(marks, ensure_ascii=False, indent=2), encoding="utf-8")
    payload = history_payload(want)
    payload["cleared"] = True
    payload["cleared_mode"] = want
    return payload


def tail_log(name: str, n: int = 40) -> str:
    path = LOG_DIR / f"admin_{name}.log"
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def calendar_payload() -> dict:
    events = load_calendar()
    now = datetime.utcnow()
    rows = []
    for e in events:
        rows.append({
            "time": e.time.isoformat(),
            "currency": e.currency,
            "impact": e.impact,
            "title": e.title,
            "past": str(e.time) < now.isoformat(),
        })
    sections = []
    for sec in NEWS_SECTIONS:
        if sec["role"] != "gate":
            sections.append({**sec, "events": []})
            continue
        curs = set(sec["currencies"])
        ev = [r for r in rows if r["currency"] in curs and r["impact"] == "High"]
        sections.append({**sec, "events": ev[:40]})
    return {
        "source": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "horizon": "только текущая неделя ForexFactory (Sun–Sat)",
        "gate": "High-impact, буфер ±N минут, валюта → инструмент",
        "map": INSTRUMENT_CURRENCIES,
        "crypto": "любой */USDT режется как USD",
        "total": len(rows),
        "high": sum(1 for r in rows if r["impact"] == "High"),
        "sections": sections,
        "all_high": [r for r in rows if r["impact"] == "High"][:80],
    }


def telegram_payload() -> dict:
    posts = fetch_posts()
    by = {k: [] for k in CHANNELS}
    for p in posts:
        by.setdefault(p.source, []).append({
            "time": str(p.time) if p.time is not None else None,
            "kind": p.kind,
            "coins": list(p.coins),
            "text": p.text[:400],
        })
    return {
        "role": "брифинг, не риск-гейт",
        "channels": [
            {"id": "cryptodaily", "url": "https://t.me/cryptodaily",
             "title": "Crypto Daily", "posts": by.get("cryptodaily", [])[-8:]},
            {"id": "coin_post", "url": "https://t.me/Coin_Post",
             "title": "Coin Post", "posts": by.get("coin_post", [])[-8:]},
            {"id": "dashi_finance", "url": "https://t.me/dashi_finance",
             "title": "Dashi Finance", "posts": by.get("dashi_finance", [])[-8:]},
        ],
    }


ANTHROPIC_MODEL = "claude-sonnet-5"
NARRATIVE_PROMPT = """Найди свежие новости и обсуждения за последние 12 часов по активу {symbol} (это бессрочный фьючерс на криптобирже). Ответь кратко и по делу, на русском:

1. Перечисли до 3 доминирующих нарративов сейчас. Для каждого укажи:
   - новая информация или пересказ старого;
   - кто продвигает (розница/медиа/инсайдеры), если понятно из источников;
   - потенциальное влияние на цену: высокое/среднее/низкое, одна строка обоснования.
2. Отдельно: {symbol} двигается изолированно (свой катализатор) или вместе со всем сектором/из-за BTC? Если есть данные о correlated активах — укажи.
3. В конце — один нарратив, который вероятнее всего подвинет цену сегодня, одной строкой.

Если свежих данных не нашлось — так и скажи, не выдумывай."""


def narrative_check(symbol: str) -> dict:
    """Сканер инфонарративов по кнопке в панели — не автоматический фильтр,
    решение о входе всегда принимает человек. См. память ml-filter-plan/
    заметку про промпты из телеграм-поста: LLM с веб-поиском на живой сигнал,
    без изменения торговой логики."""
    key = settings.anthropic_api_key
    if not key:
        return {"ok": False, "error": "Нет ANTHROPIC_API_KEY в .env"}
    payload = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1024,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        "messages": [{"role": "user", "content": NARRATIVE_PROMPT.format(symbol=symbol)}],
    }).encode("utf-8")
    req = Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=45) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    text = "".join(
        block.get("text", "") for block in data.get("content", [])
        if block.get("type") == "text"
    )
    if not text:
        return {"ok": False, "error": "пустой ответ от модели"}
    return {"ok": True, "symbol": symbol, "text": text}


def scanner_snapshot() -> dict:
    cfg = machine_config.load().get("scanner") or {}
    empty = {
        "enabled": bool(cfg.get("enabled", True)),
        "top_k": int(cfg.get("top_k", 2)),
        "min_score": float(cfg.get("min_score", 1.2)),
        "hot": [],
        "hits": [],
    }
    # Бот выключен — файл со снимком сканера мог остаться от прошлого
    # запуска (никто его не чистит на стопе) и отдавать его как живой было
    # бы враньём: панель показывала бы "горячие" тикеры часами после стопа.
    if not inferred_jobs(jobs_snapshot()):
        return empty
    path = ROOT / "logs" / "scanner_state.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return empty


def _chart_open_positions(ch: dict) -> list[dict]:
    """Открытые позиции из crypto_chart.json (blot + fallback из books)."""
    blot = ch.get("blot") or {}
    pos = list(blot.get("positions") or [])
    if pos:
        return pos
    out: list[dict] = []
    seen: set[str] = set()
    for b in ch.get("books") or []:
        p = b.get("position")
        if not p:
            continue
        key = f"{b.get('pair')}|{b.get('tf')}"
        if key in seen:
            continue
        seen.add(key)
        out.append({
            **p,
            "pair": b.get("pair"),
            "label": b.get("label"),
            "tf": b.get("tf"),
            "venue": b.get("venue"),
            "pnl": b.get("pnl_open"),
        })
    return out


def live_account_snapshot() -> dict:
    """Баланс Bybit для панели в LIVE — из снимка бота или напрямую с биржи."""
    cfg = machine_config.load()
    lev = int(cfg.get("leverage_crypto") or 5)
    risk = float(cfg.get("risk_pct") or 1)
    chart_p = ROOT / "dashboard" / "crypto_chart.json"
    if chart_p.exists():
        try:
            ch = json.loads(chart_p.read_text(encoding="utf-8"))
            if ch.get("mode") == "live":
                wallet = float(ch.get("wallet") or 0)
                equity = float(ch.get("equity") or wallet)
                positions = _chart_open_positions(ch)
                used = float(ch.get("used_margin") or 0)
                return {
                    "ok": True,
                    "source": "bot",
                    "wallet": wallet,
                    "equity": equity,
                    "available": float(ch.get("available") or max(0, equity - used)),
                    "used_margin": used,
                    "leverage": int(ch.get("leverage") or lev),
                    "risk": float(ch.get("risk") or risk),
                    "open_positions": len(positions),
                    "positions": positions,
                }
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
    running_live = any(
        j.get("running") and j.get("mode") == "live"
        for j in inferred_jobs(jobs_snapshot()))
    if not settings.bybit_api_key:
        return {"ok": False, "error": "нет ключей Bybit в .env"}
    try:
        from neft.adapters.crypto_broker import CryptoBroker

        br = CryptoBroker(testnet=False, leverage=lev)
        acc = br.connect()
        br.disconnect()
        wallet = float(acc.wallet or acc.equity)
        equity = float(acc.equity)
        return {
            "ok": True,
            "source": "exchange",
            "wallet": wallet,
            "equity": equity,
            "available": float(acc.available or acc.balance or equity),
            "used_margin": float(acc.used_margin or 0),
            "leverage": lev,
            "risk": risk,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def public_state() -> dict:
    cfg = machine_config.load()
    return {
        "config": cfg,
        "crypto_testnet": bool(settings.crypto_testnet),
        "has_binance_keys": bool(settings.binance_api_key),
        "has_bybit_keys": bool(settings.bybit_api_key),
        "routes_mt5": ROUTES,
        "crypto_watchlist": CRYPTO_WATCHLIST,
        "crypto_ban": ["BTC/USDT:USDT", "ENA/USDT:USDT"],
        "crypto_universe": CRYPTO_UNIVERSE,
        "crypto_starter": CRYPTO_STARTER,
        "scanner": scanner_snapshot(),
        "jobs": inferred_jobs(jobs_snapshot()),
        "control": read_control(),
        "ready": live_checklist(),
        "strategies_meta": {
            "hss": {
                "name": "HSS · Heikin Ashi scalp",
                "file": "neft/strategies/scalp_ha.py",
                "tf": "M1",
                "what": "Тренд по EMA100, чистый откат, вход стопом на объёмной doji. "
                        "Сигнал по HA, исполнение по реальной цене. RR фиксированный.",
            },
            "london_sr": {
                "name": "London S/R",
                "file": "neft/strategies/london_sr.py",
                "tf": "M1 (крипта иногда M5)",
                "what": "Хай/лоу лондонской сессии — уровни для Нью-Йорка. "
                        "Вход после слома структуры. Цель — ближайший свинг, не фиксированный RR.",
            },
            "breakout": {
                "name": "London Breakout",
                "file": "neft/strategies/london_breakout.py",
                "tf": "M5",
                "what": "Бокс лондона, пробой после 09:30 ET, одна сделка в день, RR 1:1.5–1:2. "
                        "В машине по умолчанию выключен, кроме маршрутов NAS100/GER40/EURUSD+.",
            },
            "squeeze": {
                "name": "Squeeze",
                "file": "neft/strategies/squeeze.py",
                "tf": "M15 → M5",
                "what": "Треугольник: lower highs + higher lows, 2 касания с каждой стороны. "
                        "Вход на сломе свинга, не по линии. RR обычно ≥ 2:1. По умолчанию выключен.",
            },
            "session_flow": {
                "name": "Flow · 6 сетапов 5m/15m",
                "file": "neft/strategies/session_flow.py",
                "tf": "5m / 15m",
                "what": "Отдельно: asian_sweep, failed_orb, range_fade, vwap_reclaim, "
                        "ema_pull, orb_follow. blend включает все и режет по режиму "
                        "тренд/флет. Сессионные — только Нью-Йорк UTC.",
            },
            "playbook": {
                "name": "Playbook · единая 5m",
                "file": "neft/strategies/factory.py",
                "tf": "5m",
                "what": "Flow + London S/R. В сканере почти не нужен: те же куски "
                        "висят на каждой монете вселенной отдельно.",
            },
            "scanner": {
                "name": "Сканер вселенной",
                "file": "neft/core/scanner.py",
                "tf": "1m + 5m",
                "what": "BTC и ENA в бане. На каждом баре rank по HSS/Flow/LSR, "
                        "в работу top-2. Монета не закреплена.",
            },
        },
    }


def public_klines(pair: str, tf: str, venue: str) -> list[dict]:
    """Публичные свечи биржи для панели графика (как смена ТФ в TradingView)."""
    coin = pair.upper().replace("/", "").replace(":USDT", "")
    if not coin.endswith("USDT"):
        coin += "USDT"
    tf = tf.lower()
    if venue == "bybit":
        iv = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}.get(tf, "1")
        url = ("https://api.bybit.com/v5/market/kline?category=linear"
               f"&symbol={coin}&interval={iv}&limit=500")
        raw = json.loads(urlopen(Request(url, headers={"User-Agent": "NEFT"}), timeout=12).read())
        rows = list(reversed((raw.get("result") or {}).get("list") or []))
        out = []
        for r in rows:
            out.append({
                "time": int(int(r[0]) / 1000),
                "open": float(r[1]), "high": float(r[2]),
                "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
            })
        return out
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={coin}&interval={tf}&limit=500"
    rows = json.loads(urlopen(Request(url, headers={"User-Agent": "NEFT"}), timeout=12).read())
    return [{
        "time": int(r[0] / 1000),
        "open": float(r[1]), "high": float(r[2]),
        "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
    } for r in rows]


def public_orderbook(pair: str, venue: str) -> dict:
    """Публичный стакан цен биржи — только на чтение, для тикета панели."""
    coin = pair.upper().replace("/", "").replace(":USDT", "")
    if not coin.endswith("USDT"):
        coin += "USDT"
    if venue == "bybit":
        url = ("https://api.bybit.com/v5/market/orderbook?category=linear"
               f"&symbol={coin}&limit=25")
        raw = json.loads(urlopen(Request(url, headers={"User-Agent": "NEFT"}), timeout=8).read())
        res = raw.get("result") or {}
        bids = [[float(p), float(q)] for p, q in (res.get("b") or [])]
        asks = [[float(p), float(q)] for p, q in (res.get("a") or [])]
        return {"bids": bids, "asks": asks}
    url = f"https://fapi.binance.com/fapi/v1/depth?symbol={coin}&limit=20"
    raw = json.loads(urlopen(Request(url, headers={"User-Agent": "NEFT"}), timeout=8).read())
    bids = [[float(p), float(q)] for p, q in (raw.get("bids") or [])]
    asks = [[float(p), float(q)] for p, q in (raw.get("asks") or [])]
    return {"bids": bids, "asks": asks}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args) -> None:
        sys.stderr.write("admin: " + (fmt % args) + "\n")

    def _send(self, code: int, body: bytes, ctype: str, *,
              session: tuple[str, str, int] | None = None,
              clear_sess: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in security_headers():
            self.send_header(k, v)
        if session:
            set_session_headers(self, *session)
        if clear_sess:
            clear_session_headers(self)
        self.end_headers()
        self.wfile.write(body)

    def _gate(self) -> bool:
        if not client_ok(self) or not host_ok(self):
            self._send(403, _json({"ok": False, "error": "только 127.0.0.1"}),
                       "application/json")
            return False
        return True

    def _need_sess(self) -> bool:
        if session_ok(self):
            return True
        path = urlparse(self.path).path
        if path.startswith("/api/") or path.endswith(".json"):
            self._send(401, _json({"ok": False, "auth": False}), "application/json")
        elif path.endswith((".js", ".css")):
            self._send(401, b"auth", "text/plain")
        else:
            self._send(200, LOGIN_HTML.encode("utf-8"), "text/html")
        return False

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > BODY_MAX:
            return {"_too_big": True}
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self) -> None:  # noqa: N802
        if not self._gate():
            return
        path = urlparse(self.path).path
        if path == "/api/login":
            ip = self.client_address[0]
            cool = login_cooldown_left(ip)
            body = {"ok": True, "auth": session_ok(self),
                    "telegram": login_tg_enabled()}
            if cool > 0:
                body["retry_after"] = cool
            self._send(200, _json(body), "application/json")
            return
        if path == "/api/login/poll":
            q = parse_qs(urlparse(self.path).query)
            cid = (q.get("id") or [""])[0]
            self._send(200, _json({"ok": True, **login_poll(cid)}),
                       "application/json")
            return
        # Публичные свечи/стакан — без сессии: только localhost, данные с биржи.
        if path == "/api/klines":
            q = parse_qs(urlparse(self.path).query)
            chk = safe_klines((q.get("pair") or [""])[0],
                              (q.get("tf") or ["1m"])[0],
                              (q.get("venue") or ["binance"])[0])
            if not chk:
                self._send(400, _json({"ok": False, "error": "пара/тф", "candles": []}),
                           "application/json")
                return
            pair, tf, venue = chk
            try:
                self._send(200, _json({"ok": True, "candles": public_klines(pair, tf, venue)}),
                           "application/json")
            except Exception as e:  # noqa: BLE001
                self._send(200, _json({"ok": False, "error": str(e), "candles": []}),
                           "application/json")
            return
        if path == "/api/orderbook":
            q = parse_qs(urlparse(self.path).query)
            chk = safe_klines((q.get("pair") or [""])[0], "1m",
                              (q.get("venue") or ["binance"])[0])
            if not chk:
                self._send(400, _json({"ok": False, "error": "пара", "bids": [], "asks": []}),
                           "application/json")
                return
            pair, _tf, venue = chk
            try:
                ob = public_orderbook(pair, venue)
                self._send(200, _json({"ok": True, **ob}), "application/json")
            except Exception as e:  # noqa: BLE001
                self._send(200, _json({"ok": False, "error": str(e), "bids": [], "asks": []}),
                           "application/json")
            return
        if not self._need_sess():
            return
        if path in ("/", "/admin", "/admin/", "/chart", "/chart.html"):
            html = ADMIN_HTML.read_text(encoding="utf-8")
            self._send(200, html.encode("utf-8"), "text/html")
            return
        if path == "/lightweight-charts.js":
            f = safe_dashboard("lightweight-charts.js")
            self._send(200, f.read_bytes() if f else b"", "text/javascript")
            return
        if path == "/panel-boot.js":
            f = safe_dashboard("panel-boot.js")
            self._send(200, f.read_bytes() if f else b"", "text/javascript")
            return
        if path == "/crypto_chart.json":
            f = safe_dashboard("crypto_chart.json")
            self._send(200, f.read_bytes() if f else b"{}", "application/json")
            return
        if path.startswith("/dashboard/"):
            f = safe_dashboard(path)
            if f:
                ctype = {".html": "text/html", ".css": "text/css",
                         ".js": "text/javascript", ".json": "application/json",
                         ".svg": "image/svg+xml", ".woff2": "font/woff2"}[f.suffix.lower()]
                self._send(200, f.read_bytes(), ctype)
                return
        if path == "/api/state":
            self._send(200, _json(public_state()), "application/json")
            return
        if path == "/api/news":
            try:
                cal = calendar_payload()
            except Exception as e:  # noqa: BLE001
                cal = {"error": str(e), "sections": NEWS_SECTIONS}
            try:
                tg = telegram_payload()
            except Exception as e:  # noqa: BLE001
                tg = {"error": str(e), "channels": []}
            self._send(200, _json({
                "calendar": cal,
                "telegram": tg,
                "fred": {
                    "releases": FRED_RELEASES,
                    "fomc": FOMC_MEETING_DATES,
                    "role": "бэктест, не live",
                },
            }), "application/json")
            return
        if path == "/api/events":
            self._send(200, _json({"ok": True, "events": tail_events()}), "application/json")
            return
        if path == "/api/history":
            q = parse_qs(urlparse(self.path).query)
            hist_mode = (q.get("mode") or ["paper"])[0]
            self._send(200, _json(history_payload(hist_mode)), "application/json")
            return
        if path == "/api/ready":
            self._send(200, _json(live_checklist()), "application/json")
            return
        if path == "/api/live/account":
            self._send(200, _json(live_account_snapshot()), "application/json")
            return
        if path == "/api/logs":
            self._send(200, _json({n: tail_log(n) for n in ("crypto", "mt5")}),
                       "application/json")
            return
        self._send(404, b"not found", "text/plain")

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._gate():
            return
        self._send(204, b"", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if not self._gate():
            return
        if not origin_ok(self):
            self._send(403, _json({"ok": False, "error": "чужой origin"}),
                       "application/json")
            return
        path = urlparse(self.path).path
        if not rate_ok("post:" + self.client_address[0], 40, 60):
            self._send(429, _json({"ok": False, "error": "слишком часто"}),
                       "application/json")
            return
        body = self._read_json()
        if body.get("_too_big"):
            self._send(413, _json({"ok": False, "error": "слишком большой запрос"}),
                       "application/json")
            return
        if path == "/api/login":
            if not rate_ok("login", 8, 60):
                self._send(429, _json({"ok": False, "error": "подождите минуту"}),
                           "application/json")
                return
            if not token_ok(str(body.get("token") or "")):
                time.sleep(0.4)
                self._send(403, _json({"ok": False, "error": "неверный код"}),
                           "application/json")
                return
            sess = issue_session()
            self._send(200, _json({"ok": True}), "application/json", session=sess)
            return
        if path == "/api/login/telegram":
            ip = self.client_address[0]
            cool = login_cooldown_left(ip)
            if cool > 0:
                self._send(429, _json({
                    "ok": False,
                    "error": f"повтор через {cool} сек — вход был отклонён",
                    "retry_after": cool,
                }), "application/json")
                return
            if not rate_ok("tg_login:" + ip, 6, 60):
                self._send(429, _json({"ok": False, "error": "подождите минуту"}),
                           "application/json")
                return
            cid, err = login_create(ip)
            if err:
                cool = login_cooldown_left(ip)
                body = {"ok": False, "error": err}
                if cool > 0:
                    body["retry_after"] = cool
                self._send(429 if cool > 0 else 400, _json(body),
                           "application/json")
                return
            self._send(200, _json({"ok": True, "id": cid, "expires": 300}),
                       "application/json")
            return
        if path == "/api/login/complete":
            if not rate_ok("login_complete", 12, 60):
                self._send(429, _json({"ok": False, "error": "подождите"}),
                           "application/json")
                return
            cid = str(body.get("id") or "")
            if not login_consume(cid):
                self._send(403, _json({"ok": False, "error": "не подтверждено или истекло"}),
                           "application/json")
                return
            sess = issue_session()
            self._send(200, _json({"ok": True}), "application/json", session=sess)
            return
        if path == "/api/logout":
            self._send(200, _json({"ok": True}), "application/json", clear_sess=True)
            return
        if not session_ok(self) or not csrf_ok(self):
            self._send(401, _json({"ok": False, "auth": False, "error": "нет сессии"}),
                       "application/json")
            return
        if path == "/api/config":
            cfg = machine_config.save(body)
            self._send(200, _json({"ok": True, "config": cfg}), "application/json")
            return
        if path == "/api/narrative":
            symbol = str(body.get("symbol") or "").strip()
            if not symbol or len(symbol) > 40:
                self._send(400, _json({"ok": False, "error": "symbol"}),
                           "application/json")
                return
            if not rate_ok("narrative:" + self.client_address[0], 6, 60):
                self._send(429, _json({"ok": False, "error": "слишком часто, подождите"}),
                           "application/json")
                return
            self._send(200, _json(narrative_check(symbol)), "application/json")
            return
        if path == "/api/history/clear":
            self._send(200, _json(clear_history(body.get("mode"))), "application/json")
            return
        if path == "/api/paper/drop":
            self._send(200, _json(drop_paper_book()), "application/json")
            return
        if path == "/api/paper/reset":
            self._send(200, _json(reset_paper_equity(body.get("mode"))), "application/json")
            return
        if path == "/api/stop":
            # Один клик — полная остановка, без промежуточной "паузы": тот
            # двухшаговый режим (1-й клик — мягкая пауза, 2-й — force)
            # только путал и ощущался как "стоп не работает". Тест/демо —
            # принудительный стоп при открытых позициях снимает их вместе
            # с ботом (условные сделки, в статистику не идут — см.
            # drop_paper_book). Бой так не делать: сделки реальные, живут
            # своим чередом до TP/SL на бирже независимо от бота;
            # drop_paper_book() сама откажет для live на всякий случай.
            open_n = state_open_count()
            chart_p = ROOT / "dashboard" / "crypto_chart.json"
            live_mode = False
            if chart_p.exists():
                try:
                    live_mode = json.loads(chart_p.read_text(encoding="utf-8")).get("mode") == "live"
                except (json.JSONDecodeError, OSError):
                    pass
            if open_n and not live_mode:
                dropped = drop_paper_book()
                self._send(200, _json({"ok": True, "paused": False, "jobs": [],
                                       "dropped": dropped.get("dropped", 0)}),
                           "application/json")
                return
            write_control(entries=True, paused=False)
            stop_jobs()
            self._send(200, _json({"ok": True, "paused": False, "jobs": []}),
                       "application/json")
            return
        if path == "/api/run":
            mode = body.get("mode")
            if mode not in ("paper", "demo", "live"):
                self._send(400, _json({"ok": False, "error": "mode: paper|demo|live"}),
                           "application/json")
                return
            if not rate_ok("run:" + str(mode), 6 if mode != "live" else 2, 60):
                self._send(429, _json({"ok": False, "error": "запуск слишком часто"}),
                           "application/json")
                return
            cfg = machine_config.load()
            if mode == "live":
                if settings.demo_only:
                    self._send(403, _json({
                        "ok": False,
                        "error": "В .env DEMO_ONLY=true — бой заблокирован. Снимите после теста.",
                    }), "application/json")
                    return
                venue = cfg.get("venue") or "crypto"
                if venue in ("crypto", "both") and not (
                        settings.binance_api_key or settings.bybit_api_key):
                    self._send(403, _json({
                        "ok": False,
                        "error": "Нет ключей Binance/Bybit в .env — на биржу ордер не уйдёт.",
                    }), "application/json")
                    return
            started = start_jobs(mode, cfg)
            self._send(200, _json({"ok": True, "started": started, "mode": mode,
                                   "jobs": jobs_snapshot()}), "application/json")
            return
        self._send(404, b"not found", "text/plain")


def main() -> None:
    if not ADMIN_HTML.exists():
        print("нет dashboard/admin.html")
        raise SystemExit(1)
    panel_token()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"NEFT админ: {url}  только 127.0.0.1")
    print("код панели: data/panel_token  (или ADMIN_PANEL_TOKEN в .env)")
    if sys.stdin.isatty():
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        stop_jobs()
        httpd.server_close()


if __name__ == "__main__":
    main()
