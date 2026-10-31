from enum import StrEnum

import pytest
from pydantic import ValidationError

from app import enums
from app.config import DEV_SECRET_KEY, Settings
from app.enums import Gender, MaritalStatus, ProfileStatus, Relocation, choices, label

PROD_SECRET = "p" * 40


def make_settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)


def test_admin_ids_are_parsed_from_comma_separated_string(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", " 11, 22 ,33,")
    assert make_settings().ADMIN_TELEGRAM_IDS == [11, 22, 33]
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "")
    assert make_settings().ADMIN_TELEGRAM_IDS == []


def test_prod_requires_a_real_secret_key():
    with pytest.raises(ValidationError):
        make_settings(ENV="prod", SECRET_KEY=DEV_SECRET_KEY)
    with pytest.raises(ValidationError):
        make_settings(ENV="prod", SECRET_KEY="short")
    with pytest.raises(ValidationError):
        make_settings(ENV="prod", SECRET_KEY="")
    assert make_settings(ENV="prod", SECRET_KEY=PROD_SECRET).is_prod
    # In dev an empty value (e.g. "SECRET_KEY=" in .env) falls back to the dev key.
    assert make_settings(ENV="dev", SECRET_KEY="").SECRET_KEY.get_secret_value() == DEV_SECRET_KEY


def test_public_dev_key_is_refused_for_a_real_site():
    # A production .env without ENV (dev is the default) and without SECRET_KEY must not start.
    with pytest.raises(ValidationError, match="development SECRET_KEY"):
        make_settings(SITE_URL="https://istina.example", SECRET_KEY="")
    assert (
        make_settings(SITE_URL="http://localhost:8000", SECRET_KEY="").SECRET_KEY.get_secret_value() == DEV_SECRET_KEY
    )


def test_dev_login_only_effective_in_dev_and_secure_cookie_default():
    prod = make_settings(ENV="prod", SECRET_KEY=PROD_SECRET, DEV_LOGIN_ENABLED=True, SESSION_COOKIE_SECURE=None)
    assert prod.DEV_LOGIN_ENABLED is False
    assert prod.SESSION_COOKIE_SECURE is True
    dev = make_settings(ENV="dev", DEV_LOGIN_ENABLED=True, SESSION_COOKIE_SECURE=None)
    assert dev.DEV_LOGIN_ENABLED is True
    assert dev.SESSION_COOKIE_SECURE is False


def test_defaults_and_normalization():
    settings = make_settings(
        SITE_URL="https://istina.example/", BOT_USERNAME="@nikah_istina_bot", SECRET_KEY=PROD_SECRET
    )
    assert settings.SITE_URL == "https://istina.example"
    assert settings.BOT_USERNAME == "nikah_istina_bot"
    assert settings.PREMODERATION is True
    assert settings.INTEREST_DAILY_LIMIT == 10
    assert settings.TELEGRAM_WIDGET_ENABLED is False
    assert "TEST-TOKEN" not in repr(settings)


def test_every_enum_member_has_a_label():
    enum_classes = [
        value
        for value in vars(enums).values()
        if isinstance(value, type) and issubclass(value, StrEnum) and value is not StrEnum
    ]
    assert len(enum_classes) >= 18
    for enum_cls in enum_classes:
        for member in enum_cls:
            assert label(member), member


def test_labels_do_not_collide_between_enums():
    assert label(ProfileStatus.PENDING) == "на модерации"
    assert label(enums.InterestStatus.PENDING) == "ожидает ответа"


def test_gender_aware_labels():
    assert label(MaritalStatus.NEVER_MARRIED, Gender.MALE) == "не был женат"
    assert label(MaritalStatus.NEVER_MARRIED, Gender.FEMALE) == "не была замужем"
    assert label(MaritalStatus.NEVER_MARRIED) == "не был(а) в браке"
    assert label(MaritalStatus.WIDOWED, "male") == "вдовец"
    assert label(MaritalStatus.WIDOWED, "female") == "вдова"
    assert label(MaritalStatus.DIVORCED, Gender.FEMALE) == "в разводе"
    assert label(Relocation.READY, Gender.FEMALE) == "готова"
    assert label(Relocation.NOT_READY, Gender.MALE) == "не готов"
    assert label(None) == ""


def test_choices_and_opposite_gender():
    assert choices(MaritalStatus, Gender.FEMALE) == [
        ("never_married", "не была замужем"),
        ("divorced", "в разводе"),
        ("widowed", "вдова"),
    ]
    assert Gender.MALE.opposite is Gender.FEMALE and Gender.FEMALE.opposite is Gender.MALE


def test_legacy_env_names_are_accepted_and_new_names_win(monkeypatch):
    for name in ("TELEGRAM_TOKEN", "ADMIN_TELEGRAM_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "111:legacy")
    monkeypatch.setenv("ADMIN_IDS", "11, 22")
    legacy = make_settings()
    assert legacy.TELEGRAM_TOKEN.get_secret_value() == "111:legacy" and legacy.ADMIN_TELEGRAM_IDS == [11, 22]
    monkeypatch.setenv("TELEGRAM_TOKEN", "222:new")
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "33")
    both = make_settings()
    assert both.TELEGRAM_TOKEN.get_secret_value() == "222:new" and both.ADMIN_TELEGRAM_IDS == [33]
    # An empty new value (copied from .env.example) does not hide the legacy one.
    monkeypatch.setenv("TELEGRAM_TOKEN", "")
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "")
    assert make_settings().TELEGRAM_TOKEN.get_secret_value() == "111:legacy"
    assert make_settings().ADMIN_TELEGRAM_IDS == [11, 22]


def test_legacy_integration_settings(monkeypatch):
    defaults = make_settings()
    assert (defaults.MAX_OPEN_CONVERSATIONS, defaults.CHAT_RATE_LIMIT_PER_MIN) == (1, 20)
    assert defaults.WITNESS_CHAT_ID is None and defaults.TELEGRAM_PROXY is None
    monkeypatch.setenv("WITNESS_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("TELEGRAM_PROXY", "socks5://user:secret@proxy.invalid:1080")
    settings = make_settings()
    assert settings.WITNESS_CHAT_ID == -1001234567890
    assert settings.TELEGRAM_PROXY is not None
    assert settings.TELEGRAM_PROXY.get_secret_value().startswith("socks5://")
    assert "secret" not in repr(settings) and "secret" not in str(settings.TELEGRAM_PROXY)
    monkeypatch.setenv("WITNESS_CHAT_ID", " ")
    monkeypatch.setenv("TELEGRAM_PROXY", "")
    assert make_settings().WITNESS_CHAT_ID is None and make_settings().TELEGRAM_PROXY is None
    with pytest.raises(ValidationError):
        make_settings(MAX_OPEN_CONVERSATIONS=0)
    with pytest.raises(ValidationError):
        make_settings(CHAT_RATE_LIMIT_PER_MIN=0)


def test_new_labels():
    assert label(enums.ConversationCloseReason.CLOSED_BY_WITNESS) == "чат завершил свидетель"
    assert label(enums.MessageKind.WITNESS) == "сообщение свидетеля"
