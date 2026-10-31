# Развёртывание «Никах Истина» на сервере

Инструкция для сервера с **Ubuntu 24.04**, расположенного в России. Всё работает в Docker Compose:
PostgreSQL (`db`), сайт (`web`), Telegram-бот (`bot`) и, в одном из режимов, Caddy (HTTPS).

Сайт можно поставить на тот же сервер, где сейчас работает старый бот @nikah_istina_bot и другие
боты: Docker не трогает чужие сервисы и занимает только те порты, которые указаны ниже.

**Переход со старого бота** (остановка, перенос данных, запуск нового бота) описан отдельно в
[CUTOVER.md](CUTOVER.md). Эта инструкция готовит сервер; **бот запускается только по CUTOVER.md**,
после переноса данных.

В командах ниже замените заглушки своими значениями:

| Заглушка | Что подставить |
|---|---|
| `example.ru` | домен сайта |
| `<IP_сервера>` | публичный IPv4 сервера |
| `<папка_старого_бота>` | папка, где лежат код старого бота, его `.env` и `data.db` |
| `<имя_сервиса>` | имя systemd-сервиса старого бота |
| `<адрес_прокси>`, `<порт_прокси>` | адрес и порт прокси для api.telegram.org |

Реальные адреса, пути, токены и id **не записывайте** в репозиторий, issues и чаты: репозиторий публичный.

## 0. Что понадобится

- **Сервер в России** (152-ФЗ: данные граждан РФ хранятся в РФ, а религиозные убеждения — особая
  категория данных): Ubuntu 24.04, от 2 ГБ памяти и 20 ГБ свободного диска.
- **Домен** с A-записью на сервер.
- **`.env` старого бота** (в `<папка_старого_бота>`): токен, id админов, группа свидетелей, прокси.
- **Доступ root** (или `sudo`) по SSH-ключу.

## 1. Подготовка сервера

```bash
ssh root@<IP_сервера>
apt update && apt upgrade -y
apt install -y git curl sqlite3 unattended-upgrades
```

`sqlite3` нужен для копии базы старого бота при переходе.

**Файрвол.** Сначала посмотрите, что уже настроено: на сервере работают другие сервисы.

```bash
ufw status verbose
```

- Если `ufw` **включён** — добавьте только недостающие правила: `ufw allow 80/tcp`, `ufw allow 443/tcp`
  (и `ufw allow 443/udp` для режима Caddy). SSH и правила других сервисов не трогайте.
- Если `ufw` **выключен** и сервер защищён иначе (файрвол хостера) — оставьте как есть.
- Порт PostgreSQL наружу не публикуется ни в одном режиме.

## 2. Установка Docker

Официальный репозиторий Docker для Ubuntu 24.04 (noble):

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

Если не скачиваются образы с Docker Hub (`python`, `postgres`, `caddy`), подключите зеркало:

```bash
cat > /etc/docker/daemon.json <<'EOF'
{ "registry-mirrors": ["https://mirror.gcr.io"] }
EOF
systemctl restart docker
```

## 3. Код проекта

```bash
git clone https://github.com/ВЛАДЕЛЕЦ/РЕПОЗИТОРИЙ.git /opt/nikah
cd /opt/nikah
```

Дальше все команды `docker compose ...` выполняются в `/opt/nikah`.

## 4. Выбор режима: Caddy или nginx

Посмотрите, кто занимает порты 80 и 443:

```bash
ss -ltnp '( sport = :80 or sport = :443 )'
```

- **Пусто** → **режим Caddy**: Caddy в контейнере сам получает и продлевает сертификат Let's Encrypt.
- **Там `nginx`** → **режим nginx**: сайт обслуживает уже установленный nginx, Caddy не запускается,
  сайт слушает только `127.0.0.1:8000` (раздел 8).

Режим и дополнительные варианты выбираются одной строкой `COMPOSE_FILE` в `.env`, поэтому все
команды `docker compose ...` дальше одинаковы для любого режима:

| Файл | Зачем |
|---|---|
| `docker-compose.yml` | основа: `db`, `web`, `bot`, `caddy` |
| `docker-compose.nginx.yml` | режим nginx: Caddy выключен, `web` на `127.0.0.1:${WEB_HOST_PORT:-8000}` |
| `docker-compose.hostproxy.yml` | прокси Telegram слушает `127.0.0.1` хоста: бот в сети хоста (раздел 6) |
| `docker-compose.hostgateway.yml` | прокси Telegram слушает docker-интерфейс: бот обращается к `host.docker.internal` (раздел 6) |

## 5. Настройки (.env)

```bash
cp .env.example .env
chmod 600 .env
openssl rand -hex 24      # → POSTGRES_PASSWORD
openssl rand -base64 48   # → SECRET_KEY
nano .env
```

| Переменная | Значение |
|---|---|
| `COMPOSE_FILE` | режим Caddy — не трогать; режим nginx — `docker-compose.yml:docker-compose.nginx.yml` (+ варианты прокси, раздел 6) |
| `DOMAIN` | `example.ru` (без `https://`) |
| `POSTGRES_PASSWORD` | результат `openssl rand -hex 24`; после первого запуска не меняйте |
| `SECRET_KEY` | результат `openssl rand -base64 48` |
| `TELEGRAM_TOKEN` | токен @nikah_istina_bot (тот же, что у старого бота) |
| `ADMIN_TELEGRAM_IDS` | Telegram ID админов через запятую |
| `WITNESS_CHAT_ID` | id группы свидетелей (`-100…`); пусто — копии получает в личку каждый админ |
| `TELEGRAM_PROXY` | прокси для api.telegram.org (раздел 6); пусто — напрямую |

Остальное можно оставить как есть. `DATABASE_URL` и `SITE_URL` docker compose собирает сам из
`POSTGRES_PASSWORD` и `DOMAIN`.

### Настройки из старого бота

Посмотрите `.env` старого бота **на сервере** (не копируйте его вывод в чаты):

```bash
cat "<папка_старого_бота>/.env"
```

| В старом `.env` | В новом `.env` |
|---|---|
| `BOT_TOKEN` | `TELEGRAM_TOKEN` (имя `BOT_TOKEN` тоже принимается) |
| `ADMIN_IDS` | `ADMIN_TELEGRAM_IDS` (имя `ADMIN_IDS` тоже принимается, формат тот же: `111,222`) |
| `WITNESS_CHAT_ID` | `WITNESS_CHAT_ID` — как есть |
| `TELEGRAM_PROXY` | `TELEGRAM_PROXY` — как есть, если прокси на другом сервере; если это `127.0.0.1` этого сервера — раздел 6 |
| `DB_PATH` | **не переносится**: это путь к `data.db` (по умолчанию `data.db` в `<папка_старого_бота>`). Он нужен только при переносе данных ([CUTOVER.md](CUTOVER.md)) |

Если заданы и новое, и старое имя, действует новое; пустое новое значение не скрывает старое.

Проверка: `docker compose config --quiet` ничего не выводит, если всё заполнено, а иначе называет
пустую обязательную переменную. `docker compose config --services` показывает сервисы выбранного
режима (в режиме nginx — без `caddy`).

## 6. Прокси Telegram

С серверов в России api.telegram.org часто недоступен напрямую, поэтому старый бот ходит через
`TELEGRAM_PROXY`. Поддерживаются `http://` (CONNECT), `socks5://` и `socks4://`, в том числе с логином и
паролем; `https://` бот отвергнет при запуске. Значение прокси в логи не пишется.

**Где слушает прокси.** Если прокси стоит на этом же сервере, найдите его порт:

```bash
ss -ltnp | grep <порт_прокси>
```

| Колонка «Local Address» | Что делать |
|---|---|
| `127.0.0.1:<порт_прокси>` (только loopback) | вариант **A**: `TELEGRAM_PROXY=http://127.0.0.1:<порт_прокси>` и `docker-compose.hostproxy.yml` |
| `172.17.0.1:<порт_прокси>` или `0.0.0.0:<порт_прокси>` / `*:<порт_прокси>` | вариант **B**: `TELEGRAM_PROXY=http://host.docker.internal:<порт_прокси>` и `docker-compose.hostgateway.yml` |
| прокси на другом сервере | ничего добавлять не нужно: `TELEGRAM_PROXY=http://<адрес_прокси>:<порт_прокси>` |

**Вариант A — бот в сети хоста** (проще всего, если прокси слушает только `127.0.0.1`). Контейнер
бота использует сеть сервера, поэтому `127.0.0.1` для него — это сам сервер, как у старого бота.
До базы бот доходит через порт, опубликованный только на `127.0.0.1` (снаружи он недоступен):

```
COMPOSE_FILE=docker-compose.yml:docker-compose.hostproxy.yml
# в режиме nginx: COMPOSE_FILE=docker-compose.yml:docker-compose.nginx.yml:docker-compose.hostproxy.yml
TELEGRAM_PROXY=http://127.0.0.1:<порт_прокси>
```

Если порт `5432` на `127.0.0.1` уже занят (`ss -ltnp | grep 5432` — например, PostgreSQL другого
сервиса), задайте другой: `POSTGRES_HOST_PORT=5433`.

**Вариант B — `host.docker.internal`.** Бот остаётся в сети compose и обращается к серверу по
имени `host.docker.internal` (адрес docker-моста, обычно `172.17.0.1`). Прокси должен слушать этот
адрес или `0.0.0.0`; во втором случае закройте его порт снаружи (`ufw deny <порт_прокси>/tcp`) и
разрешите только docker-сетям: `ufw allow from 172.16.0.0/12 to any port <порт_прокси> proto tcp`.

```
COMPOSE_FILE=docker-compose.yml:docker-compose.hostgateway.yml
TELEGRAM_PROXY=http://host.docker.internal:<порт_прокси>
```

**Проверка прокси с сервера** (ответ `302` значит, что Telegram доступен через прокси):

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -x "http://127.0.0.1:<порт_прокси>" https://api.telegram.org/
curl -sS -o /dev/null -w '%{http_code}\n' --socks5-hostname 127.0.0.1:<порт_прокси> https://api.telegram.org/   # для socks5
```

**Проверка из контейнера бота** (для `http://`-прокси; выводит `200`, ничего секретного не печатает):

```bash
docker compose run --rm --no-deps bot python -c "import os, urllib.request as u; \
print(u.build_opener(u.ProxyHandler({'https': os.environ['TELEGRAM_PROXY']})).open('https://api.telegram.org/', timeout=15).status)"
```

После запуска бота (CUTOVER.md) в `docker compose logs bot` должна быть строка
`connected to Telegram as @nikah_istina_bot`.

## 7. DNS

У регистратора домена создайте **A-запись** `example.ru → <IP_сервера>`. AAAA-запись (IPv6)
создавайте, только если IPv6 на сервере действительно настроен: неверная AAAA-запись мешает
выпуску сертификата.

```bash
getent hosts example.ru   # должен показать IP сервера (иногда ждать до суток)
```

## 8. HTTPS: режим Caddy или режим nginx

### Режим Caddy (порты 80/443 свободны)

`COMPOSE_FILE` в `.env` не задавайте (или только с вариантом прокси). Caddy запускается вместе с
сайтом и сам получает сертификат при первом обращении (порты 80 и 443 должны быть открыты). Если
нужен `www.example.ru`, добавьте для него A-запись, а в `Caddyfile` замените `{$DOMAIN}` на
`{$DOMAIN}, www.{$DOMAIN}`.

### Режим nginx (порты 80/443 заняты nginx)

1. В `.env`: `COMPOSE_FILE=docker-compose.yml:docker-compose.nginx.yml` (плюс вариант прокси из
   раздела 6). Если порт 8000 на `127.0.0.1` занят (`ss -ltnp | grep 8000`), задайте
   `WEB_HOST_PORT=8010` и тот же порт в конфиге nginx.
2. Конфиг сайта для nginx — [`deploy/nginx/nikah.conf`](../deploy/nginx/nikah.conf). В нём уже
   настроено всё, что нужно живому чату (Server-Sent Events): `proxy_buffering off`, HTTP/1.1 к
   приложению, долгий `proxy_read_timeout`, заголовки `X-Forwarded-*`. Замените в нём `example.ru` и
   подключите:

   ```bash
   sed 's/example\.ru/ВАШ_ДОМЕН/g' deploy/nginx/nikah.conf > /etc/nginx/sites-available/nikah.conf
   ln -s /etc/nginx/sites-available/nikah.conf /etc/nginx/sites-enabled/nikah.conf
   nginx -t && systemctl reload nginx
   ```

3. Сертификат Let's Encrypt (certbot сам допишет `listen 443 ssl`, сертификат и редирект с http, и
   будет продлевать его по таймеру):

   ```bash
   apt install -y certbot python3-certbot-nginx
   certbot --nginx -d example.ru --redirect
   systemctl list-timers | grep certbot     # автопродление включено
   ```

   Если в общем `nginx.conf` в `gzip_types` перечислен `text/event-stream`, уберите его: сжатие
   буферизует живой чат.

Сайт заработает после запуска `web` (CUTOVER.md, шаг 1). Проверка:
`curl -fsS https://example.ru/healthz` → `{"status":"ok"}`.

## 9. Вход через виджет Telegram (BotFather `/setdomain`)

Вход по кнопке «Войти через Telegram» (ссылка в бота) работает сразу после запуска нового бота.
Чтобы включить ещё и виджет Telegram Login:

1. В @BotFather: `/setdomain` → выберите @nikah_istina_bot → отправьте `example.ru`.
2. В `.env`: `TELEGRAM_WIDGET_ENABLED=true`.
3. `docker compose up -d` (контейнеры пересоздадутся с новыми настройками).

## 10. Первый запуск и переход со старого бота

Дальше — по [CUTOVER.md](CUTOVER.md): поднять базу и сайт **без бота**, пробный перенос, остановка
старого бота, перенос с `--commit`, запуск нового бота, проверка. Не запускайте `docker compose up -d`
целиком до переноса: новый бот начнёт работать параллельно со старым (оба получат
`Conflict: terminated by other getUpdates request`), а созданные им анкеты не дадут выполнить перенос.

Бот всегда работает в **одном** экземпляре: не запускайте `--scale bot=2` и второй стек с тем же
токеном (в том числе локально при разработке).

## 11. Проверка после запуска

- [ ] `docker compose ps`: `db` и `web` — `healthy`, `bot` (и `caddy` в режиме Caddy) — `running`.
- [ ] `curl -fsS https://example.ru/healthz` отвечает `{"status":"ok"}`; сайт открывается с замком.
- [ ] `docker compose logs bot --tail 50`: есть `connected to Telegram as @nikah_istina_bot`,
      нет `Conflict` и `Unauthorized`.
- [ ] С другого компьютера порты базы и сайта закрыты: `nc -zv <IP_сервера> 5432` и
      `nc -zv <IP_сервера> 8000` не подключаются.
- [ ] Остальное — чек-лист в [CUTOVER.md](CUTOVER.md), шаг 7.

## 12. Администраторы

Права админа есть у всех из `ADMIN_TELEGRAM_IDS`. Выдать их ещё кому-то без перезапуска (человек
должен сначала нажать `/start` в боте):

```bash
docker compose exec web python -m scripts.make_admin 123456789
docker compose exec web python -m scripts.make_admin 123456789 --revoke   # снять права
```

Админка: https://example.ru/admin — сводка (в том числе итоги переноса из старого бота), модерация
анкет, жалобы, помеченные сообщения (фильтр контактов, стоп-слова, свидетель, жалобы), сообщения
свидетеля, чаты, поиск людей по номеру анкеты (`№512` или `#512`), Telegram ID или @username.
В боте админам по-прежнему доступны `/admin`, `/ban` и `/unban`.

## 13. Обновление

```bash
cd /opt/nikah
./scripts/backup.sh                 # копия базы перед обновлением
git pull
docker compose up -d --build        # миграции применятся при старте web
docker compose ps
docker image prune -f               # удалить старые образы
```

Если в обновлении менялись `deploy/nginx/nikah.conf` или файлы `docker-compose.*.yml`, сравните
их со своими копиями (`diff deploy/nginx/nikah.conf /etc/nginx/sites-available/nikah.conf`).

## 14. Логи

```bash
docker compose logs -f web bot          # в реальном времени
docker compose logs --since 1h bot      # за последний час
docker compose logs caddy               # режим Caddy: сертификаты, HTTPS
tail -f /var/log/nginx/nikah.error.log  # режим nginx
```

Логи контейнеров ограничены 5 файлами по 10 МБ на контейнер (`x-logging` в `docker-compose.yml`).
Тексты сообщений, анкеты и значение прокси в логи не пишутся.

## 15. Резервные копии

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
**тоже расположенное в России** (152-ФЗ), лучше в зашифрованном виде, например
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

## 16. Откат версии кода

```bash
git log --oneline -5                                    # найдите предыдущий коммит
docker compose run --rm --no-deps web alembic history | head   # были ли новые миграции
```

- Новых миграций не было: `git checkout ПРЕДЫДУЩИЙ_КОММИТ && docker compose up -d --build`.
- Были: сначала откатите схему **кодом новой версии**
  (`docker compose run --rm web alembic downgrade РЕВИЗИЯ_ПРЕДЫДУЩЕЙ_ВЕРСИИ`), затем `git checkout ...`
  и `docker compose up -d --build`.
- Самый надёжный путь — восстановить копию, сделанную перед обновлением (раздел 15).

Вернуться на ветку: `git checkout main && git pull && docker compose up -d --build`.

Возврат к **старому боту** — в [CUTOVER.md](CUTOVER.md), раздел «Откат».

## 17. Частые проблемы

| Симптом | Причина и решение |
|---|---|
| В логах бота `Conflict: terminated by other getUpdates request` | Работает второй процесс с тем же токеном — обычно старый бот. `systemctl stop <имя_сервиса> && systemctl disable <имя_сервиса>` (CUTOVER.md, шаг 3). |
| `Conflict: can't use getUpdates method while webhook is active` | Webhook установил другой процесс с тем же токеном. Остановите его и `docker compose restart bot`. |
| Бот не подключается: `TelegramNetworkError`, таймауты | Telegram недоступен без прокси или прокси недоступен из контейнера: раздел 6 (`ss -ltnp`, проверка `curl` и из контейнера). |
| Бот не стартует: `TELEGRAM_PROXY: use http://host:port or socks5://host:port …` | `https://`-прокси не поддерживается: укажите `http://` или `socks5://`. |
| `web` не стартует: `SECRET_KEY must be set...` | Не заполнен `SECRET_KEY` (раздел 5). `docker-compose.yml` всегда запускает приложение с `ENV=prod`. |
| `required variable ... is missing a value` | Не заполнены `DOMAIN` или `POSTGRES_PASSWORD` в `.env`. |
| `bind: address already in use` для `127.0.0.1:8000` или `127.0.0.1:5432` | Порт занят другим сервисом: `WEB_HOST_PORT` / `POSTGRES_HOST_PORT` в `.env` (и порт в конфиге nginx). |
| Режим Caddy: нет HTTPS, в логах Caddy ошибки ACME | DNS ещё не указывает на сервер или закрыты порты 80/443 (разделы 1, 7). |
| Режим Caddy: `bind: address already in use` для `:80` | Порты заняты nginx: нужен режим nginx (разделы 4, 8). |
| 502 Bad Gateway | `web` не запустился (`docker compose logs web`) или nginx смотрит не на тот порт (`WEB_HOST_PORT`). |
| `password authentication failed` после смены `POSTGRES_PASSWORD` | Пароль задаётся только при создании базы. Верните старый или смените его в базе: `docker compose exec db psql -U nikah -c "ALTER ROLE nikah PASSWORD '...'"`. |
| Живой чат не обновляется | Caddy: в `Caddyfile` должно остаться `flush_interval -1`. nginx: `proxy_buffering off` и `proxy_http_version 1.1` из `deploy/nginx/nikah.conf`, `text/event-stream` не в `gzip_types`. |
