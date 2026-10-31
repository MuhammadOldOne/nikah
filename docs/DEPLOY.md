# Развёртывание «Истины» на VPS

Пошаговая инструкция для сервера с Ubuntu 22.04 или 24.04, расположенного в России. Всё работает
в Docker Compose: PostgreSQL, сайт (`web`), Telegram-бот (`bot`) и Caddy (HTTPS).

Команды ниже выполняются на новом сервере, если не сказано иначе. Вместо `example.ru` подставьте
свой домен.

## 0. Что понадобится

- **VPS в России** (152-ФЗ: персональные данные граждан РФ хранятся в РФ, а религиозные убеждения —
  особая категория данных): Ubuntu 22.04/24.04, от 2 ГБ памяти и 20 ГБ диска, публичный IPv4.
- **Домен**, в DNS которого можно добавить A-запись.
- **Токен бота** @nikah_istina_bot: @BotFather → `/mybots` → бот → *API Token*.
  Это тот же токен, что у текущего бота, поэтому для пользователей бот останется прежним.
- **Доступ к старому серверу** (`oldvps`), где сейчас работает бот: его нужно будет остановить,
  а данные — перенести ([LEGACY_IMPORT.md](LEGACY_IMPORT.md)).

## 1. Подготовка сервера

```bash
ssh root@IP_СЕРВЕРА
apt update && apt upgrade -y
apt install -y git ufw unattended-upgrades

# Открыты только SSH и веб. Порт базы наружу не публикуется (его нет в docker-compose.yml).
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw enable
```

Входите по SSH-ключу, а вход по паролю отключите (`PasswordAuthentication no` в
`/etc/ssh/sshd_config`, затем `systemctl restart ssh`).

## 2. Установка Docker

Официальный репозиторий Docker:

```bash
apt install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
docker compose version
```

Если `download.docker.com` недоступен с сервера, поставьте Docker из репозитория Ubuntu:
`apt install -y docker.io docker-compose-v2`, затем проверьте `docker compose version`.

Если не скачиваются образы с Docker Hub (`python`, `postgres`, `caddy`), подключите зеркало и
перезапустите Docker:

```bash
cat > /etc/docker/daemon.json <<'EOF'
{ "registry-mirrors": ["https://mirror.gcr.io"] }
EOF
systemctl restart docker
```

## 3. Код проекта

```bash
mkdir -p /opt/nikah
git clone https://github.com/ВЛАДЕЛЕЦ/РЕПОЗИТОРИЙ.git /opt/nikah
cd /opt/nikah
```

Дальше все команды `docker compose ...` выполняются в `/opt/nikah`.

## 4. Настройки (.env)

```bash
cp .env.example .env
chmod 600 .env
openssl rand -hex 24      # → POSTGRES_PASSWORD
openssl rand -base64 48   # → SECRET_KEY
nano .env
```

Заполните:

| Переменная | Значение |
|---|---|
| `DOMAIN` | `example.ru` (без `https://`) |
| `POSTGRES_PASSWORD` | результат `openssl rand -hex 24`; после первого запуска не меняйте |
| `SECRET_KEY` | результат `openssl rand -base64 48` |
| `TELEGRAM_TOKEN` | токен @nikah_istina_bot |
| `ADMIN_TELEGRAM_IDS` | ваш Telegram ID (и других модераторов) через запятую |

Остальное можно оставить как есть. `DATABASE_URL` и `SITE_URL` docker compose собирает сам из
`POSTGRES_PASSWORD` и `DOMAIN`. Файл `.env` содержит секреты: не копируйте его в репозиторий и в чаты.

Проверка: `docker compose config --quiet` ничего не выводит, если всё заполнено. Если нет, он назовёт
пустую обязательную переменную.

## 5. DNS

У регистратора домена создайте **A-запись** `example.ru → IP_СЕРВЕРА`. Если нужен `www.example.ru`,
добавьте и его, а в `Caddyfile` замените `{$DOMAIN}` на `{$DOMAIN}, www.{$DOMAIN}`.
AAAA-запись (IPv6) создавайте, только если IPv6 на сервере действительно настроен: неверная
AAAA-запись мешает выпуску сертификата.

Проверьте, что запись уже видна (может занять от нескольких минут до суток):

```bash
getent hosts example.ru   # должен показать IP сервера
```

Caddy сам получит сертификат Let's Encrypt при первом запуске и будет его продлевать.
Для этого порты 80 и 443 должны быть открыты.

## 6. Домен для входа через виджет Telegram

Вход по кнопке «Войти через Telegram» (ссылка в бота) работает сразу. Чтобы включить ещё и
виджет Telegram Login:

1. В @BotFather: `/setdomain` → выберите @nikah_istina_bot → отправьте `example.ru`.
2. В `.env`: `TELEGRAM_WIDGET_ENABLED=true`.
3. Если стек уже запущен: `docker compose up -d` (контейнеры пересоздадутся с новыми настройками).

## 7. ВАЖНО: остановить старый бот

Telegram отдаёт обновления бота **только одному** процессу. Если новый и старый бот работают с одним
токеном одновременно, оба получают ошибку `Conflict: terminated by other getUpdates request`, а
сообщения пользователей теряются или достаются старому боту. Поэтому старый процесс нужно
остановить **до** запуска нового бота и отключить его автозапуск.

**Сначала** снимите схему старой базы и сохраните копию данных ([LEGACY_IMPORT.md](LEGACY_IMPORT.md),
шаги 1–2). Это можно сделать, пока старый бот ещё работает.

Найдите, как запущен старый бот (на сервере `oldvps`):

```bash
ps aux | grep -i python                      # процессы Python: путь к скрипту, PID
systemctl list-units --type=service | grep -i -E 'bot|nikah|python'
pm2 list                                     # если бот запущен через pm2
screen -ls                                   # сессии screen
tmux ls                                      # сессии tmux
docker ps                                    # если бот в контейнере
crontab -l; sudo crontab -l                  # строки @reboot могут запускать бот снова
```

Остановите тем же способом, каким он запущен, и отключите автозапуск:

| Как запущен | Остановить и отключить |
|---|---|
| systemd | `sudo systemctl stop ИМЯ && sudo systemctl disable ИМЯ` |
| pm2 | `pm2 stop ИМЯ && pm2 delete ИМЯ && pm2 save` |
| screen | `screen -r ИМЯ`, затем Ctrl+C и `exit` |
| tmux | `tmux attach -t ИМЯ`, затем Ctrl+C и `exit` |
| docker | `docker update --restart=no КОНТЕЙНЕР && docker stop КОНТЕЙНЕР` |
| просто процесс | `kill PID` (через 10 секунд, если не завершился: `kill -9 PID`) |
| cron `@reboot` | удалите строку в `crontab -e` |

Убедитесь, что процесса больше нет: `ps aux | grep -i python`.

Если старый бот работал через **webhook**, а не polling, новый бот при запуске сам удалит webhook
(накопившиеся сообщения пользователей не теряются). Поэтому старый бот нужно остановить **до** запуска
нового: иначе он перестанет получать обновления посреди работы. Проверить, установлен ли webhook,
можно так (подставьте токен; выполните с пробелом в начале строки, чтобы токен не попал в историю
команд bash):

```bash
 curl -s "https://api.telegram.org/botТОКЕН/getWebhookInfo"    # "url":"" — webhook нет
```

## 8. Перенос пользователей и анкет

Если маппинг для старой базы уже готов, выполните перенос по [LEGACY_IMPORT.md](LEGACY_IMPORT.md),
шаги 3–5. Там база поднимается и мигрируется **до** запуска сайта и бота. Без переноса пользователям
придётся заново заполнить анкеты, поэтому лучше дождаться маппинга.

## 9. Запуск

```bash
docker compose up -d --build
docker compose ps
```

Первая сборка занимает несколько минут. `web` при старте сам применяет миграции (`alembic upgrade head`).
`bot` запускается после того, как `web` стал healthy, то есть после миграций.

Бот всегда работает в **одном** экземпляре: не запускайте `--scale bot=2` и не запускайте второй
стек с тем же токеном (в том числе локально при разработке).

## 10. Проверка

- [ ] `docker compose ps`: `db` и `web` — `healthy`, `bot` и `caddy` — `running`.
- [ ] `curl -fsS https://example.ru/healthz` отвечает `{"status":"ok"}`.
- [ ] https://example.ru открывается с замком, сертификат выдан на домен.
- [ ] `docker compose logs bot --tail 50`: есть `connected to Telegram as @nikah_istina_bot`,
      нет `Conflict` и `Unauthorized`.
- [ ] С другого компьютера порт базы закрыт: `nc -zv IP_СЕРВЕРА 5432` не подключается.
- [ ] В Telegram: `/start` в боте, создайте тестовую анкету. Она пришла админам в бот и видна в
      https://example.ru/admin/moderation. Одобрите её — автору пришло уведомление.
- [ ] Вход на сайт: «Войти через Telegram» → подтверждение в боте → вкладка авторизовалась.
- [ ] Синхронизация: отправьте отклик на сайте, примите его в боте, напишите сообщение в боте —
      оно сразу появляется в открытом чате на сайте. Ответ с сайта приходит в бот.
- [ ] Вали: в настройках сайта создайте ссылку, откройте её другим аккаунтом Telegram, подтвердите —
      копии переписки приходят вали.
- [ ] Удалите тестовые аккаунты: `/delete` в боте.

## 11. Администраторы

Права админа есть у всех из `ADMIN_TELEGRAM_IDS`. Выдать их ещё кому-то без перезапуска (человек
должен сначала нажать `/start` в боте):

```bash
docker compose exec web python -m scripts.make_admin 123456789
docker compose exec web python -m scripts.make_admin 123456789 --revoke   # снять права
```

Админка: https://example.ru/admin — сводка, модерация анкет, жалобы, стоп-слова, поиск пользователей
и блокировка. Новые анкеты и жалобы также приходят админам в бот с кнопками.

## 12. Обновление

```bash
cd /opt/nikah
./scripts/backup.sh                 # копия базы перед обновлением
git pull
docker compose up -d --build        # миграции применятся при старте web
docker compose ps
docker image prune -f               # удалить старые образы
```

## 13. Логи

```bash
docker compose logs -f web bot          # в реальном времени
docker compose logs --since 1h bot      # за последний час
docker compose logs caddy               # сертификаты, HTTPS
```

Логи каждого контейнера ограничены 5 файлами по 10 МБ (см. `x-logging` в `docker-compose.yml`).

## 14. Резервные копии

`scripts/backup.sh` делает `pg_dump` базы, сжимает его и кладёт в `backups/nikah-ДАТА-ВРЕМЯ.sql.gz`.
Хранятся 14 последних копий. Файлы доступны только владельцу: в них персональные данные.

```bash
./scripts/backup.sh
BACKUP_DIR=/var/backups/nikah KEEP=30 ./scripts/backup.sh    # другая папка и срок
```

Ежедневно в 03:15 (`crontab -e` от root):

```
15 3 * * * cd /opt/nikah && ./scripts/backup.sh >> /var/log/nikah-backup.log 2>&1
```

Копии на том же сервере не спасут при потере диска. Регулярно переносите их в другое хранилище,
**тоже расположенное в России** (152-ФЗ), и лучше в зашифрованном виде, например
`gpg --symmetric --cipher-algo AES256 ФАЙЛ`.

### Восстановление

Дамп содержит `DROP ... IF EXISTS`, поэтому восстанавливается поверх существующей базы:

```bash
docker compose stop web bot
gunzip -c backups/nikah-20260101-031500.sql.gz \
  | docker compose exec -T db psql -U nikah -d nikah -v ON_ERROR_STOP=1 --quiet --output=/dev/null
docker compose up -d
```

Если копия старее кода, `web` при запуске догонит схему миграциями.

Проверяйте копии хотя бы раз в месяц — восстановите в отдельную базу и удалите её:

```bash
docker compose exec db createdb -U nikah nikah_restore_check
gunzip -c backups/ПОСЛЕДНЯЯ.sql.gz \
  | docker compose exec -T db psql -U nikah -d nikah_restore_check -v ON_ERROR_STOP=1 -q -o /dev/null
docker compose exec db psql -U nikah -d nikah_restore_check -c "SELECT count(*) FROM users;"
docker compose exec db dropdb -U nikah nikah_restore_check
```

## 15. Откат

**Новая версия кода работает плохо.** Верните предыдущий коммит:

```bash
git log --oneline -5                       # найдите предыдущий коммит
docker compose run --rm web alembic history | head   # были ли новые миграции
```

- Если новых миграций не было: `git checkout ПРЕДЫДУЩИЙ_КОММИТ && docker compose up -d --build`.
- Если были, сначала откатите схему **кодом новой версии**, а потом переключите код:
  `docker compose run --rm web alembic downgrade РЕВИЗИЯ_ПРЕДЫДУЩЕЙ_ВЕРСИИ`, затем
  `git checkout ...` и `docker compose up -d --build`.
- Самый надёжный путь — восстановить копию, сделанную перед обновлением (раздел 14). Изменения,
  сделанные после неё, пропадут.

Вернуться на ветку: `git checkout main && git pull && docker compose up -d --build`.

**Нужно срочно вернуть старый бот.** Остановите новый (`docker compose stop bot`) и запустите
старый процесс на `oldvps`. Учтите: анкеты и переписка, созданные в новой системе, в старом боте
не появятся.

## 16. Частые проблемы

| Симптом | Причина и решение |
|---|---|
| В логах бота `Conflict: terminated by other getUpdates request` | Работает второй процесс с тем же токеном: старый бот или локальный запуск. Шаг 7. |
| `Conflict: can't use getUpdates method while webhook is active` | Webhook снова установил другой процесс с тем же токеном (старый бот). Остановите его (шаг 7) и перезапустите бота: `docker compose restart bot`. |
| `web` не стартует: `SECRET_KEY must be set...` | Не заполнен `SECRET_KEY` в `.env` (шаг 4). |
| `required variable ... is missing a value` | Не заполнены `DOMAIN` или `POSTGRES_PASSWORD` в `.env`. |
| Нет HTTPS, в логах Caddy ошибки ACME | DNS ещё не указывает на сервер или закрыты порты 80/443 (шаги 1, 5). |
| 502 Bad Gateway | `web` не запустился: `docker compose logs web`. |
| `password authentication failed` после смены `POSTGRES_PASSWORD` | Пароль задаётся только при создании базы. Верните старый или смените его в базе: `docker compose exec db psql -U nikah -c "ALTER ROLE nikah PASSWORD '...'"`. |
| Живой чат не обновляется | Проверьте, что в `Caddyfile` осталось `flush_interval -1` и сжатие не включено для `text/event-stream`. |
