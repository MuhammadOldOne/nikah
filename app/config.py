"""Application settings loaded from environment variables and an optional .env file."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEV_SECRET_KEY = "dev-insecure-secret-key-do-not-use-in-production"
MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    """Settings are read once per process; field names match the environment variables.

    After validation the fields hold *effective* values: DEV_LOGIN_ENABLED is always
    False outside ENV=dev and SESSION_COOKIE_SECURE defaults to True in prod.
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

    @field_validator("ADMIN_TELEGRAM_IDS", mode="before")
    @classmethod
    def _split_admin_ids(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [int(part) for part in value.replace(" ", "").split(",") if part]
        if isinstance(value, int):
            return [value]
        return value

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
        is_prod = self.ENV == "prod"
        secret = self.SECRET_KEY.get_secret_value()
        if not secret and not is_prod:
            self.SECRET_KEY = SecretStr(DEV_SECRET_KEY)
        if is_prod and (secret == DEV_SECRET_KEY or len(secret) < MIN_SECRET_KEY_LENGTH):
            raise ValueError(
                f"SECRET_KEY must be set to a random value of at least {MIN_SECRET_KEY_LENGTH} characters when ENV=prod"
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
