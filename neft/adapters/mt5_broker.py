"""MT5: форекс/CFD (Bybit CFD, MetaQuotes-Demo и любой другой сервер).

Подключается к УЖЕ ЗАПУЩЕННОМУ терминалу. Если в .env заданы логин/пароль/сервер —
переключает терминал на этот счёт.
"""
import logging
import time

import MetaTrader5 as mt5

from neft.core.broker import Broker
from neft.core.config import settings
from neft.core.models import Account, BrokerError, Position, Quote, Side

log = logging.getLogger(__name__)

_TRADE_MODE = {0: "DEMO", 1: "CONTEST", 2: "REAL"}


class MT5Broker(Broker):
    name = "mt5"

    def __init__(self, demo_only: bool | None = None):
        self.demo_only = settings.demo_only if demo_only is None else demo_only
        self._connected = False

    # ── жизненный цикл ───────────────────────────────────────────────
    def connect(self) -> Account:
        # См. Settings.mt5_kwargs: path запускает терминал, а не выбирает его.
        if not mt5.initialize(**settings.mt5_kwargs()):
            raise BrokerError(f"MT5 initialize failed: {mt5.last_error()}")
        self._connected = True

        term = mt5.terminal_info()
        if not term.connected:
            raise BrokerError("Терминал не подключён к серверу брокера")
        if not term.trade_allowed:
            log.warning(
                "Алготрейдинг выключен в терминале — ордера не пройдут. "
                "Включите кнопку 'Алготрейдинг' (Сервис → Настройки → Советники)."
            )

        acc = self.account()
        log.info(
            "MT5 %s | %s | %s | %.2f %s",
            acc.login, acc.server, "DEMO" if acc.is_demo else "REAL",
            acc.balance, acc.currency,
        )
        if not acc.is_demo and self.demo_only:
            raise BrokerError(
                f"Счёт {acc.login} на {acc.server} — БОЕВОЙ, а DEMO_ONLY=true. "
                "Подключите демо-счёт либо снимите предохранитель в .env осознанно."
            )
        return acc

    def disconnect(self) -> None:
        if self._connected:
            mt5.shutdown()
            self._connected = False

    # ── чтение ───────────────────────────────────────────────────────
    def account(self) -> Account:
        a = mt5.account_info()
        if a is None:
            raise BrokerError(f"Нет данных счёта: {mt5.last_error()}")
        return Account(
            login=str(a.login),
            server=a.server,
            currency=a.currency,
            balance=a.balance,
            equity=a.equity,
            is_demo=a.trade_mode != 2,
            available=float(getattr(a, "margin_free", 0) or 0),
            used_margin=float(getattr(a, "margin", 0) or 0),
            wallet=float(a.balance),
        )

    def quote(self, symbol: str) -> Quote:
        info = mt5.symbol_info(symbol)
        if info is None:
            raise BrokerError(f"Символ {symbol} не найден у брокера")
        # Символ обязан быть в Market Watch, иначе котировки нулевые.
        if not info.visible:
            if not mt5.symbol_select(symbol, True):
                raise BrokerError(f"Не удалось добавить {symbol} в Market Watch")
            # После подписки первый тик приходит не мгновенно.
            for _ in range(10):
                t = mt5.symbol_info_tick(symbol)
                if t and t.bid:
                    break
                time.sleep(0.3)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or not tick.bid:
            raise BrokerError(f"Нет котировок по {symbol} (рынок закрыт?)")
        return Quote(symbol=symbol, bid=tick.bid, ask=tick.ask)

    def is_tradable(self, symbol: str) -> bool:
        """У Bybit CFD торгуются только символы с '+'; остальные видны, но заблокированы."""
        info = mt5.symbol_info(symbol)
        return bool(info and info.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL)

    def positions(self) -> list[Position]:
        return [
            Position(
                symbol=p.symbol,
                side=Side.BUY if p.type == mt5.POSITION_TYPE_BUY else Side.SELL,
                volume=p.volume,
                entry=p.price_open,
                pnl=p.profit,
                ticket=str(p.ticket),
            )
            for p in (mt5.positions_get() or ())
        ]

    def symbols(self, pattern: str = "*") -> list[str]:
        return [s.name for s in (mt5.symbols_get(pattern) or ())]

    # ── торговля ─────────────────────────────────────────────────────
    def market_order(
        self,
        symbol: str,
        side: Side,
        volume: float,
        sl: float | None = None,
        tp: float | None = None,
        comment: str = "neft",
    ) -> str:
        acc = self.account()
        if not acc.is_demo and self.demo_only:
            raise BrokerError("Отказ: боевой счёт при DEMO_ONLY=true")

        if not self.is_tradable(symbol):
            raise BrokerError(f"{symbol}: торговля запрещена брокером (trade_mode)")
        q = self.quote(symbol)
        info = mt5.symbol_info(symbol)
        volume = self._round_volume(volume, info)

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY if side is Side.BUY else mt5.ORDER_TYPE_SELL,
            "price": q.ask if side is Side.BUY else q.bid,
            "deviation": 20,  # допустимое проскальзывание в пунктах
            "magic": 20260821,  # метка бота — чтобы отличать свои сделки от ручных
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(info),
        }
        if sl:
            req["sl"] = sl
        if tp:
            req["tp"] = tp

        res = mt5.order_send(req)
        if res is None:
            raise BrokerError(f"order_send вернул None: {mt5.last_error()}")
        if res.retcode != mt5.TRADE_RETCODE_DONE:
            raise BrokerError(f"Ордер отклонён: retcode={res.retcode} {res.comment}")
        log.info("%s %s %.2f @ %.5f -> #%s", side.value, symbol, volume, res.price, res.deal)
        return str(res.deal)

    @staticmethod
    def _round_volume(volume: float, info) -> float:
        """Объём обязан попадать в шаг лота брокера, иначе отказ."""
        step = info.volume_step
        volume = round(round(volume / step) * step, 8)
        return max(info.volume_min, min(volume, info.volume_max))

    @staticmethod
    def _filling_mode(info) -> int:
        """Режим исполнения различается у брокеров; берём поддерживаемый."""
        allowed = info.filling_mode
        if allowed & 1:
            return mt5.ORDER_FILLING_FOK
        if allowed & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN
