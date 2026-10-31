# One image for both services: the website (default command) and the bot (python -m app.bot).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY app ./app

USER app
EXPOSE 8000

# Apply migrations, then serve. Only the reverse proxy reaches this port (Caddy on the compose
# network; in nginx mode it is published on 127.0.0.1 only), so the X-Forwarded-* headers it sets
# can be trusted from any address.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.web.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --workers 2 --timeout-graceful-shutdown 5"]
