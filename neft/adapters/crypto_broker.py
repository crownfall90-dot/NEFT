"""Крипта через ccxt: только Bybit (субаккаунт по ключам из .env).

Ключи тестнета / демо и боя разные. Для боя — ключи торгового субаккаунта UTA.
"""
import logging

import ccxt

from neft.core.broker import Broker
from neft.core.config import settings
from neft.core.models import Account, BrokerError, Position, Quote, Side

log = logging.getLogger(__name__)

# Единственная поддерживаемая биржа.
EXCHANGE_ID = "bybit"


class CryptoBroker(Broker):
    """Bybit USDT-perpetual (swap), unified / субаккаунт."""

    def __init__(self, exchange_id: str = EXCHANGE_ID, testnet: bool | None = None,
                 demo: bool = False, leverage: int | None = None):
        """demo=True — демо Bybit (api-demo.bybit.com).

        leverage — целевое плечо на символе перед входом (None = не трогать).
        exchange_id игнорируется, если не bybit — всегда Bybit.
        """
        if exchange_id and exchange_id != EXCHANGE_ID:
            log.warning("биржа %s отключена — только bybit", exchange_id)
        self.name = EXCHANGE_ID
        self.demo = demo
        self.leverage = int(leverage) if leverage else None
        self._lev_ok: dict[str, int] = {}
        self.testnet = False if demo else (
            settings.crypto_testnet if testnet is None else testnet)
        key = settings.bybit_api_key
        secret = settings.bybit_api_secret
        if not key or not secret:
            log.warning("bybit: ключи не заданы — только публичные данные")

        self.ex = ccxt.bybit({
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
                "accountType": "UNIFIED",  # UTA / субаккаунт
            },
        })
        if self.demo:
            if not hasattr(self.ex, "enable_demo_trading"):
                raise BrokerError("bybit: демо-режим не поддерживается ccxt")
            self.ex.enable_demo_trading(True)
        elif self.testnet:
            self.ex.set_sandbox_mode(True)

    def connect(self) -> Account:
        self.ex.load_markets()
        mode = "DEMO" if self.demo else ("TESTNET" if self.testnet else "LIVE")
        log.info("%s %s | рынков: %d", self.name, mode, len(self.ex.markets))
        if not self.ex.apiKey:
            return Account(self.name, "public", "USDT", 0.0, 0.0,
                           self.demo or self.testnet)
        return self.account()

    def disconnect(self) -> None:
        close = getattr(self.ex, "close", None)
        if close:
            close()

    def account(self) -> Account:
        """Торговый баланс субаккаунта (USDT на UTA / swap)."""
        try:
            try:
                bal = self.ex.fetch_balance({"type": "swap", "accountType": "UNIFIED"})
            except Exception:  # noqa: BLE001
                bal = self.ex.fetch_balance({"type": "swap"})
        except ccxt.AuthenticationError as e:
            raise BrokerError(f"{self.name}: ключи отклонены — {e}") from e
        usdt = bal.get("USDT") or {}
        total = float(usdt.get("total") or usdt.get("free") or 0)
        free = float(usdt.get("free") or total)
        equity = total
        wallet = total
        used = 0.0
        info = bal.get("info") if isinstance(bal.get("info"), dict) else {}
        # Bybit v5: totalEquity / available / initial margin в result.list[0]
        try:
            lst = (info.get("result") or {}).get("list") or []
            if lst:
                row = lst[0]
                if row.get("totalEquity"):
                    equity = float(row["totalEquity"])
                if row.get("totalWalletBalance"):
                    wallet = float(row["totalWalletBalance"])
                elif row.get("totalEquity"):
                    wallet = equity
                for key in ("totalAvailableBalance", "totalMarginBalance",
                            "availableBalance"):
                    if row.get(key) not in (None, ""):
                        free = float(row[key])
                        break
                for key in ("totalInitialMargin", "totalPositionIM",
                            "totalOrderIM"):
                    if row.get(key) not in (None, ""):
                        used = max(used, float(row[key]))
        except (TypeError, ValueError, KeyError, IndexError):
            pass
        if used <= 0 and equity > 0 and free >= 0:
            used = max(0.0, equity - free)
        return Account(
            login=self.name,
            server="demo" if self.demo else ("testnet" if self.testnet else "live"),
            currency="USDT",
            balance=free if free > 0 else equity,
            equity=equity,
            is_demo=self.demo or self.testnet,
            available=free if free > 0 else equity,
            used_margin=used,
            wallet=wallet if wallet > 0 else equity,
        )

    def quote(self, symbol: str) -> Quote:
        t = self.ex.fetch_ticker(symbol)
        bid, ask = t.get("bid"), t.get("ask")
        if not bid or not ask:
            # Binance отдаёт свопы через 24h-тикер без bid/ask — берём вершину стакана.
            # limit=5: Binance futures принимает только 5/10/20/50/100/500/1000.
            ob = self.ex.fetch_order_book(symbol, limit=5)
            if not ob["bids"] or not ob["asks"]:
                raise BrokerError(f"{self.name}: нет котировок по {symbol}")
            bid, ask = ob["bids"][0][0], ob["asks"][0][0]
        return Quote(symbol=symbol, bid=float(bid), ask=float(ask))

    def positions(self) -> list[Position]:
        out = []
        for p in self.ex.fetch_positions():
            size = float(p.get("contracts") or 0)
            if not size:
                continue
            out.append(Position(
                symbol=p["symbol"],
                side=Side.BUY if p["side"] == "long" else Side.SELL,
                volume=size,
                entry=float(p.get("entryPrice") or 0),
                pnl=float(p.get("unrealizedPnl") or 0),
                ticket=str(p.get("id") or p["symbol"]),
            ))
        return out

    def request_demo_funds(self, coin: str = "USDT", amount: str = "100000") -> dict:
        """Пополнить демо-счёт виртуальными средствами."""
        if not self.demo:
            raise BrokerError("доступно только в демо-режиме")
        return self.ex.privatePostV5AccountDemoApplyMoney(
            {"adjustType": 0, "utaDemoApplyMoney": [
                {"coin": coin, "amountStr": amount}]})

    def _max_leverage(self, symbol: str) -> int | None:
        m = self.ex.markets.get(symbol) or {}
        lim = (m.get("limits") or {}).get("leverage") or {}
        mx = lim.get("max")
        return int(mx) if mx else None

    def ensure_leverage(self, symbol: str, leverage: int | None = None) -> int | None:
        """Выставить плечо на символе (кэш: повторно не дергаем биржу)."""
        lev = int(leverage if leverage is not None else (self.leverage or 0))
        if lev <= 0:
            return None
        mx = self._max_leverage(symbol)
        if mx and lev > mx:
            log.warning("%s %s: плечо %dx > max %dx — режем до max",
                        self.name, symbol, lev, mx)
            lev = mx
        if self._lev_ok.get(symbol) == lev:
            return lev
        try:
            params = {}
            if self.name == "bybit":
                # unified: часто требуют оба плеча
                params = {"buyLeverage": str(lev), "sellLeverage": str(lev)}
            self.ex.set_leverage(lev, symbol, params)
            self._lev_ok[symbol] = lev
            log.info("%s %s: плечо %dx", self.name, symbol, lev)
            return lev
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            # уже стоит нужное / «leverage not modified» — ок
            if any(x in msg for x in (
                    "not modified", "same leverage", "leverage not changed",
                    "no need to change", "110043")):
                self._lev_ok[symbol] = lev
                log.info("%s %s: плечо уже %dx", self.name, symbol, lev)
                return lev
            raise BrokerError(f"{self.name}: set_leverage {symbol} {lev}x — {e}") from e

    def market_order(self, symbol, side, volume, sl=None, tp=None, comment="neft",
                     leverage: int | None = None) -> str:
        if not (self.testnet or self.demo) and settings.demo_only:
            raise BrokerError("Отказ: боевая биржа при DEMO_ONLY=true")
        self.ensure_leverage(symbol, leverage)
        params = {}
        if sl:
            params["stopLoss"] = sl
        if tp:
            params["takeProfit"] = tp
        order = self.ex.create_order(symbol, "market", side.value, volume, None, params)
        log.info("%s %s %s %s -> #%s", self.name, side.value, symbol, volume, order["id"])
        return str(order["id"])
