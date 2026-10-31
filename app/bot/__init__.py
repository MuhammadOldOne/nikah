"""Telegram bot @nikah_istina_bot (aiogram 3): ``python -m app.bot``.

- ``__main__``: polling + outbox worker + housekeeping in one process, graceful shutdown;
- ``dispatcher``: Bot/Dispatcher factories, error handler, command menu;
- ``middlewares``: one DB transaction per update, the current account, admin-only routers;
- ``handlers``: thin handlers per menu section; every rule comes from ``app.services``;
- ``render`` / ``keyboards`` / ``texts``: pure rendering (all user text HTML-escaped);
- ``outbox``: delivery of domain events (messages, interests, moderation…) to Telegram.
"""
