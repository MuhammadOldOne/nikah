"""Application settings loaded from environment variables and an optional .env file."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEV_SECRET_KEY = "dev-insecure-secret-key-do-not-use-in-production"
MIN_SECRET_KEY_LENGTH = 32
_ID_SEPARATORS = re.compile(r"[\s,;]+")


def _blank_to_none(value: Any) -> Any:
    return None if isinstance(value, str) and not value.strip() else value


class Settings(BaseSettings):
    """Settings are read once per process; field names match the environment variables.

    After validation the fields hold *effective* values: DEV_LOGIN_ENABLED is always
    False outside ENV=dev and SESSION_COOKIE_SECURE defaults to True in prod.

    The legacy bot's variable names are accepted so its .env can be reused as is: BOT_TOKEN for
    TELEGRAM_TOKEN and ADMIN_IDS for ADMIN_TELEGRAM_IDS. A non-empty new name wins; after
    validation the new fields hold the effective values (do not read the legacy ones).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ENV: Literal["dev", "prod"] = "dev"
    DATABASE_URL: str = "postgresql+asyncpg://nikah:nikah@127.0.0.1:5432/nikah"
    TELEGRAM_TOKEN: SecretStr = SecretStr("")
    BOT_USERNAME: str = "nikah_istina_bot"
    SITE_URL: str = "http://localhost:8000"
    SECRET_KEY: SecretStr = SecretStr(DEV_SECRET_KEY)
    ADMIN_TELEGRAM_IDS: Annotated[list[int], NoDecode] = Field(default_factory=list)
    PREMODERATION: bool = True
    INTEREST_DAILY_LIMIT: int = Field(default=10, ge=1)
    DEV_LOGIN_ENABLED: bool = False
    TELEGRAM_WIDGET_ENABLED: bool = False
    SESSION_COOKIE_SECURE: bool | None = None
    # Open conversations per person; accepting an interest is refused at the limit.
    MAX_OPEN_CONVERSATIONS: int = Field(default=1, ge=1)
    # Chat messages per person per minute, the site and Telegram together.
    CHAT_RATE_LIMIT_PER_MIN: int = Field(default=20, ge=1)
    # Telegram chat (usually a group, negative id) that receives copies of every chat message and the
    # witness notices. Unset: each admin receives them in a private chat.
    WITNESS_CHAT_ID: int | None = None
    # Proxy for api.telegram.org (http://, socks5:// or socks4://; the bot rejects https:// at start);
    # may contain credentials: never log it.
    TELEGRAM_PROXY: SecretStr | None = None
    # Legacy names (the old bot's .env), folded into TELEGRAM_TOKEN / ADMIN_TELEGRAM_IDS.
    BOT_TOKEN: SecretStr = SecretStr("")
    ADMIN_IDS: Annotated[list[int], NoDecode] = Field(default_factory=list)

    @field_validator("ADMIN_TELEGRAM_IDS", "ADMIN_IDS", mode="before")
    @classmethod
    def _split_admin_ids(cls, value: Any) -> Any:
        """``111,222`` (also with spaces or semicolons, as the legacy bot accepted)."""
        if isinstance(value, str):
            return [int(part) for part in _ID_SEPARATORS.split(value) if part]
        if isinstance(value, int):
            return [value]
        return value

    @field_validator("WITNESS_CHAT_ID", "TELEGRAM_PROXY", mode="before")
    @classmethod
    def _optional(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("SITE_URL")
    @classmethod
    def _strip_site_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("BOT_USERNAME")
    @classmethod
    def _strip_bot_username(cls, value: str) -> str:
        return value.lstrip("@")

    @model_validator(mode="after")
    def _apply_environment_rules(self) -> Settings:
        if not self.TELEGRAM_TOKEN.get_secret_value().strip():
            self.TELEGRAM_TOKEN = SecretStr(self.BOT_TOKEN.get_secret_value().strip())
        if not self.ADMIN_TELEGRAM_IDS:
            self.ADMIN_TELEGRAM_IDS = list(self.ADMIN_IDS)
        is_prod = self.ENV == "prod"
        secret = self.SECRET_KEY.get_secret_value()
        if not secret and not is_prod:
            self.SECRET_KEY = SecretStr(DEV_SECRET_KEY)
        if is_prod and (secret == DEV_SECRET_KEY or len(secret) < MIN_SECRET_KEY_LENGTH):
            raise ValueError(
                f"SECRET_KEY must be set to a random value of at least {MIN_SECRET_KEY_LENGTH} characters when ENV=prod"
            )
        # The dev key is public: anyone could sign a session cookie. Fail closed on a real (https)
        # site that runs in dev mode by mistake, e.g. when ENV is missing from .env.
        if self.SECRET_KEY.get_secret_value() == DEV_SECRET_KEY and self.SITE_URL.startswith("https://"):
            raise ValueError(
                "the development SECRET_KEY cannot be used for an https:// SITE_URL: set ENV=prod and SECRET_KEY"
            )
        self.DEV_LOGIN_ENABLED = self.DEV_LOGIN_ENABLED and not is_prod
        if self.SESSION_COOKIE_SECURE is None:
            self.SESSION_COOKIE_SECURE = is_prod
        return self

    @property
    def is_prod(self) -> bool:
        return self.ENV == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
