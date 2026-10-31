"""Business rules shared by the website and the bot.

Every function takes an ``AsyncSession`` first, may ``flush`` but never commits: the caller owns
the transaction (see app/db.py). Rule violations raise ``app.services.errors.DomainError``
subclasses with Russian user-facing messages, before anything is written.
"""
