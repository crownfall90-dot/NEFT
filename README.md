# NEFT — торговый бот

Единый каркас для двух площадок: **MT5** (форекс/CFD у Bybit) и **крипта** (Binance,
Bybit через ccxt). Стратегия пишется один раз и работает на обеих — она общается
только с интерфейсом `Broker` и не знает, что под ней.

## Структура

```
neft/core/config.py       чтение .env
neft/core/models.py       Quote, Account, Position, Side — общий язык площадок
neft/core/broker.py       абстрактный интерфейс площадки
neft/adapters/mt5_broker.py     MT5 (форекс/CFD)
neft/adapters/crypto_broker.py  Binance / Bybit через ccxt
scripts/check_connection.py     диагностика, ничего не торгует
```

## Установка

Самый быстрый способ — двойной клик по `setup.bat` (или запуск из терминала):

```bat
setup.bat
```

Скрипт сам создаст `.venv`, поставит зависимости из `requirements.txt`,
скопирует `.env.example` → `.env` (если его ещё нет — существующий не
трогает) и откроет его в Блокноте, чтобы вписать ключи. Ничего не ломает
при повторном запуске — можно гонять сколько угодно раз.

После установки — панель:

```bat
start_neft_services.bat
```

Откроется на http://127.0.0.1:8787 (логин — код из `data/panel_token`,
создаётся сам при первом запуске).

### Вручную (если нужен контроль над каждым шагом)

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env          # заполнить ключи
./.venv/Scripts/python.exe scripts/check_connection.py
```

## Предохранители

В `.env`:

- `DEMO_ONLY=true` — бот откажется отправлять ордера на боевой счёт. Снимать
  только после успешного прогона на демо.
- `CRYPTO_TESTNET=true` — Binance/Bybit работают в песочнице. Ключи тестнета
  отдельные: testnet.binance.vision и testnet.bybit.com, боевые там не подойдут.

## Особенности Bybit CFD (проверено на счёте 6057723)

- Демо-серверов у Bybit нет: терминал пишет `no demo/preliminary groups`.
  Проверка бота по CFD (NAS100, XAUUSD+, …): терминал на **Bybit-Live**,
  режим панели **Демо/Тест** (= paper, ордера не уходят). MetaQuotes-Demo
  для отладки кода без Bybit-тикеров — не для полного CFD-набора.
- Торгуются **только символы с `+`**: `XAUUSD+`, `EURUSD+`. Тикеры без плюса
  видны в списке, но имеют `trade_mode=0` — брокер их заблокировал.
- Счёт в режиме hedging, плечо 1:500, валюта `UST`.
- После `symbol_select()` первый тик приходит с задержкой — адаптер ждёт его сам.
- Кнопка **Алготрейдинг** в терминале должна быть включена, иначе `order_send`
  вернёт отказ.
