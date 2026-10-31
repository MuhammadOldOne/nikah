# «Истина» — никах-платформа

Сайт и Telegram-бот [@nikah_istina_bot](https://t.me/nikah_istina_bot) для знакомства с целью никаха.
Это **одна система с одной базой данных**: анкета, отклик или сообщение, созданные в боте, сразу видны
на сайте, и наоборот. Если человек написал собеседнику в боте, сообщение тут же появляется у того в
открытом чате на сайте.

Продуктовая спецификация: [docs/MVP.md](docs/MVP.md).

## Возможности

- **Анкета за 2 минуты, без фото.** Мастер из 3 шагов на сайте или те же вопросы кнопками в боте.
  Вместо фото у всех безликий силуэт.
- **Каталог** анкет противоположного пола с фильтрами: возраст, город, семейное положение, дети,
  образование. Каталог закрыт от поисковиков и доступен только тем, у кого есть анкета.
- **Отклик → согласие → чат.** Написать можно только после того, как вторая сторона приняла отклик.
  Лимит новых откликов в сутки.
- **Живой чат** на сайте (SSE) и в боте: это одна и та же переписка. Можно завершить общение
  или пожаловаться.
- **Вали.** Подопечный приглашает вали ссылкой, и тот получает в боте копии всей переписки.
  Собеседник видит значок «Вали подключён».
- **Модерация.** Премодерация анкет, жалобы, блокировки. Сообщения и анкеты со стоп-словами
  (переводы денег, карты, крипта и т. п.) помечаются для модератора. Модерировать можно
  в админке `/admin` или прямо в боте.
- **Вход через Telegram:** ссылка в бота, магическая ссылка из бота или виджет Telegram Login.
- **Персональные данные:** сервер в России, отдельное согласие на обработку, удаление аккаунта
  со всеми данными (`/delete` в боте или кнопка на сайте).

## Как устроено

```
Браузер ──► Caddy (HTTPS) ──► web: FastAPI + Jinja2/HTMX ──┐
                                                            ├──► PostgreSQL 16 (одна база)
Telegram ◄──► bot: aiogram 3 (polling) + доставка событий ──┘      LISTEN/NOTIFY + outbox
```

- `web` и `bot` — один Python-пакет `app` с общими моделями и сервисным слоем `app/services`.
  Все правила (лимиты, чат, вали, баны) живут только в сервисах, поэтому в обоих каналах они одинаковы.
- Любое событие сервис записывает в таблицу `outbox` и в той же транзакции вызывает `pg_notify`.
  Бот доставляет события в Telegram, сайт пушит их в открытые вкладки.

Подробно для разработчиков: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Стек:** Python 3.12+, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, aiogram 3, Jinja2 + HTMX
(без CDN), PostgreSQL 16, Caddy, Docker Compose.

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

Отдельные части: `tests/services` (бизнес-правила), `tests/web` (сайт), `tests/bot` (бот, Telegram
замокан), `tests/admin` (админка и скрипты). Линтер: `pip install ruff`, затем `ruff check .` и
`ruff format --check .`.

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
Dockerfile, docker-compose.yml, Caddyfile, .env.example   продакшен
```

## Документация

- [docs/MVP.md](docs/MVP.md) — структура сайта и MVP, правила продукта.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — архитектура и контракты модулей.
- [docs/DEPLOY.md](docs/DEPLOY.md) — развёртывание на VPS, обновления, резервные копии, откат.
- [docs/LEGACY_IMPORT.md](docs/LEGACY_IMPORT.md) — перенос пользователей и анкет из текущего бота.

## Персональные данные и репозиторий

Репозиторий публичный. Никогда не добавляйте в него `.env`, токены, базы данных, дампы, резервные
копии и любые выгрузки с данными пользователей. Это персональные данные, в том числе особой категории
(религиозные убеждения, 152-ФЗ). `.gitignore` уже исключает `.env`, `*.db`, `*.sqlite*`, `*.sql.gz`,
`*.dump` и `backups/`, но проверяйте `git status` перед каждым коммитом.
