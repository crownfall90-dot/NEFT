# NEFT — торговый бот

Единый каркас для двух площадок: **MT5** (форекс/CFD у Bybit) и **крипта** (Binance,
Bybit через ccxt). Стратегия пишется один раз и работает на обеих — она общается
только с интерфейсом `Broker` и не знает, что под ней.

## Быстрый старт на новом ПК

1. Клонируйте репозиторий и откройте папку проекта.
2. Двойной клик **`УСТАНОВКА.bat`**.
   - Создаёт `.venv`, ставит `requirements.txt`
   - Копирует `.env.example` → `.env` (если `.env` ещё нет)
   - Создаёт `logs/`, `data/crypto/`, `data/updown/`
3. Проверьте в `.env` пути **`MT5_PATH`** и **`MT5_DEMO_PATH`** под вашу установку MT5.
4. Запустите MT5, включите **Алготрейдинг**.
5. **`ЗАПУСК.bat`** — панель и Telegram-бот.
6. Откройте http://127.0.0.1:8787 — код входа в `data/panel_token` (создаётся при первом запуске).

Автозапуска нет: после перезагрузки ПК сервисы сами не поднимутся,
нужно снова нажать `ЗАПУСК.bat`. Остановить — `ОСТАНОВКА.bat`,
перезапустить — `РЕСТАРТ.bat`.

> `.env.example` содержит рабочие ключи владельца — репозиторий должен оставаться **приватным**.

## Панель и бэктест CFD

- **Стратегии** — включить/выключить HSS, SonikPulse, ApexShot и др.
- **Бэктест 90д** — прогон на всех CFD из конфига (нужен MT5 с историей M1).
- Отчёт с графиками: http://127.0.0.1:8787/hss_report.html

CLI:

```bash
.venv\Scripts\python.exe scripts\strategy_backtest.py --strategy hss --days 90
.venv\Scripts\python.exe scripts\strategy_backtest.py --strategy apex_shot --days 90
```

## Структура

```
neft/strategies/          стратегии (HSS, SonikPulse, ApexShot, …)
neft/strategies/cfd_factory.py   сборка CFD-стратегий для бэктеста
scripts/strategy_backtest.py     бэктест 90д + отчёт
scripts/admin_server.py          панель управления
scripts/forward.py               live/paper CFD
scripts/crypto_forward.py        live/paper крипта
УСТАНОВКА.bat                    установщик (install_neft.ps1)
ЗАПУСК / ОСТАНОВКА / РЕСТАРТ     управление сервисами
.env.example                     шаблон окружения (→ .env при setup)
data/bot_config.json             настройки бота и стратегий
```

## Установка вручную

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python.exe scripts\check_connection.py
```

## Предохранители

В `.env`:

- `DEMO_ONLY=true` — бот откажется отправлять ордера на боевой счёт.
- `CRYPTO_TESTNET=true` — Binance/Bybit в песочнице (отдельные ключи testnet).

## Bybit CFD

- Демо-серверов у Bybit нет: CFD-тест = **paper** на Bybit-Live (ордера не уходят).
- Торгуются символы с **`+`**: `XAUUSD+`, `EURUSD+`, `NAS100`, …
- Кнопка **Алготрейдинг** в MT5 должна быть включена.
