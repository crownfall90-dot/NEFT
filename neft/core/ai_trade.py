"""Команды ИИ-разбора из Crypto Daily (#AItrade) — на данных NEFT, не на Pine.

Пост канала предлагает TradingView + промпты ChatGPT. У нас уже есть свой
бэктест, риск-слой и годовая статистика: команды отвечают теми же вопросами
(где ломается идея, что упущено, каков худший сценарий), но опираются на
цифры проекта, а не на пустой чат.

Это не торговые сигналы. Разбор не ставит ордера и не обходит риск-слой.
"""
from __future__ import annotations

import json

from neft.core.config import ROOT
from neft.core.crypto_feed import Post, fetch_posts, label, relevant
from neft.core.routing import CRYPTO_ROUTES, CRYPTO_WATCHLIST

YEARLY = ROOT / "logs" / "crypto_yearly.json"

COMMANDS = (
    "tldr", "devil", "premortem", "blindspot", "steelman", "ripple",
    "realpc", "compare", "primer", "mentalmodel", "myths", "decisions",
    "analyze",
)


def _yearly() -> dict:
    if not YEARLY.exists():
        return {}
    return json.loads(YEARLY.read_text(encoding="utf-8"))


def _coin_stats(symbol: str) -> dict | None:
    coins = _yearly().get("coins") or {}
    return coins.get(symbol)


def _resolve_symbol(text: str) -> str | None:
    raw = text.strip().upper().replace("USDT", "").replace("/", "").replace(":", "")
    for sym in list(CRYPTO_ROUTES) + list(CRYPTO_WATCHLIST):
        if label(sym) == raw or raw in (label(sym), sym.upper()):
            return sym
        if raw == label(sym):
            return sym
    aliases = {
        "BITCOIN": "BTC/USDT:USDT", "БИТОК": "BTC/USDT:USDT", "BTC": "BTC/USDT:USDT",
        "ETH": "ETH/USDT:USDT", "ЭФИР": "ETH/USDT:USDT",
        "SOL": "SOL/USDT:USDT", "HYPE": "HYPE/USDT:USDT",
        "DOGE": "DOGE/USDT:USDT", "XRP": "XRP/USDT:USDT",
        "BNB": "BNB/USDT:USDT", "BCH": "BCH/USDT:USDT",
        "ZEC": "ZEC/USDT:USDT", "ENA": "ENA/USDT:USDT",
        "SUI": "SUI/USDT:USDT", "PEPE": "1000PEPE/USDT:USDT",
    }
    return aliases.get(raw)


def _facts(block: dict) -> list[str]:
    s = block.get("summary", block)
    pf = s.get("pf")
    pf_s = f"{pf:.2f}" if isinstance(pf, (int, float)) and pf == pf else "—"
    out = [
        f"винрейт {s['win_rate']:.1f}%  просадка {s['dd_estimate']:.1f}%  "
        f"PF {pf_s}  P&L {s.get('pnl', 0):+.0f}$",
    ]
    monthly = block.get("monthly") or {}
    losers = [(m, v) for m, v in monthly.items() if v.get("pnl", 0) < 0]
    losers.sort(key=lambda x: x[1]["pnl"])
    if losers:
        m, v = losers[0]
        out.append(f"худший месяц {m}: {v['win_rate']:.0f}% винрейт, "
                   f"{v['pnl']:+.0f}$ на {v['trades']} сделках")
    wd = block.get("weekday") or {}
    if wd:
        worst = min(wd.items(), key=lambda kv: kv[1].get("pnl", 0))
        out.append(f"слабый день недели — {worst[0]}: "
                   f"{worst[1]['win_rate']:.0f}%, {worst[1]['pnl']:+.0f}$")
    return out


def tldr(posts: list[Post] | None = None, n: int = 8) -> str:
    """Твёрдые факты из ленты: монеты, цифры, без воды и рекламы."""
    posts = relevant(posts if posts is not None else fetch_posts(), ours_only=False)
    lines = ["Crypto Daily + Coin Post + Dashi Finance — факты (реклама и дубли выкинуты):"]
    shown = 0
    for p in reversed(posts):
        if shown >= n:
            break
        coins = ",".join(label(c) for c in p.coins) or "—"
        snippet = p.text.replace("\n", " ")
        if len(snippet) > 220:
            snippet = snippet[:217] + "…"
        when = p.time.strftime("%d.%m %H:%M") if p.time is not None else "?"
        src = {"cryptodaily": "CD", "coin_post": "CP", "dashi_finance": "DF"}.get(p.source, p.source or "?")
        lines.append(f"• {when} {src}  [{coins}]  {snippet}")
        shown += 1
    if shown == 0:
        lines.append("лента пуста или недоступна")
    return "\n".join(lines)


def devil(thesis: str) -> str:
    thesis = thesis.strip() or "торговать крипту по текущей CRYPTO_ROUTES на полном годе"
    lines = [
        "/DEVIL — слабые места тезиса",
        f"тезис: {thesis}",
        "",
        "• Короткое окно (27 дней) в этом проекте уже врало: на годе у BTC "
        "просадка 11.9% при винрейте 53.8% — короткий тест занижает DD.",
        "• Канал пишет прогнозы ($100k–$150k, «дно как в 2022») — это нарратив, "
        "не сетап. У нас вход только по структуре стратегии, не по инфлу.",
        "• USD-новости (CPI/NFP/FOMC) на крипте идут 24/7: фильтр ±5 мин "
        "режет спайк, но не весь день после релиза.",
        "• ENA на годе: винрейт 36.7%, просадка 24.5%, PF 0.99 — машина "
        "может тащить мёртвый маршрут, если не выкинуть его явно.",
        "• Риск 0.5–3% на сделку не спасает от серии: 5–7 стопов подряд "
        "на 0.75% это уже 3.5–5% депозита без учёта спреда.",
    ]
    sym = _resolve_symbol(thesis)
    s = _coin_stats(sym) if sym else None
    if s:
        lines.append("")
        lines.append(f"по {label(sym)} из годового прогона:")
        for r in _facts(s):
            lines.append(f"  — {r}")
    return "\n".join(lines)


def premortem(plan: str) -> str:
    plan = plan.strip() or "включить крипто-машину на демо с риском 0.5%"
    sym = _resolve_symbol(plan)
    lines = [
        "/PREMORTEM — как этот план умирает",
        f"план: {plan}",
        "",
        "1. Первый месяц совпадает с провальным режимом (как март SOL: "
        "9% винрейт) — серия стопов, рука тянется поднять риск выше 3%.",
        "2. Форвард копит 10 сделок, не 100: удачный старт принимается "
        "за готовность к реальному рынку.",
        "3. Новостной гейт не покрывает EUR/GBP; на крипте это слабее, "
        "но FOMC+CPI в одну неделю дают два спайка подряд.",
        "4. Спред на альте в моменте шире кэша — матожидание, посчитанное "
        "на истории, исчезает.",
        "5. Один RiskManager на портфель: дневной лимит срабатывает на "
        "ENA, и BTC в тот же день уже не входит — упускается лучшая нога.",
    ]
    if sym:
        s = _coin_stats(sym)
        if s:
            sm = s["summary"]
            lines += [
                "",
                f"конкретный провал по {label(sym)} (год, риск 0.5%):",
                f"  сделок {sm['trades']}, винрейт {sm['win_rate']:.1f}%, "
                f"просадка {sm['dd_estimate']:.1f}%, P&L {sm['pnl']:+.0f}$",
            ]
            for r in _facts(s):
                lines.append(f"  — {r}")
        elif sym in CRYPTO_WATCHLIST:
            lines.append(f"\n{label(sym)} в watchlist, не в машине: {CRYPTO_WATCHLIST[sym]}")
    return "\n".join(lines)


def blindspot(situation: str) -> str:
    situation = situation.strip() or "крипто-сессия сейчас"
    posts = [p for p in relevant(ours_only=False) if p.kind != "ad"][:5]
    lines = [
        "/BLINDSPOT — что обычно не смотрят",
        f"ситуация: {situation}",
        "",
        "• Ликвидность выходных: сделок много, спред и проскальзывание другие.",
        "• Маршрутизация подобрана на той же истории, на которой мерится — "
        "оптимизм заложен в CRYPTO_ROUTES.",
        "• FRED-фильтр видит только США; канал пишет про ETF/Казначейство — "
        "это не в календаре ForexFactory.",
        "• HYPE/DOGE на годе: просадка 3.9% и 3.5%; BTC/ETH — 11.9% и 14.1% "
        "при том же риске 0.5%. Плюс по монете ≠ плюс по портфелю.",
        "• Пост канала про AI→Pine не переносится 1:1: наш вход — stop-ордер "
        "на следующем баре, комиссии другие, overfitting через Pine-сетку "
        "так же опасен, как и через нашу.",
    ]
    if posts:
        lines.append("")
        lines.append("сейчас в ленте, что легко пропустить:")
        for p in posts[:3]:
            coins = ",".join(label(c) for c in p.coins) or "макро"
            lines.append(f"  [{coins}] {p.text[:160].replace(chr(10), ' ')}")
    return "\n".join(lines)


def steelman(view: str) -> str:
    view = view.strip() or "лонг BTC от текущих"
    bear = any(w in view.lower() for w in ("шорт", "медвед", "слив", "дно", "short"))
    side = "бычью" if bear else "медвежью"
    lines = [
        f"/STEELMAN — сильнейшая {side} аргументация против: {view}",
        "",
    ]
    if not bear:
        lines += [
            "• Медвежий крест 21/200 EMA на BTC уже был в ленте как рифма с 2022.",
            "• Год HSS+S/R по BTC: PF 1.38 при просадке 11.9% — тренд кормит, "
            "но путь к цели депозита ломает.",
            "• Пятница по BTC в годовом разборе отрицательная (−40$), остальные дни плюс.",
            "• После шорт-сквиза кластеры сверху часто сняты — продолжение "
            "без новой ликвидности статистически слабее импульса.",
        ]
    else:
        lines += [
            "• HYPE и DOGE на 350+ днях: винрейт ~59%, просадка <4%, PF >2.5 — "
            "не «весь альт мёртв».",
            "• Август 2026 у BTC в годовом окне +942$ при 64% винрейте — "
            "режим восстановления существует.",
            "• Крипта 24/7: сессионный фильтр 16–19 на M1 почти везде ухудшал "
            "результат — «ночью не торговать» здесь не аксиома.",
        ]
    return "\n".join(lines)


def ripple(event: str) -> str:
    event = event.strip() or "релиз CPI США"
    return "\n".join([
        f"/RIPPLE — каскад от: {event}",
        "",
        "1-й порядок: USD и ставка → XAUUSD, NAS100/DJ30 в ту же минуту.",
        "2-й порядок: риск-он/офф → вся крипта через доллар-ликвидность "
        "(у нас CRYPTO_CURRENCIES = USD). GBP/AUD релизы на BTC почти нулевые.",
        "3-й порядок: если импульс снимает стопы на BTC, альты (ENA, ZEC) "
        "дают более глубокую просадку на том же риске 0.5% — портфельный "
        "дневной лимит может закрыть день раньше, чем отработает HYPE.",
        "Смежные: ETF-потоки и выкуп Treasuries, о которых пишет канал, "
        "не стоят в ForexFactory — живой гейт их не увидит, только лента.",
    ])


def realpc(decision: str) -> str:
    decision = decision.strip() or "торговать все 10 монет vs только HYPE+DOGE+BCH"
    return "\n".join([
        f"/REALPC — честный аудит: {decision}",
        "",
        "Плюсы широкого набора: больше сделок, диверсификация по дням.",
        "Минусы: ENA на годе −1.2% и 24.5% просадки тянет общий счёт вниз; "
        "BTC даёт доход при просадке 11.9% — это другой профиль риска.",
        "Скрытые издержки: спред альта, ночная дыра ForexFactory (только "
        "текущая неделя), время на сопровождение 10 пар.",
        "Альтернатива: HYPE+DOGE (мягче по DD) + BCH (PF 2.01, DD 7%). "
        "Меньше сделок — правило проекта, не жертва.",
        "Асимметрия: оставить ENA стоит глубокой просадки; не взять BTC — "
        "упущенный доход при более спокойном счёте.",
    ])


def compare(pair: str) -> str:
    parts = [x.strip() for x in pair.replace(" vs ", " vs ").split(" vs ") if x.strip()]
    if len(parts) != 2:
        parts = ["BTC", "HYPE"]
    rows = []
    for name in parts:
        sym = _resolve_symbol(name)
        s = _coin_stats(sym) if sym else None
        if not s:
            rows.append((name, None))
        else:
            rows.append((label(sym), s["summary"]))
    lines = [f"/COMPARE — {parts[0]} vs {parts[1]}", ""]
    for name, sm in rows:
        if sm is None:
            lines.append(f"{name}: нет годового прогона (нет в CRYPTO_ROUTES или не считали)")
            continue
        lines.append(
            f"{name}: {sm['trades']} сд, WR {sm['win_rate']:.1f}%, "
            f"PF {sm['pf']:.2f}, DD {sm['dd_estimate']:.1f}%, "
            f"{sm['return_pct']:+.1f}%"
        )
    return "\n".join(lines)


def primer(topic: str) -> str:
    topic = topic.strip() or "машина NEFT на крипте"
    return "\n".join([
        f"/PRIMER — {topic}",
        "",
        "Механика: сигнал стратегии → RiskManager.approve → отложенный стоп → "
        "повторная проверка на заполнении (новости в том числе).",
        "Сайзинг: риск 0.5–3% депозита, не лоты «на глаз».",
        "Крипта: HSS на M1, London S/R чаще на M5; сессия 16–19 на M1 обычно вредна.",
        "Тесты крутятся на $1000 — это единица сравнения, не целевой капитал. "
        "В бою объём считается от реального баланса счёта.",
        "Новости live: ForexFactory thisweek. Ретро: FRED только USD. "
        "Ленты Crypto Daily и Coin Post — брифинг, не гейт.",
    ])


def mentalmodel(concept: str) -> str:
    concept = concept.strip() or "просадка vs доходность"
    return "\n".join([
        f"/MENTALMODEL — {concept}",
        "",
        "Доход — скорость. Просадка — можно ли доехать. "
        "BTC +328% за год при DD 11.9% — быстрый счёт с глубокой ямой. "
        "Это факт года, не verdикт «нельзя торговать».",
        "Винрейт — не «магия стратегии», а доля баров, где структура "
        "совпала с последующим ходом. На коротком окне это удача.",
        "Новостной фильтр — шлагбаум на переезде, не прогноз погоды: "
        "он не говорит, куда пойдёт цена, только «сейчас не входить».",
    ])


def myths(topic: str) -> str:
    topic = topic.strip() or "крипто-скальп"
    return "\n".join([
        f"/MYTHS — {topic}",
        "",
        "• «Инфл назвал $150k — значит сетап есть». Нет, это нарратив ленты.",
        "• «AI напишет Pine и стратегия готова». Без издержек, next-bar fill "
        "и запрета подгона сеткой — это overfitting в красивой обёртке.",
        "• «Год в плюсе = можно на реал». Сначала форвард с ордерами на демо, "
        "потом DEMO_ONLY=false и --live. Плюс на истории не равен исполнению.",
        "• «Крипта 24/7, новости FX не важны». NFP/FOMC двигают BTC. "
        "AUD employment — почти нет.",
        "• «Больше пар — безопаснее». ENA в широком наборе ломает DD.",
    ])


def decisions(notes: str) -> str:
    notes = notes.strip() or "(пустые заметки — заполните вход, триггер, инвалидацию)"
    entry = {
        "notes": notes,
        "entry": None,
        "trigger": None,
        "invalidation": None,
        "risk_pct": None,
        "errors": [],
    }
    path = ROOT / "data" / "trade_journal.jsonl"
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"raw": notes, "parsed": entry}, ensure_ascii=False) + "\n")
    return "\n".join([
        "/DECISIONS — запись в торговый дневник",
        f"заметка: {notes}",
        "",
        "структура, которую надо дописать руками, если её нет в тексте:",
        "  вход / триггер / уровень инвалидации / риск % / ошибка прошлой сделки",
        f"сохранено: {path}",
        "ордер из этой команды НЕ выставляется.",
    ])


def analyze() -> str:
    """Три вопроса из первого #AItrade-поста — по годовому крипто-прогону."""
    data = _yearly()
    if not data:
        return "нет logs/crypto_yearly.json — сначала годовой прогон крипты"
    lines = [
        "Разбор бэктеста (вместо Pine/TradingView — наш годовой прогон, риск 0.5%)",
        "",
        "1. Где стратегия теряла деньги",
    ]
    for sym, block in (data.get("coins") or {}).items():
        sm = block["summary"]
        losers = [(m, v) for m, v in (block.get("monthly") or {}).items() if v["pnl"] < 0]
        if not losers:
            continue
        losers.sort(key=lambda x: x[1]["pnl"])
        m, v = losers[0]
        lines.append(f"  {label(sym)}: {m}  WR {v['win_rate']:.0f}%  {v['pnl']:+.0f}$")
    lines += ["", "2. Что раздуло просадку"]
    ranked = sorted(
        ((label(s), b["summary"]["dd_estimate"], b["summary"]["win_rate"])
         for s, b in (data.get("coins") or {}).items()),
        key=lambda x: -x[1],
    )
    for name, dd, wr in ranked[:5]:
        lines.append(f"  {name}: DD {dd:.1f}%  WR {wr:.1f}%")
    lines += [
        "",
        "3. Без подгона под одну выборку",
        "  — не крутить RR/сетку на той же истории: маршрутизация уже "
        "подобрана здесь, новый перебор = overfitting.",
        "  — ENA: PF≈1, DD 24.5% — слабое звено широкого набора.",
        "  — риск 0.5–3% на сделку остаётся правилом сайзинга; "
        "поднимать его «чтобы догнать» на широком портфеле опасно.",
        "  — новостной гейт для live/форварда; $1000 в тестах — линейка, "
        "боевой объём от баланса счёта.",
    ]
    return "\n".join(lines)


DISPATCH = {
    "tldr": lambda arg: tldr(),
    "devil": devil,
    "premortem": premortem,
    "blindspot": blindspot,
    "steelman": steelman,
    "ripple": ripple,
    "realpc": realpc,
    "compare": compare,
    "primer": primer,
    "mentalmodel": mentalmodel,
    "myths": myths,
    "decisions": decisions,
    "analyze": lambda arg: analyze(),
}
