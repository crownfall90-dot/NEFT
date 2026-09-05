"""Мейкерское исполнение для BTC up/down 5m: вход лимитным ордером.

ЗАЧЕМ. Замер живого рынка predict.fun (scripts/predict_spread.py) показал:
медианный ask 0.55 при bid 0.46. Покупка по рынку (тейкером) даёт порог
безубытка 55.8% при винрейте стратегии 52.3% — то есть гарантированный
минус 3.5 п.п. на сделке. Цена 0.46, на которой строился весь бэктест, —
это bid, цена ПРОДАЖИ, по ней купить нельзя.

Единственный способ получить положительное ожидание — вставать мейкером:
лимитный ордер по своей цене, комиссия 0%, плюс ребейт 25% от тейкерской.

ЭКОНОМИКА (scripts/updown_maker.py, винрейт 52.3%):
    цена лимитника   перевес мейкера   EV на сделку
        0.46             +6.51 п.п.       +14.2%
        0.50             +2.52 п.п.        +5.1%
        0.52             +0.52 п.п.        +1.0%
        0.53             -0.49 п.п.        убыток
Предел прибыльности — 0.52. Выше ставить нельзя ни при каких условиях.

ГЛАВНЫЙ РИСК — НЕИСПОЛНЕНИЕ. Лимитник по 0.46 при рыночном ask 0.55
исполнится только если цена придёт к нам. Неисполненная заявка убытка не
приносит, но сокращает число сделок: при исполнении 50% выходит ~1.68x
за месяц, при 10% — ~1.11x.

ВАЖНО ПРО ЭКСПИРАЦИЮ. Заявку нужно снимать до конца окна: если она
исполнится за секунды до расчёта, входа по сути не было, а риск полный.
Отсюда cancel_before_sec.
"""
from __future__ import annotations

from dataclasses import dataclass

# Предел, выше которого мейкерский вход убыточен при винрейте ~52.3%.
MAX_MAKER_PRICE = 0.52
REBATE_SHARE = 0.25


def taker_fee(price: float, discount: float = 0.10) -> float:
    return 0.02 * min(price, 1 - price) * (1 - discount)


def maker_credit(price: float) -> float:
    """Ребейт мейкеру — 25% от тейкерской комиссии на этом уровне."""
    return taker_fee(price) * REBATE_SHARE


def effective_cost(price: float) -> float:
    """Во сколько реально обходится контракт мейкеру, с учётом ребейта."""
    return price - maker_credit(price)


def breakeven_wr(price: float) -> float:
    """Винрейт, при котором вход по этой цене выходит в ноль, %."""
    return effective_cost(price) * 100


def edge_pp(price: float, win_rate: float) -> float:
    """Перевес в процентных пунктах."""
    return win_rate - breakeven_wr(price)


@dataclass
class MakerOrder:
    side: str          # "Up" | "Down"
    price: float       # цена лимитной заявки
    size_usd: float    # объём в долларах
    market_id: int
    cancel_before_sec: int   # снять заявку за N секунд до экспирации


class MakerExecution:
    """Выбор цены лимитной заявки под целевой винрейт.

    Не размещает ордера — только считает, по какой цене имеет смысл
    вставать и стоит ли вообще входить при текущем стакане.
    """

    def __init__(
        self,
        win_rate: float = 52.3,
        min_edge_pp: float = 1.0,      # не входим ради перевеса меньше 1 п.п.
        cancel_before_sec: int = 45,
        max_price: float = MAX_MAKER_PRICE,
    ):
        self.win_rate = win_rate
        self.min_edge_pp = min_edge_pp
        self.cancel_before_sec = cancel_before_sec
        self.max_price = max_price

    def limit_price(self) -> float:
        """Самая высокая цена, при которой перевес ещё >= min_edge_pp.

        Ставить дешевле — выгоднее по EV, но заявка почти не исполнится.
        Ставить дороже — перевес тает. Берём границу приемлемого.
        """
        lo, hi = 0.30, self.max_price
        for _ in range(60):
            mid = (lo + hi) / 2
            if edge_pp(mid, self.win_rate) >= self.min_edge_pp:
                lo = mid
            else:
                hi = mid
        # Округление вверх может увести цену за порог перевеса —
        # цены на площадке кратны центу, поэтому округляем вниз.
        price = int(lo * 100) / 100
        while price > 0.30 and edge_pp(price, self.win_rate) < self.min_edge_pp:
            price = round(price - 0.01, 2)
        return price

    def plan(self, side: str, market_id: int, size_usd: float,
             best_ask: float | None, best_bid: float | None) -> MakerOrder | None:
        """Заявка на вход, или None если входить не стоит.

        Если рынок уже даёт ask дешевле нашего лимита — берём по рынку
        (тейкером), но только когда перевес это оправдывает.
        """
        want = self.limit_price()

        # Рынок сам предлагает дешевле — проверяем тейкерский вариант.
        if best_ask is not None and best_ask <= want:
            cost = best_ask + taker_fee(best_ask)
            if (self.win_rate - cost * 100) >= self.min_edge_pp:
                return MakerOrder(side, best_ask, size_usd, market_id,
                                  self.cancel_before_sec)

        # Иначе встаём лимитником, но не выше текущего bid+шаг:
        # заявка выше bid просто улучшит лучшую цену и будет ждать.
        price = want
        if best_bid is not None:
            price = min(want, round(best_bid + 0.01, 2))
        if price > self.max_price or edge_pp(price, self.win_rate) < self.min_edge_pp:
            return None
        return MakerOrder(side, price, size_usd, market_id,
                          self.cancel_before_sec)
