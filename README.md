# «Никах Истина» — никах-платформа

Сайт и Telegram-бот [@nikah_istina_bot](https://t.me/nikah_istina_bot) для знакомства с целью никаха.
Это **одна система с одной базой данных**: анкета, отклик или сообщение, созданные в боте, сразу видны
на сайте, и наоборот. Если человек написал собеседнику в боте, сообщение тут же появляется у того в
открытом чате на сайте.

Бот @nikah_istina_bot уже работал до сайта. Новая система встаёт на его место без заметной смены для
пользователей: те же подписи меню и команды, номера анкет, правила и тексты, а их анкеты, отклики и
переписки переносятся ([docs/LEGACY_BOT.md](docs/LEGACY_BOT.md)).

Продуктовая спецификация: [docs/MVP.md](docs/MVP.md).

## Возможности

- **Знакомство по номеру, без имён и фото.** Человека знают только как «Анкета №512 — Брат/Сестра»;
  вместо фото у всех безликий силуэт. Анкета за 2 минуты: мастер из 3 шагов на сайте или те же
  вопросы кнопками в боте.
- **Каталог** анкет противоположного пола с фильтрами: возраст, город, семейное положение, дети,
  образование. Каталог закрыт от поисковиков и открывается только с одобренной анкетой
  (защита от фейков, собирающих анкеты сестёр).
- **Отклик → согласие → чат.** Написать можно только после того, как вторая сторона приняла отклик.
  Лимит новых откликов в сутки; у человека один открытый чат, как в боте.
- **Живой чат** на сайте (SSE) и в боте: это одна и та же переписка, только текст. Телефоны, ссылки
  и @username скрываются фильтром. Можно завершить общение или пожаловаться на сообщение.
- **Свидетель.** Копию каждого сообщения получает группа свидетелей (администраторы): они могут
  пометить сообщение, написать обеим сторонам или закрыть чат.
- **Вали.** Подопечный приглашает вали ссылкой, и тот получает в боте копии всей переписки.
  Собеседник видит значок «Вали подключён».
- **Модерация.** Премодерация анкет, жалобы, блокировки. Сообщения и анкеты со стоп-словами
  (переводы денег, карты, крипта и т. п.) помечаются для модератора. Модерировать можно
  в админке `/admin` или прямо в боте.
- **Перенос из старого бота:** `scripts/import_legacy.py` переносит пользователей, анкеты с их
  номерами, отклики, чаты и сообщения; перенесённые пользователи дают согласие при первом заходе
  и дополняют анкету за минуту.
- **Вход через Telegram:** ссылка в бота, магическая ссылка из бота или виджет Telegram Login.
- **Персональные данные:** сервер в России, отдельное согласие на обработку, удаление аккаунта
  со всеми данными (`/delete` в боте или кнопка на сайте).

## Как устроено

```
Браузер ──► Caddy или nginx ──► web: FastAPI + Jinja2/HTMX ─┐
                                                            ├──► PostgreSQL 16 (одна база)
Telegram ◄──► bot: aiogram 3 (polling) + доставка событий ──┘      LISTEN/NOTIFY + outbox
```

- `web` и `bot` — один Python-пакет `app` с общими моделями и сервисным слоем `app/services`.
  Все правила (лимиты, чат, вали, баны) живут только в сервисах, поэтому в обоих каналах они одинаковы.
- Любое событие сервис записывает в таблицу `outbox` и в той же транзакции вызывает `pg_notify`.
  Бот доставляет события в Telegram, сайт пушит их в открытые вкладки.

Подробно для разработчиков: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Стек:** Python 3.12+, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, aiogram 3, Jinja2 + HTMX
(без CDN), PostgreSQL 16, Caddy (или существующий nginx), Docker Compose.

## Быстрый старт для разработки

Нужны Python 3.12+ и PostgreSQL 16.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# База и пользователь для разработки (логин и пароль nikah — только для локальной машины)
sudo -u postgres psql -c "CREATE ROLE nikah LOGIN PASSWORD 'nikah';"
sudo -u postgres psql -c "CREATE DATABASE nikah OWNER nikah;"
# или в Docker:
# docker run -d --name nikah-db -e POSTGRES_USER=nikah -e POSTGRES_PASSWORD=nikah \
#   -e POSTGRES_DB=nikah -p 127.0.0.1:5432:5432 postgres:16-alpine

.venv/bin/alembic upgrade head
```

Без файла `.env` приложение работает в режиме `ENV=dev` с базой
`postgresql+asyncpg://nikah:nikah@127.0.0.1:5432/nikah`. Для разработки создайте `.env`:

```bash
cat > .env <<'EOF'
ENV=dev
DEV_LOGIN_ENABLED=true
# Токен ОТДЕЛЬНОГО тестового бота из @BotFather. Не используйте токен @nikah_istina_bot:
# два процесса с одним токеном мешают друг другу, и рабочий бот перестанет получать сообщения.
TELEGRAM_TOKEN=
BOT_USERNAME=
# Ваш Telegram ID, чтобы открыть /admin
ADMIN_TELEGRAM_IDS=
EOF
```

Все настройки с пояснениями — в [.env.example](.env.example) (это шаблон для сервера, там `ENV=prod`).

Запуск:

```bash
.venv/bin/uvicorn app.web.main:app --reload --port 8000   # сайт: http://localhost:8000
.venv/bin/python -m app.bot                               # бот (нужен TELEGRAM_TOKEN)
```

Войти на сайт без Telegram (только при `ENV=dev` и `DEV_LOGIN_ENABLED=true`):
<http://localhost:8000/auth/dev-login?telegram_id=111&first_name=Тест>.
Админка — <http://localhost:8000/admin> для Telegram ID из `ADMIN_TELEGRAM_IDS` или после
`.venv/bin/python -m scripts.make_admin <telegram_id>`.

## Тесты

Тесты удаляют и заново создают схему базы, поэтому им нужна **отдельная** база, в имени которой
есть `test`:

```bash
sudo -u postgres psql -c "CREATE DATABASE nikah_test OWNER nikah;"
TEST_DATABASE_URL=postgresql+asyncpg://nikah:nikah@127.0.0.1:5432/nikah_test .venv/bin/pytest -q
```

Отдельные части: `tests/services` (бизнес-правила и миграции), `tests/web` (сайт), `tests/bot` (бот,
Telegram замокан), `tests/admin` (админка и скрипты), `tests/import` (перенос старого бота на
синтетической SQLite-базе). Линтер: `pip install ruff`, затем `ruff check .` и
`ruff format --check .`.

Сквозной тест «сайт ↔ бот» в настоящем браузере (`tests/e2e`) по умолчанию пропускается. Ему нужны
Node.js с Playwright и свободный порт 8000. Тест **очищает** свою базу (`E2E_DATABASE_URL`, по
умолчанию `nikah_e2e_test`; в имени должно быть `test`), мигрирует её, переносит синтетическую базу
старого бота (`scripts/import_legacy.py --commit`), запускает сайт и проходит сценарий: две анкеты
через мастер, модерация, отклик, живой чат, ответ из Telegram (настоящий код бота, Telegram
замокан), копии свидетелю из обоих каналов, «Написать в чат» свидетеля, подключение вали, фильтр
контактов, вход перенесённого пользователя (согласие, старый номер, «Дополнить анкету», история
чата) и отказ принять второй чат. Скриншоты — в `docs/screenshots/e2e-*.png`.

```bash
sudo -u postgres psql -c "CREATE DATABASE nikah_e2e_test OWNER nikah;"
E2E=1 TEST_DATABASE_URL=postgresql+asyncpg://nikah:nikah@127.0.0.1:5432/nikah_test .venv/bin/pytest -q tests/e2e
```

## Структура

```
app/
  config.py, db.py, models.py, schemas.py, enums.py   настройки, база, модели, валидация, справочники
  services/        бизнес-правила: анкеты, отклики, чат, вали, модерация, события (outbox)
  web/             сайт: routes/, templates/, static/ (routes/admin.py и templates/admin/ — админка)
  bot/             Telegram-бот и доставка уведомлений: python -m app.bot
migrations/        миграции Alembic
scripts/           backup.sh, make_admin.py, import_legacy.py
tests/             тесты (pytest)
docs/              спецификация, архитектура, развёртывание, перенос старого бота
deploy/nginx/       конфиг сайта для существующего nginx
Dockerfile, docker-compose*.yml, Caddyfile, .env.example   продакшен
```

## Документация

- [docs/MVP.md](docs/MVP.md) — структура сайта и MVP, правила продукта.
- [docs/LEGACY_BOT.md](docs/LEGACY_BOT.md) — связка с действующим ботом @nikah_istina_bot: что
  сохраняется, перенос данных, сервер (где расходится с MVP.md, прав он).
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — архитектура и контракты модулей.
- [docs/DEPLOY.md](docs/DEPLOY.md) — развёртывание на VPS (Caddy или nginx, прокси для Telegram),
  обновления, резервные копии, откат.
- [docs/CUTOVER.md](docs/CUTOVER.md) — переключение со старого бота на новую систему и откат.
- [docs/LEGACY_IMPORT.md](docs/LEGACY_IMPORT.md) — перенос пользователей, анкет, откликов и чатов
  из старого бота.

## Персональные данные и репозиторий

Репозиторий публичный. Никогда не добавляйте в него `.env`, токены, базы данных, дампы, резервные
копии и любые выгрузки с данными пользователей. Это персональные данные, в том числе особой категории
(религиозные убеждения, 152-ФЗ). `.gitignore` уже исключает `.env`, `*.db`, `*.sqlite*`, `*.sql.gz`,
`*.dump` и `backups/`, но проверяйте `git status` перед каждым коммитом.
