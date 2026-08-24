"""Конфигурация из .env. Секреты в код не попадают — только сюда."""
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Боевой/основной терминал — источник котировок наших инструментов.
    mt5_login: int | None = None
    mt5_password: str | None = None
    mt5_server: str | None = None
    mt5_path: str | None = None

    # Второй терминал под демо. Один Python-процесс видит один счёт,
    # поэтому для параллельной работы нужна отдельная установка.
    mt5_demo_login: int | None = None
    mt5_demo_password: str | None = None
    mt5_demo_server: str | None = None
    mt5_demo_path: str | None = None

    # Бесплатная регистрация: https://fredaccount.stlouisfed.org/login/secure/
    # Нужен только для ретроспективной проверки новостного фильтра на бэктестах
    # (neft/core/news_historical.py) — живой фид (news.py) ключа не требует.
    fred_api_key: str | None = None

    def mt5_kwargs(self, demo: bool = False, force_login: bool = False) -> dict:
        """Параметры подключения для нужного терминала.

        ВАЖНО: аргумент path у mt5.initialize() не «выбирает, к чему
        подключиться», а ЗАПУСКАЕТ терминал по этому пути со своим последним
        счётом. Если передать его, когда терминал уже открыт на другом счёте,
        мы молча попадём не туда.

        Более того, передача login/password ПЕРЕКЛЮЧАЕТ уже открытый терминал
        на этот счёт. Если человек вручную выбрал демо, а в конфиге записан
        боевой логин, мы молча вернём терминал на боевой счёт.

        Поэтому по умолчанию возвращаем пустой словарь: initialize() без
        аргументов подключается к тому счёту, который открыт в терминале.
        Принудительный вход — только по явному force_login (например, на VPS,
        где терминал поднимается автоматически).
        """
        if not force_login:
            return {}
        pref = "mt5_demo_" if demo else "mt5_"
        login = getattr(self, pref + "login")
        if not login:
            return {}
        out = {
            "login": int(login),
            "password": getattr(self, pref + "password"),
            "server": getattr(self, pref + "server"),
        }
        path = getattr(self, pref + "path")
        if path:
            out["path"] = path
        return out

    binance_api_key: str | None = None
    binance_api_secret: str | None = None
    bybit_api_key: str | None = None
    bybit_api_secret: str | None = None

    # Предохранители
    demo_only: bool = True
    crypto_testnet: bool = True
    admin_panel_token: str | None = None

    # Telegram-бот панели (scripts/telegram_bot.py) — токен от @BotFather и
    # список Telegram user id через запятую, кому отвечает бот. Пока только
    # статус на чтение, без управления ботом.
    telegram_bot_token: str | None = None
    telegram_allowed_ids: str | None = None
    telegram_chat_id: str | None = None  # можно вместо/вместе с telegram_allowed_ids

    # Для /api/narrative — проверка новостного нарратива по кнопке в панели
    # перед входом в сделку (scripts/admin_server.py). Без ключа кнопка
    # просто вернёт понятную ошибку, ни на что другое не влияет.
    anthropic_api_key: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        """Незаполненная строка в .env значит «не задано», а не пустое значение."""
        return None if isinstance(v, str) and not v.strip() else v


settings = Settings()
