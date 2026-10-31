"""Bot entry point: ``python -m app.bot``.

Runs long polling, the outbox worker (woken by PostgreSQL NOTIFY, polling every 5 s as a
fallback) and periodic housekeeping in one process. SIGINT/SIGTERM stop polling (in-flight
updates finish), the worker finishes its current batch, then connections are closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramServerError, TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError

from app.bot.dispatcher import create_bot, create_dispatcher, set_commands
from app.bot.outbox import OutboxWorker, housekeeping
from app.config import get_settings
from app.db import dispose_engine
from app.services.events import EventListener

logger = logging.getLogger("app.bot")

WORKER_STOP_TIMEOUT = 15.0
CONNECT_RETRY_MAX = 60.0


async def wait_for_telegram(bot: Bot) -> None:
    """Retry getMe until Telegram answers: a network outage at startup must not crash the bot."""
    delay = 1.0
    while True:
        try:
            me = await bot.me()
        except (TelegramNetworkError, TelegramServerError) as exc:
            logger.warning("Telegram is unreachable (%s); retrying in %.0f s", type(exc).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, CONNECT_RETRY_MAX)
            continue
        logger.info("connected to Telegram as @%s", me.username)
        return


async def serve(bot: Bot, dispatcher: Dispatcher) -> None:
    await wait_for_telegram(bot)
    # A webhook left by the previous bot blocks getUpdates; pending updates are kept.
    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await set_commands(bot)
    except TelegramAPIError as exc:
        logger.warning("could not register the command menu: %s", type(exc).__name__)
    await dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False)


async def stop_serving(dispatcher: Dispatcher, serving: asyncio.Task[None]) -> None:
    logger.info("stopping the bot")
    try:
        await dispatcher.stop_polling()
    except RuntimeError:  # still connecting to Telegram: polling has not started
        serving.cancel()


async def run() -> None:
    token = get_settings().TELEGRAM_TOKEN.get_secret_value()
    if not token:
        raise SystemExit("TELEGRAM_TOKEN is not set: the bot cannot start.")
    try:
        bot = create_bot(token)
    except TokenValidationError:
        raise SystemExit("TELEGRAM_TOKEN is malformed: the bot cannot start.") from None
    dispatcher = create_dispatcher()
    worker = OutboxWorker(bot)
    listener = EventListener()
    listener.add_callback(worker.wake)
    await listener.start()
    worker_task = asyncio.create_task(worker.run(), name="outbox-worker")
    housekeeping_task = asyncio.create_task(housekeeping(), name="housekeeping")
    serving = asyncio.create_task(serve(bot, dispatcher), name="polling")
    stopping: set[asyncio.Task[None]] = set()

    def on_signal() -> None:
        if not stopping and not serving.done():
            stopping.add(asyncio.create_task(stop_serving(dispatcher, serving)))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_signal)
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await serving
    except TelegramUnauthorizedError:
        raise SystemExit("Telegram rejected TELEGRAM_TOKEN: the bot cannot start.") from None
    finally:
        worker.stop()
        housekeeping_task.cancel()
        try:
            await asyncio.wait_for(worker_task, WORKER_STOP_TIMEOUT)
        except TimeoutError:
            logger.warning("outbox worker did not stop in time; pending rows will be retried")
        with contextlib.suppress(asyncio.CancelledError):
            await housekeeping_task
        await listener.stop()
        await bot.session.close()
        await dispose_engine()
        logger.info("bot stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
