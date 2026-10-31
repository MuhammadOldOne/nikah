"""FSM states of multi-step dialogs. Flow data is kept under one key per flow (see handlers)."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ProfileForm(StatesGroup):
    filling = State()
    confirm = State()
    consent = State()
    adult = State()


class CatalogForm(StatesGroup):
    age = State()
    city = State()
    interest_message = State()


class ReportForm(StatesGroup):
    comment = State()


class AdminForm(StatesGroup):
    reject_reason = State()
