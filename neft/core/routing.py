"""Какая стратегия на каком инструменте работает.

Основание — характер инструмента, а не только цифры бэктеста:
  HSS ловит продолжение тренда внутри импульса — ему нужны трендовые
  индексные фьючерсы с чистым моментумом.
  London S/R торгует отбой от уровня — ему нужен инструмент, который эти
  уровни уважает.

ВАЖНО: набор выбран на той же истории, на которой измерялся. Это делает
цифры оптимистичными. Перед боевым запуском маршрутизацию надо подтвердить
на новых данных.
"""

ROUTES: dict[str, dict[str, bool]] = {
    # HSS M5 plus_only (90д): только плюсовые символы. NAS100 — preflight, без HSS.
    "NAS100":  {"HSS": False, "London S/R": True,  "Breakout": False},
    "DJ30":    {"HSS": True,  "London S/R": False, "Breakout": False},
    "FRA40":   {"HSS": True,  "London S/R": False, "Breakout": False},
    "XAUUSD+": {"HSS": True,  "London S/R": True,  "Breakout": False},
    "UKOUSD":  {"HSS": True,  "London S/R": False, "Breakout": False},
    "GBPUSD+": {"HSS": True,  "London S/R": True,  "Breakout": False},
    "USDJPY+": {"HSS": True,  "London S/R": True,  "Breakout": False},
    "USDCAD+": {"HSS": True,  "London S/R": True,  "Breakout": False},
    "USDCHF+": {"HSS": True,  "London S/R": True,  "Breakout": False},
    "EURJPY+": {"HSS": True,  "London S/R": True,  "Breakout": False},
    "GER40":   {"HSS": False, "London S/R": True,  "Breakout": True},
    "EURUSD+": {"HSS": False, "London S/R": True,  "Breakout": True},
    "USOUSD":  {"HSS": False, "London S/R": False, "Breakout": False},
    "AUDUSD+": {"HSS": False, "London S/R": True,  "Breakout": False},
    "EURGBP+": {"HSS": False, "London S/R": True,  "Breakout": False},
}

# RR пробоя — как у автора: 2:1. Не подгоняем под бэктест.
BREAKOUT_RR = {"NAS100": 2.0, "GER40": 2.0, "EURUSD+": 2.0}

DEFAULT = {"HSS": False, "London S/R": True, "Breakout": False, "Squeeze": False}

# Squeeze (Joovier Day 9): треугольник LH+HL, вход на сломе свинга. По умолчанию
# выключен в машине, пока нет отдельного годового прогона — включается в
# data/bot_config.json → strategies.squeeze.enabled.


def enabled_for(symbol: str) -> dict[str, bool]:
    if symbol in ROUTES:
        return ROUTES[symbol]
    # Bybit XAUUSD+ ↔ MetaQuotes XAUUSD и т.п.
    base = symbol[:-1] if symbol.endswith("+") else symbol
    for key, routes in ROUTES.items():
        if key == base or key.rstrip("+") == base or key == base + "+":
            return routes
    return DEFAULT


# ── Крипта ──────────────────────────────────────────────────────────────
#
# Из широкого прогона (12 пар, M1 против M5, круглосуточно против сессии
# 16-19) вышли системные закономерности, а не совпадение по одной паре:
#
#   HSS ловит doji-разворот — на M5 такая свеча растягивается на 5 минут,
#   сигнал размывается. HSS живёт на M1.
#
#   London S/R ждёт слома структуры после касания уровня — на M1 внутри
#   крипто-волатильности это чаще шум, чем сигнал; на M5 картина чище.
#
#   Сессионный фильтр 16-19 придуман под открытие Нью-Йорка на форекс и
#   индексах. У крипты нет открытия биржи — рынок никогда не закрывается.
#   На M1 фильтр почти везде ухудшает результат: он просто обрезает
#   выборку без выигрыша в качестве. На M5 он всё же работает, но по
#   другой причине — отсекает шумные ночные касания уровней, которые на
#   грубом таймфрейме чаще дают ложный пробой.
#
# Формат: список конфигураций (стратегия, таймфрейм, сессия или None).
# Монета не фиксируется: сканер на каждом баре выбирает, где сейчас сетап.

# Цель машины — винрейт и короткий риск, не максимум дохода.
# Биткоин не торгуем: слишком связан со всеми альтами, пятница на годе
# минус, а «+300% слота» — не live. ENA на годе минус при DD 24%.
CRYPTO_BAN: frozenset[str] = frozenset({
    "BTC/USDT:USDT",
    "ENA/USDT:USDT",
    # Binance и Bybit сняли контракт с торгов (Binance status=SETTLING,
    # deliveryDate 2026-06-23; Bybit status=Closed, 2026-06-15). Обе биржи
    # молча отдают на fetch_ohlcv одну и ту же замороженную свечу с нулевым
    # объёмом вместо ошибки — без бана панель и сканер думали, что рынок
    # стоит на месте.
    "TON/USDT:USDT",
})

# Вселенная сканера: одни и те же стратегии, монета выбирается по сетапу.
CRYPTO_UNIVERSE: list[str] = [
    "ETH/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
    "BCH/USDT:USDT",
    "HYPE/USDT:USDT",
    "SOL/USDT:USDT",
    "DOGE/USDT:USDT",
    "ZEC/USDT:USDT",
    "SUI/USDT:USDT",
    "AVAX/USDT:USDT",
    "LINK/USDT:USDT",
    "ADA/USDT:USDT",
    "LTC/USDT:USDT",
    "NEAR/USDT:USDT",
    "APT/USDT:USDT",
    "ARB/USDT:USDT",
    "TRX/USDT:USDT",
    "SEI/USDT:USDT",
    "ATOM/USDT:USDT",
    "INJ/USDT:USDT",
    "UNI/USDT:USDT",
    "1000PEPE/USDT:USDT",
]

# Старт с малого: ликвидные альты, где LSR/HSS на годе не развалились.
CRYPTO_STARTER: list[str] = [
    "ETH/USDT:USDT",
    "BCH/USDT:USDT",
    "HYPE/USDT:USDT",
    "DOGE/USDT:USDT",
]


# Сканер в сумме закрывает 1м / 5м / 15м. Стратегия живёт на своём ТФ:
# HSS — doji на 1м, London/Flow/пробой — 5м, уровни и squeeze ещё на 15м.
SCAN_TFS: tuple[str, ...] = ("1m", "5m", "15m")
STRATEGY_TFS: dict[str, tuple[str, ...]] = {
    "HSS": ("1m",),
    "London S/R": ("5m", "15m"),
    "Flow": ("5m",),
    "Squeeze": ("5m",),
    "Breakout": ("5m",),
}


def _bar_tf(raw: str, default: str) -> str:
    t = str(raw or default).strip().lower()
    return {"m1": "1m", "m5": "5m", "m15": "15m", "h1": "1h"}.get(t, t or default)


# Имя в маршруте → ключ в bot_config.strategies (галочки панели).
KIND_CFG = {
    "HSS": "hss",
    "London S/R": "london_sr",
    "LondonSR": "london_sr",
    "Flow": "session_flow",
    "Session Flow": "session_flow",
    "Playbook": "playbook",
    "All": "playbook",
    "Squeeze": "squeeze",
    "Breakout": "breakout",
    "London Breakout": "breakout",
}


def strategy_on(name: str, strategies: dict | None) -> bool:
    """Включена ли стратегия в панели. Нет записи — считаем включённой."""
    if not strategies:
        return True
    key = KIND_CFG.get(name)
    if key is None:
        for label, cfg_key in KIND_CFG.items():
            if label in name:
                key = cfg_key
                break
    if key is None:
        return True
    return bool((strategies.get(key) or {}).get("enabled", True))


def kit_from_config(strategies: dict | None, *, hss_24h: bool = False) -> list[dict]:
    """Маршруты крипты: каждая включённая стратегия на 1м, 5м и 15м."""
    s = strategies or {}
    hss = s.get("hss") or {}
    lsr = s.get("london_sr") or {}
    flow = s.get("session_flow") or {}
    pb = s.get("playbook") or {}
    sq = s.get("squeeze") or {}
    bo = s.get("breakout") or {}
    kit: list[dict] = []

    def add(name: str, session, extra: dict | None = None) -> None:
        for tf in STRATEGY_TFS.get(name, SCAN_TFS):
            rec = {"strategy": name, "tf": tf, "session": session}
            if extra:
                rec.update(extra)
            kit.append(rec)

    def utc3_to_utc(pair) -> tuple[int, int] | None:
        """Панель в UTC+3, свечи Binance — UTC."""
        if pair is None:
            return None
        return (int(pair[0]) - 3, int(pair[1]) - 3)

    # Playbook — это «полный набор», не отдельный слот: иначе HSS дублируется.
    if (hss.get("enabled", True) or pb.get("enabled")) and hss.get("crypto_enabled", False):
        around = bool(hss.get("all_day") or hss_24h)
        raw = hss.get("session") or (16, 19)
        add("HSS", None if around else utc3_to_utc(raw))
    if lsr.get("enabled", True) or pb.get("enabled"):
        add("London S/R", utc3_to_utc(lsr.get("ny") or (16, 23)))
    if flow.get("enabled", True) or pb.get("enabled"):
        add("Flow", None)
    if sq.get("enabled"):
        add("Squeeze", None)
    if bo.get("enabled"):
        add("Breakout", None)
    return kit


def scan_kit() -> list[dict]:
    """Запасной набор, если в конфиге всё выключено."""
    kit: list[dict] = []
    for name, sess in (("HSS", None), ("London S/R", (16, 19)), ("Flow", None)):
        for tf in STRATEGY_TFS[name]:
            kit.append({"strategy": name, "tf": tf, "session": sess})
    return kit


CRYPTO_ROUTES: dict[str, list[dict]] = {s: scan_kit() for s in CRYPTO_UNIVERSE}

# Кандидаты вне вселенной, пока мало сделок.
CRYPTO_WATCHLIST: dict[str, str] = {
    "1000PEPE/USDT:USDT": "London S/R M5 16-19: слабый плюс, требует проверки",
}


def banned(symbol: str) -> bool:
    return symbol in CRYPTO_BAN or symbol.startswith("BTC/")


def norm_crypto_symbol(s: str) -> str:
    t = str(s or "").strip().upper().replace(" ", "")
    if not t:
        return ""
    if t.endswith("USDT") and "/" not in t:
        t = t[:-4].rstrip("/") + "/USDT:USDT"
    elif "/" in t and ":" not in t:
        t = t + ":USDT"
    elif "/" not in t:
        t = f"{t}/USDT:USDT"
    base = t.split("/")[0]
    if base in {"PEPE", "1000PEPE"}:
        t = "1000PEPE/USDT:USDT"
    if base in {"SHIB", "1000SHIB"}:
        t = "1000SHIB/USDT:USDT"
    return t


def crypto_routes_for(symbol: str) -> list[dict]:
    if banned(symbol):
        return []
    return CRYPTO_ROUTES.get(symbol, scan_kit())
