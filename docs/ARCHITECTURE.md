# Architecture and contracts

This is the contract between the parallel builders (web, bot, admin/ops). Product rules come from
[`docs/MVP.md`](MVP.md); this document describes the code that implements them. User-facing texts
are Russian; code, identifiers and comments are English.

## 1. Module map

```
app/
  config.py        Settings (pydantic-settings, env/.env), get_settings() (cached)
  db.py            async engine, get_session (FastAPI), session_scope() (bot/workers), affected_rows()
  clock.py         utcnow(), MOSCOW_TZ, current_year(), age_from_birth_year(), to_moscow()
  enums.py         all enums + label(value, gender=None), choices(EnumCls, gender=None)
  models.py        SQLAlchemy 2 models (spec section 6)
  schemas.py       pydantic input validation shared by web and bot (Russian messages)
  text.py          clean_line/clean_text, find_contacts, plural, normalize_city_key
  services/        THE ONLY PLACE FOR BUSINESS RULES (web and bot call these)
    errors.py      DomainError hierarchy (Russian .message)
    events.py      outbox + NOTIFY, outbox worker helpers, EventListener
    users.py auth.py profiles.py interests.py chat.py guardians.py moderation.py
    stopwords.py   find_stop_words / flag_reason (re-exported as moderation.find_stop_words)
    relations.py   report_between() helper
  web/             FastAPI + Jinja2/HTMX (web agent; admin routes: ops agent)
    main.py        create_app(), module-level app (uvicorn app.web.main:app), /healthz, lifespan
    deps.py        SessionDep, user guards, CSRF, login_user/logout_user, RedirectTo
    templating.py  shared Jinja2 env: templates.TemplateResponse(request, "x.html", ctx)
    routes/        public auth profile catalog interests chats settings admin legal (router = APIRouter())
    templates/base.html, static/
  bot/             aiogram 3 bot + outbox worker (bot agent): python -m app.bot
migrations/        Alembic (async env, URL from settings); 0001 = initial schema
tests/             conftest.py (shared fixtures), services/, test_web_skeleton.py
```

Run: `uvicorn app.web.main:app`, `python -m app.bot`, `alembic upgrade head`.
Tests: `TEST_DATABASE_URL=postgresql+asyncpg://nikah:nikah@127.0.0.1:5432/<your_test_db> .venv/bin/pytest -q`.

## 2. Configuration (`app.config.Settings`)

Field names equal env names. After validation fields hold *effective* values.

| Setting | Default | Notes |
|---|---|---|
| `ENV` | `dev` | `dev` or `prod` |
| `DATABASE_URL` | `postgresql+asyncpg://nikah:nikah@127.0.0.1:5432/nikah` | SQLAlchemy URL |
| `TELEGRAM_TOKEN` | `""` | `SecretStr`: use `.get_secret_value()` |
| `BOT_USERNAME` | `nikah_istina_bot` | leading `@` stripped |
| `SITE_URL` | `http://localhost:8000` | trailing `/` stripped |
| `SECRET_KEY` | dev key | `SecretStr`; prod requires ≥32 chars, not the dev key; empty in dev → dev key |
| `ADMIN_TELEGRAM_IDS` | `[]` | env: `11,22` |
| `PREMODERATION` | `true` | |
| `INTEREST_DAILY_LIMIT` | `10` | rolling 24 h |
| `DEV_LOGIN_ENABLED` | `false` | forced `false` when `ENV=prod` |
| `TELEGRAM_WIDGET_ENABLED` | `false` | after `/setdomain` |
| `SESSION_COOKIE_SECURE` | `ENV == prod` | |

Always call `get_settings()` at use time (not at import) so tests can override values.
`.env.example` lists all variables.

## 3. Transaction convention

- Every service function is `async def f(session: AsyncSession, ...)`. Services **flush, never
  commit or roll back** (they may use SAVEPOINTs internally).
- Services check every rule and **raise `DomainError` before writing anything**, so a caller that
  catches a `DomainError` and renders an error page may still commit safely.
- **Web**: use `SessionDep` from `app.web.deps` (it is `Depends(get_session, scope="function")`):
  the session commits right after the endpoint returns and *before* the response is sent, and
  rolls back if the endpoint raises. One session per request; never hold it inside a streaming
  (SSE) response: open `async with session_scope()` for each DB access inside the stream.
  A generator dependency (`yield`) that depends on `SessionDep` must itself use `scope="function"`.
- **Bot**: an outer middleware wraps each update in `async with session_scope() as session:`
  (commit on success, rollback on exception) and passes `session` + the `User` to handlers.
- **Workers** (outbox, periodic purge): one `session_scope()` per batch.
- `expire_on_commit=False`: objects stay usable after commit. Relationships are lazy and **async
  lazy loading raises**: use what services eager-load (listed below) or
  `await obj.awaitable_attrs.<rel>`.
- `users.delete_account` calls `session.expunge_all()`; do not use previously loaded objects after it.

## 4. Data model (app/models.py)

Public identifier of a person is `users.id` (`/u/{id}`, bot `p_<id>`, "Имя (#id)").
`profiles.user_id` is the profile's primary key (1:1).

| Table | Key columns / rules |
|---|---|
| `users` | `telegram_id` unique, `username`, `first_name`, `phone`, `is_admin`, `is_banned`, `banned_at`, `notify_telegram` (default true), `consent_at`, `bot_active_conversation_id` (FK conversations, SET NULL), `last_seen_at`, `created_at`. Property `display_name`. |
| `profiles` | all fields of spec §3, `status`, `moderation_note`, `flagged`, `flag_reason`, `submitted_at`, `approved_at` (NULL while current content is unapproved), `created_at`, `updated_at` |
| `interests` | `from_user_id`, `to_user_id`, `message`, `status`, `flagged`, `flag_reason`, `created_at`, `decided_at`; unique index on `(LEAST, GREATEST)` of the pair where status in (pending, accepted) |
| `conversations` | ordered pair `user_a_id < user_b_id` (unique; a side becomes NULL when that user deletes the account), `created_from_interest_id`, `last_message_at`, `closed_at`, `closed_by_id`, `close_reason` (`closed_by_user`/`account_deleted`/`user_banned`). Helpers: `is_open`, `is_participant(uid)`, `partner_id(uid)` (None = deleted) |
| `messages` | `conversation_id`, `sender_id`, `text`, `origin` (`web`/`telegram`), `created_at`, `read_at`, `flagged`, `flag_reason` |
| `guardians` | `ward_user_id`, `guardian_user_id` (NULL until accepted), `invite_token` unique, `status` (pending/active/revoked), `expires_at` (invite TTL 7 days), `linked_at`, `revoked_at`; at most one active per ward |
| `login_tokens` | `token_hash` (SHA-256; raw tokens are never stored), `kind` (login/magic), `user_id`, `expires_at` (10 min), `confirmed_at`, `consumed_at` |
| `reports` | `reporter_id`, `target_user_id`, `message_id` (SET NULL), `reason` (ReportReason), `comment`, `status` (open/resolved), `resolution` (ReportAction), `resolved_by_id`, `resolved_at` |
| `outbox` | `kind`, `payload` JSONB, `created_at`, `next_attempt_at`, `attempts`, `last_error`, `sent_at`, `failed_at` |

All user FKs are `ON DELETE CASCADE` (except conversation sides / closer / resolver: `SET NULL`):
deleting a `users` row wipes the person's data (152-FZ). Enums are stored as VARCHAR values.
Schema changes: edit models, `alembic revision --autogenerate`, review, `alembic check` must be clean.

## 5. Enums and labels (app/enums.py)

`Gender` (`.opposite`), `MaritalStatus`, `Education`, `Employment`, `Madhab`, `Prayer`,
`NikahTimeline`, `Relocation`, `ProfileStatus`, `InterestStatus`, `ReportReason`, `ReportStatus`,
`ReportAction`, `GuardianStatus`, `LoginTokenKind`, `LoginTokenState`, `MessageOrigin`,
`ConversationCloseReason`.

`label(value, gender=None)` → Russian label; pass the gender of the person described for
gender-dependent words (`не был женат`/`не была замужем`, `вдовец`/`вдова`, `готов`/`готова`);
without gender a neutral form is used (`не был(а) в браке`). `choices(EnumCls, gender)` →
`[(value, label)]` for selects and bot keyboards. Values are safe for URLs and callback data.

## 6. Validation (app/schemas.py)

- `ProfileCreate` — required: `gender, name, birth_year, city, marital_status, children, education,
  employment, wishes`; optional: `height, weight, occupation, nationality, madhab, prayer,
  nikah_timeline, relocation, about`. Blank strings → missing (required) or `None` (optional).
  Rules: age 18–80 by birth year, height 140–220, weight 35–200, children 0–15, name 2–40 letters
  (spaces, hyphen, apostrophe), city 2–60, occupation ≤100, nationality ≤50, wishes 10–1000,
  about ≤2000; whitespace normalized; **links, e-mails, @usernames and phone numbers are rejected
  in all public text fields**. `extra="ignore"`, so `ProfileCreate.model_validate(dict(form))` works.
- `ProfileUpdate` — partial (`.changes()` = fields that were passed); **no `gender`** (immutable).
- `CatalogFilters` — `age_min, age_max, city, marital_status, has_children, education` (blank = any).
- `InterestIn(message)` ≤500, contacts rejected; `ChatMessageIn(text)` 1–2000 (contacts allowed);
  `ReportIn(reason, comment ≤1000)`; `ModerationNoteIn(note ≤500)`.
- `humanize_errors(exc) -> {field: message}` for forms; `first_error(exc)` → "Поле: сообщение" for
  the bot. `PROFILE_REQUIRED_FIELDS`, `PROFILE_OPTIONAL_FIELDS`, `FIELD_LABELS` drive forms/wizards.
- Services that take raw text (`send_interest`, `send_message`, `create_report`, `reject`) validate
  it themselves and raise `InvalidInput(message)`.

## 7. Services API

Errors are in `app.services.errors`; all subclass `DomainError` with a Russian `.message`
(`str(exc)` is the same). Show it to the user as is in both channels. `NotFound` subclasses are
also used when the caller must not learn whether something exists.

### users
| Function | Semantics | Raises |
|---|---|---|
| `get_or_create_by_telegram(s, telegram_id, username, first_name) -> User` | upsert on first contact (race-safe), refresh names; `profile` loaded | — |
| `get_by_id(s, user_id)`, `get_by_telegram_id(s, telegram_id) -> User \| None` | `profile` loaded | — |
| `set_consent(s, user)` | record consent (first time kept) | — |
| `set_notify(s, user, enabled)` | Telegram notifications on/off | — |
| `touch_last_seen(s, user)` | throttled to once a minute | — |
| `is_admin(user) -> bool` | `is_admin` flag or `telegram_id in ADMIN_TELEGRAM_IDS` (sync) | — |
| `admin_telegram_ids(s) -> list[int]` | who receives moderation notifications | — |
| `ban(s, user)` | idempotent; cancels pending interests (both directions), closes open conversations (`user_banned`, emits `conversation.closed`), revokes guardian links where the user is ward or guardian; catalog hides banned users; administrators cannot be banned (revoke the rights first) | `AdminBanDenied` |
| `unban(s, user)` | lifts the ban; nothing is reopened | — |
| `delete_account(s, user)` | closes open conversations for partners (`account_deleted`), deletes orphaned conversations, deletes the user row (cascade wipes profile, interests, own messages, guardians, tokens, reports); `expunge_all()` | — |

Admin rights are checked by callers (web `AdminDep`, bot `users.is_admin`), except in
`moderation.resolve_report`.

### auth
| Function | Semantics | Raises |
|---|---|---|
| `create_login_token(s, kind=LOGIN, user=None) -> str` | raw token (43 chars), TTL 10 min | `ValueError` for MAGIC without user |
| `login_deep_link(token) -> str` | `https://t.me/{BOT_USERNAME}?start=login_{token}` | — |
| `confirm_login_token(s, token, user)` | bot side, after an explicit button press; idempotent for the same user | `TokenInvalid` (unknown/expired/consumed/other user), `UserBanned` |
| `poll_login_token(s, token) -> int \| None` | site side; consumes on success (single use); None for banned | — |
| `login_token_state(s, token) -> LoginTokenState` | pending/confirmed/consumed/expired (unknown = expired) | — |
| `create_magic_link(s, user) -> str` | `{SITE_URL}/auth/magic/{token}`, TTL 10 min | `UserBanned` |
| `consume_magic_token(s, token) -> int \| None` | single use; None if invalid/expired/banned | — |
| `verify_telegram_widget(data, bot_token, *, max_age=1 day, now=None) -> TelegramWidgetUser \| None` | HMAC-SHA256 per Telegram docs, constant-time compare, `auth_date` ≤ 1 day old; pass exactly Telegram's fields | — |
| `purge_expired_tokens(s, older_than=1 day) -> int` | housekeeping | — |

`TelegramWidgetUser(telegram_id, username, first_name)`.

### profiles
| Function | Semantics | Raises |
|---|---|---|
| `create_profile(s, user, data: ProfileCreate) -> Profile` | pending (+`profile.submitted`) or active if `PREMODERATION=false`; flags stop words | `UserBanned`, `ConsentRequired`, `ProfileAlreadyExists` |
| `update_profile(s, user, data: ProfileUpdate) -> Profile` | applies changed fields; active/rejected/pending → resubmitted (pending + event; active if no premoderation); hidden stays hidden but loses approval | `UserBanned`, `ProfileRequired` |
| `hide(s, user)` / `show(s, user) -> Profile` | active/pending → hidden; hidden → active (or pending + event if content unapproved) | `ProfileRequired`, `ProfileStateError`, (`show`: `UserBanned`) |
| `approve(s, user_id) -> Profile` | pending → active, emits `profile.moderated` | `ProfileNotFound`, `ProfileStateError` |
| `reject(s, user_id, reason) -> Profile` | any non-rejected → rejected with note, emits `profile.moderated` | `InvalidInput`, `ProfileNotFound`, `ProfileStateError` |
| `get_profile(s, user_id)` / `get_profile_by_user(s, user) -> Profile \| None` | no visibility checks; `user` loaded | — |
| `get_viewable_profile(s, viewer, user_id) -> Profile` | own profile, catalog-visible profile, or counterpart of a pending/accepted interest or conversation; `user` loaded | `ProfileRequired`, `UserBanned`, `ProfileNotFound` |
| `list_catalog(s, viewer, filters=None, page=1, per_page=20) -> (list[Profile], total)` | ACTIVE, opposite gender, not banned, not the viewer, no report between them in either direction; newest approved first; per_page ≤ 50; `user` loaded | `ProfileRequired`, `UserBanned` |
| `list_pending(s, *, limit, offset) -> list[Profile]` | moderation queue, oldest first | — |
| `public_counts(s) -> {Gender: int}` | published profiles for the landing page | — |

Pending profiles may browse the catalog but are not shown and cannot send interests.

### interests
| Function | Semantics | Raises |
|---|---|---|
| `send_interest(s, from_user, to_user_id, message=None) -> Interest` | see rules below; emits `interest.created` | `UserBanned`, `InterestToSelf`, `InvalidInput`, `ProfileNotActive`, `ProfileNotFound`, `InterestAlreadyExists`, `InterestDeclinedBefore`, `InterestLimitReached` |
| `accept(s, interest_id, user) -> Conversation` | recipient only; creates the conversation; emits `interest.accepted` | `UserBanned`, `InterestNotFound`, `InterestNotPending`, `ProfileNotFound`, `InterestAlreadyExists` |
| `decline(s, interest_id, user) -> Interest` | recipient only; emits `interest.declined` | `UserBanned`, `InterestNotFound`, `InterestNotPending` |
| `cancel(s, interest_id, user) -> Interest` | sender only, pending only; no event | `InterestNotFound`, `InterestNotPending` |
| `get_interest(s, interest_id, user) -> Interest` | sender or recipient; both users + profiles loaded | `InterestNotFound` |
| `get_relation(s, viewer, other_user_id) -> PairRelation` | `.interest` (latest, either direction), `.conversation` — for buttons on `/u/{id}` | — |
| `list_incoming(s, user, statuses=(PENDING,)) -> list[Interest]` | newest first, banned senders hidden, `from_user.profile` loaded | — |
| `list_outgoing(s, user, limit=100) -> list[Interest]` | all statuses, `to_user.profile` loaded | — |
| `pending_incoming_count(s, user) -> int` | badge | — |

Rules of `send_interest`: sender not banned and has an ACTIVE profile; recipient profile ACTIVE,
opposite gender, owner not banned, no report between them; no pending/accepted interest and no
conversation (open or closed) for the pair in either direction; the recipient has not declined an
interest from this sender before (the recipient may still write first); at most
`INTEREST_DAILY_LIMIT` interests created by the sender in the last 24 hours (cancelled ones count);
message ≤500 chars without contacts; stop words flag the interest.

### chat
| Function | Semantics | Raises |
|---|---|---|
| `list_conversations(s, user) -> list[ConversationSummary]` | most recent activity first | — |
| `get_conversation_for_participant(s, conversation_id, user) -> Conversation` | `user_a`/`user_b` + profiles loaded | `ConversationNotFound` |
| `send_message(s, conversation_id, sender, text, origin) -> Message` | participant, conversation open, partner exists, sender not banned; stop words flag; emits `message.created` | `UserBanned`, `ConversationNotFound`, `PartnerDeleted`, `ConversationClosed`, `InvalidInput` |
| `list_messages(s, conversation, *, after_id=None, before_id=None, limit=50) -> list[Message]` | ascending; `after_id` = next page after it (resync), else the latest `limit` (before `before_id`) | — |
| `mark_read(s, conversation, reader) -> int` | marks the partner's messages read | — |
| `close_conversation(s, conversation, user)` | «Завершить общение»; emits `conversation.closed` | `ConversationNotFound`, `ConversationClosed` |
| `unread_total(s, user) -> int` | badge | — |
| `set_bot_active_conversation(s, user, conversation_id \| None)` | bot «Ответить» / `/stop` | `ConversationNotFound`, `PartnerDeleted`, `ConversationClosed` |
| `get_conversation_between(s, a, b)`, `create_conversation(...)`, `force_close(...)` | internal helpers for other services | — |

`ConversationSummary(conversation, partner: User | None (profile loaded; None = deleted),
last_message, unread_count, partner_has_guardian)` + property `partner_profile`. Closing a
conversation clears `bot_active_conversation_id` of users who had it active. Show
"собеседник удалил аккаунт" when `conversation.partner_id(me) is None`.

### guardians
| Function | Semantics | Raises |
|---|---|---|
| `create_invite(s, ward) -> str` | deep link `https://t.me/{BOT_USERNAME}?start=wali_{token}`, TTL 7 days; revokes previous pending invites; current guardian stays until a new one accepts | `UserBanned` |
| `invite_link(token) -> str`, `get_pending_invite(s, ward) -> Guardian \| None` | re-show the link in settings | — |
| `get_invite(s, token) -> Guardian \| None` | valid pending invite, `ward` + `ward.profile` loaded (bot confirmation screen) | — |
| `accept_invite(s, token, guardian_user) -> Guardian` | after explicit confirmation; replaces the previous active guardian; emits `guardian.linked` | `GuardianInviteInvalid`, `GuardianSelf`, `UserBanned` |
| `revoke(s, ward) -> bool` | disconnect active guardian + cancel pending invites (no event) | — |
| `get_active_guardian(s, ward_user_id) -> Guardian \| None` | `guardian` (User) loaded | — |
| `has_active_guardian(s, user_id) -> bool`, `users_with_active_guardian(s, ids) -> set[int]` | badge «👁 Вали подключён — переписка видна вали» | — |
| `active_guardians_for(s, ids) -> {ward_id: guardian_user_id}` | batch | — |
| `list_wards(s, guardian_user) -> list[Guardian]` | active links, `ward` + `ward.profile` loaded | — |

### moderation
| Function | Semantics | Raises |
|---|---|---|
| `create_report(s, reporter, target_user_id, reason: ReportReason, comment=None, message_id=None) -> Report` | `message_id` must be the target's message in a conversation shared with the reporter; one open report per pair; emits `report.created` | `UserBanned`, `ReportToSelf`, `InvalidInput`, `UserNotFound`, `NotFound`, `AlreadyReported` |
| `get_report(s, report_id)`, `list_reports(s, status=OPEN, *, limit, offset)` | reporter/target (+profiles) and message loaded | — |
| `resolve_report(s, report_id, admin, action: ReportAction) -> Report` | `dismiss`; `hide_profile` = `profiles.reject` with a standard note; `ban_user` = `users.ban` + resolve all open reports on the target | `PermissionDenied`, `ReportNotFound`, `ReportAlreadyResolved`, `AdminBanDenied` |
| `list_flagged_messages(s, *, limit, offset) -> list[Message]` | newest first, `sender.profile` loaded | — |
| `find_stop_words(text) -> list[str]` | scam markers (money transfers, cards, card-like numbers, crypto, investments, bets, loans, prepayment, webcam; Russian + translit) | — |

Profiles and interests have `flagged`/`flag_reason` too (filter `Profile.flagged` in the admin).
Flagged content is never blocked.

### events
| Function | Semantics |
|---|---|
| `emit(s, kind: EventKind, payload, notify_user_ids=()) -> Outbox` | outbox row + `pg_notify('events', json)` in the same transaction (delivered on commit only) |
| `fetch_pending(s, limit=50) -> list[Outbox]` | due undelivered rows, `FOR UPDATE SKIP LOCKED` (several workers are safe); process them in the same transaction |
| `mark_sent(s, row)` / `mark_failed(s, row, error, *, max_attempts=8)` | retry backoff 10 s·2ⁿ (≤1 h), then `failed_at` |
| `purge_processed(s, older_than=30 days) -> int` | housekeeping |
| `EventListener(dsn=None, *, channel="events", ...)` | `await start()/stop()` or `async with`; `async with listener.subscribe() as sub: async for event in sub`; `add_callback(fn)` (sync or async); `wait_connected(timeout)`; reconnects with backoff and then delivers `Event(kind=LISTENER_RECONNECTED)` to subscribers/callbacks |

`Event(kind, outbox_id, user_ids: frozenset[int], data: dict)` — `data` is the whole NOTIFY JSON.

## 8. Events

NOTIFY JSON = `{"kind", "outbox_id", **payload}`; the outbox `payload` is the same minus `kind`
and `outbox_id`. Every payload has `user_ids` = users whose open web pages must update (SSE).
IDs only, never personal data: consumers load current data from the DB when they handle the event
and must tolerate rows that no longer exist (deleted account → skip and `mark_sent`).

| Kind | Payload (besides `user_ids`) | `user_ids` | Telegram delivery (bot worker) |
|---|---|---|---|
| `message.created` | `conversation_id, message_id, sender_id, recipient_id, origin, guardians: [{ward_user_id, guardian_user_id}]` | sender, recipient | recipient: «💬 Имя (#id): текст» + «Ответить»; each guardian: copy marked whose conversation it is (re-check `get_active_guardian(ward)` before sending) |
| `interest.created` | `interest_id, from_user_id, to_user_id` | both | `to_user`: profile card + «✅ Принять» / «❌ Отклонить» |
| `interest.accepted` | `interest_id, conversation_id, from_user_id, to_user_id` | both | `from_user`: accepted, chat opened |
| `interest.declined` | `interest_id, from_user_id, to_user_id` | both | `from_user` (gentle wording) |
| `conversation.closed` | `conversation_id, closed_by_id (null for ban/deletion), reason` | both participants | the other participant(s), text by `reason` |
| `profile.submitted` | `user_id` | — | admins (`users.admin_telegram_ids`): card + «Одобрить» / «Отклонить»; skip if no longer pending |
| `profile.moderated` | `user_id, status` (`active`/`rejected`) | owner | owner; on rejection include `profile.moderation_note` |
| `report.created` | `report_id, reporter_id, target_user_id, message_id` | — | admins, with action buttons |
| `guardian.linked` | `guardian_id, ward_user_id, guardian_user_id` | ward, guardian | ward: «Вали подключён» |

Telegram delivery honours `users.notify_telegram` and skips banned/deleted recipients. Guardian
copies are the purpose of the wali link and are sent regardless of the guardian's
`notify_telegram`. If Telegram answers "bot was blocked by the user", mark the row sent (no retry).

Outbox worker pattern (bot): wake on every `EventListener` event and at least every 5 seconds:

```python
async with session_scope() as session:
    for row in await events.fetch_pending(session, limit=20):
        try:
            await deliver(row)            # loads data by IDs, sends via aiogram Bot
            await events.mark_sent(session, row)
        except TelegramRetryAfter / network errors as exc:
            await events.mark_failed(session, row, repr(exc))
```

Delivery is at-least-once. Housekeeping (e.g. daily from the bot process):
`events.purge_processed`, `auth.purge_expired_tokens`.

Web SSE: start one `EventListener` per process in `lifespan`; each SSE stream subscribes and
forwards events whose `user_ids` contain the current user; on `LISTENER_RECONNECTED` tell the
browser to resync (it re-fetches `list_messages(after_id=…)`).

## 9. Web foundation (app/web)

- `create_app()` installs `SessionMiddleware` (signed cookie `istina_session`, 30 days, `HttpOnly`,
  `SameSite=Lax`, `Secure` = `SESSION_COOKIE_SECURE`), `/static`, `/healthz` (DB ping), all routers,
  app-wide CSRF dependency and the `RedirectTo` handler (303, or `204 + HX-Redirect` for HTMX).
  OpenAPI/docs are disabled. Extend `lifespan` in `main.py` for startup/shutdown work.
- Dependencies (`app.web.deps`): `SessionDep`; `OptionalUserDep` (User | None, `profile` loaded,
  touches `last_seen_at`); `LoginDep` (any logged-in user, incl. banned — logout, delete account);
  `UserDep` (logged in, not banned → else redirect to `/login?next=…` / 403); `ProfileUserDep`
  (also has a profile → else redirect `/profile/new`); `AdminDep` (403 unless `users.is_admin`).
- Sessions: `login_user(request, user)` (clears the session first: no fixation; rotates CSRF),
  `logout_user(request)`; `safe_next_url(next)` against open redirects.
- CSRF: every POST/PUT/PATCH/DELETE needs the session token in form field `csrf_token` or header
  `X-CSRF-Token`; templates: `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">`,
  HTMX: `hx-headers='{"X-CSRF-Token": "{{ csrf_token() }}"}'` on `<body>`.
- Templates: `from app.web.templating import templates`; globals `csrf_token()`, `label()`,
  `choices()`, `settings`, `age()`, `plural()`; filters `age`, `plural`, `msk`, `msk_time`, `msk_date`
  (Moscow time). `base.html` has blocks `title`, `head`, `content`.
- Login flows: `/login` creates `auth.create_login_token`, keeps the raw token in the session,
  shows `auth.login_deep_link(token)` and polls (`poll_login_token` / `login_token_state`).
  Magic links: the bot should send them as an inline URL button (no link preview that could consume
  the single-use token). Widget: pass only Telegram's query fields to `verify_telegram_widget`,
  then `users.get_or_create_by_telegram`.
- Mark catalog/profile pages `noindex` (meta + `robots.txt`); keep everything self-hosted (no CDN).

## 10. Tests

- `tests/conftest.py` sets the environment before importing `app`: `DATABASE_URL` =
  `TEST_DATABASE_URL` (default `.../nikah_test`; **use your own database**), `ENV=dev`,
  `PREMODERATION=true`, `INTEREST_DAILY_LIMIT=10`, `SITE_URL=http://testserver`,
  `TELEGRAM_TOKEN=123456:TEST-TOKEN`, `ADMIN_TELEGRAM_IDS=""`, `SESSION_COOKIE_SECURE=false`.
- Schema is recreated from the models once per run; all tables are truncated before every test.
  One event loop for the whole session (configured in `pyproject.toml`).
- Fixtures: `session`; `settings`; `override_settings(**values)`; factories (they **commit**):
  `make_user(telegram_id=None, username=None, first_name="Тест", consent=True, is_admin=False,
  is_banned=False, notify_telegram=True)`, `make_profile(user=None, gender=MALE, status=ACTIVE,
  **fields)`, `make_pair() -> (man, woman)` with active profiles, `make_conversation(sender,
  recipient)`; `notifications` (asyncio.Queue of decoded NOTIFY payloads); `web_client`
  (httpx.AsyncClient on `create_app()`; do not use Starlette's TestClient — it runs another event
  loop and breaks the asyncpg pool); `login_client(client, user) -> csrf_token`;
  helper `tests.conftest.session_cookie(data)`.
- Each test directory needs an `__init__.py` (test packages: `tests/services`, `tests/web`,
  `tests/bot`, `tests/admin`). Mock the aiogram `Bot`; never call api.telegram.org.

## 11. File ownership

| Owner | Files |
|---|---|
| core (this foundation) | `app/config.py`, `app/db.py`, `app/clock.py`, `app/enums.py`, `app/models.py`, `app/schemas.py`, `app/text.py`, `app/services/**`, `migrations/**`, `alembic.ini`, `requirements.txt`, `pyproject.toml`, `.env.example`, `tests/conftest.py`, `tests/services/**`, `tests/test_web_skeleton.py`, `docs/ARCHITECTURE.md` |
| web agent | `app/web/**` except `app/web/routes/admin.py` and `app/web/templates/admin/**`; `tests/web/**` |
| bot agent | `app/bot/**`, `tests/bot/**` |
| ops/admin agent | `app/web/routes/admin.py`, `app/web/templates/admin/**`, Docker/compose/Caddy, `README.md`, `docs/DEPLOY.md`, `docs/LEGACY_IMPORT.md`, `scripts/**`, `tests/admin/**` |

Shared code (`app/models.py`, `app/services/**`, `app/schemas.py`, `app/enums.py`,
`tests/conftest.py`, `requirements.txt`) may only be **extended** (append new functions,
fields, fixtures, dependencies) — never changed incompatibly. A new column needs a new Alembic
revision (coordinate: one migration head).

## 12. Decisions where the spec was silent

1. Public id = `users.id`; profile PK = `user_id`.
2. Gender cannot be changed after the profile is created (delete the account to start over).
3. Contacts are rejected in public profile texts and interest messages (consent first), allowed in
   chats after acceptance.
4. A decline is final for that sender → recipient direction; the recipient may still write first.
5. One conversation per pair, ever: after it is closed, neither side can send a new interest.
6. Any report (even dismissed) hides the two users from each other's catalog and blocks interests.
7. Ban cancels pending interests, closes open chats and revokes wali links; unban reopens nothing.
8. Account deletion is a hard delete; the partner keeps their own messages in a closed chat.
   No record is kept to stop a banned person from re-registering with the same Telegram account
   (question for the lawyer / product owner).
9. Login tokens are stored hashed; banned users cannot log in to the site.
10. Wali invites expire after 7 days; a wali cannot be one's own wali.
11. Editing a profile always goes back to moderation when `PREMODERATION` is on (including
    re-submission of a pending one, so admins see the latest content).
12. Profiles with stop words are flagged (not blocked), like messages and interests.
