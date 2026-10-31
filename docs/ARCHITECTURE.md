# Architecture and contracts

This is the contract between the parallel builders (web, bot, admin/ops). Product rules come from
[`docs/LEGACY_BOT.md`](LEGACY_BOT.md) (integration with the existing @nikah_istina_bot; it wins
where it differs) and [`docs/MVP.md`](MVP.md); this document describes the code that implements
them. User-facing texts are Russian; code, identifiers and comments are English.

## 1. Module map

```
app/
  config.py        Settings (pydantic-settings, env/.env), get_settings() (cached)
  db.py            async engine, get_session (FastAPI), session_scope() (bot/workers), affected_rows()
  clock.py         utcnow(), MOSCOW_TZ, current_year(), age_from_birth_year(), to_moscow(),
                   msk_day_time() («01.04 13:05», with the year when it is not the current one)
  enums.py         all enums + label(value, gender=None), choices(EnumCls, gender=None)
  models.py        SQLAlchemy 2 models (spec section 6 + LEGACY_BOT.md)
  schemas.py       pydantic input validation shared by web and bot (Russian messages), AgeIn
  text.py          clean_line/clean_text, find_contacts/hide_contacts (contact filter), plural,
                   normalize_city_key, profile_title («Анкета №512»), gender_word («Брат»/«Сестра»)
  logutil.py       exc_summary(exc): exception type + frames without the message (no personal data in logs)
  services/        THE ONLY PLACE FOR BUSINESS RULES (web and bot call these)
    errors.py      DomainError hierarchy (Russian .message)
    events.py      outbox + NOTIFY, outbox worker helpers, EventListener
    users.py auth.py profiles.py interests.py chat.py guardians.py moderation.py
    witness.py     witness actions (flag / say / close), admin-checked
    meta.py        app_meta key-value store, legacy import marker
    stopwords.py   find_stop_words / flag_reason (re-exported as moderation.find_stop_words)
    relations.py   report_between(), lock_users() helpers
  web/             FastAPI + Jinja2/HTMX (web agent; admin routes: ops agent)
    main.py        create_app(), module-level app (uvicorn app.web.main:app), /healthz, lifespan
    deps.py        SessionDep, user guards, CSRF, login_user/logout_user, RedirectTo
    gates.py       profile_gate(): friendly pages for ProfileNotActive (pending/rejected/hidden)
    templating.py  shared Jinja2 env: templates.TemplateResponse(request, "x.html", ctx)
    routes/        public auth consent profile catalog interests chats live settings admin legal
                   (router = APIRouter())
    templates/base.html, static/
  bot/             aiogram 3 bot + outbox worker (bot agent): python -m app.bot; handlers/legacy.py
                   (old callback data), handlers/witness.py (witness buttons), handlers/consent.py
scripts/          import_legacy.py (--inspect / --import), make_admin.py, backup.sh
migrations/        Alembic (async env, URL from settings); 0001 = initial schema, 0002 = users.session_version + outbox (kind, created_at),
                   0003 = legacy integration (profiles.number, no name, nullable new fields, messages.kind, open-pair
                   conversations, pending-per-direction interests, app_meta)
tests/             conftest.py (shared fixtures), services/ (incl. test_migrations.py), web/, bot/, admin/,
                   import/ (synthetic legacy SQLite), fixtures/legacy_schema.sql (DDL only), e2e/
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
| `ADMIN_TELEGRAM_IDS` | `[]` | env: `11,22` (spaces/semicolons also accepted) |
| `PREMODERATION` | `true` | |
| `INTEREST_DAILY_LIMIT` | `10` | rolling 24 h |
| `MAX_OPEN_CONVERSATIONS` | `1` | open chats per person; `interests.accept` refuses at the limit (≥1) |
| `CHAT_RATE_LIMIT_PER_MIN` | `20` | chat messages per sender per minute, site + Telegram together (≥1) |
| `WITNESS_CHAT_ID` | `None` | `int`, usually a negative group id; unset/blank → witness notices go to each admin privately |
| `TELEGRAM_PROXY` | `None` | `SecretStr \| None` (`http://` CONNECT, `socks5://`, `socks4://`; may hold credentials — never log it); the bot passes `.get_secret_value()` to `AiohttpSession(proxy=…)` (`app.bot.dispatcher.telegram_session`, `aiohttp-socks`); an `https://` proxy is refused at startup (decision B8) |
| `DEV_LOGIN_ENABLED` | `false` | forced `false` when `ENV=prod` |
| `TELEGRAM_WIDGET_ENABLED` | `false` | after `/setdomain` |
| `SESSION_COOKIE_SECURE` | `ENV == prod` | |

Legacy names from the old bot's `.env` are accepted: `BOT_TOKEN` → `TELEGRAM_TOKEN`,
`ADMIN_IDS` → `ADMIN_TELEGRAM_IDS` (same comma format). A non-empty new name wins; an empty new value
(e.g. copied from `.env.example`) does not hide the legacy one. After validation read only the new
fields (`Settings.BOT_TOKEN`/`ADMIN_IDS` exist only to receive the legacy values). `DB_PATH` is
read by the import script only.

Always call `get_settings()` at use time (not at import) so tests can override values.
`.env.example` lists all variables (the ops agent owns it from now on).

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
  Telegram calls cannot be rolled back, so a Bot API request middleware (`CommitBeforeTelegram`)
  commits the update's work **before every Bot API call**: a call that fails afterwards (network
  error, "query is too old") never undoes an action the user was told about. A rejected
  `answerCallbackQuery` does not abort the handler.
- **Lock order**: users rows (several: ascending id, `relations.lock_users`) → interest row →
  conversation row → message row. The bot and the site update the sender's `last_seen_at` before
  sending; `chat.force_close` updates users before the conversation; `interests.send_interest` and
  `accept` lock both users of the pair (FOR NO KEY UPDATE) before any pair rule is checked. Two
  people acting on each other in the same second, both right after a `last_seen_at` touch, can
  still deadlock; PostgreSQL aborts one transaction (the request fails, nothing is half-written).
- **Workers** (outbox, periodic purge): one `session_scope()` per batch.
- `expire_on_commit=False`: objects stay usable after commit. Relationships are lazy and **async
  lazy loading raises**: use what services eager-load (listed below) or
  `await obj.awaitable_attrs.<rel>`.
- `users.delete_account` calls `session.expunge_all()`; do not use previously loaded objects after it.

## 4. Data model (app/models.py)

Public identifier of a person is the **profile number** `profiles.number` («Анкета №512»,
`/p/{number}`, bot deep link `p_<number>`; render with `app.text.profile_title(profile)` and
`gender_word(gender)`). `users.id` is internal: never show it. There is no name field.
`profiles.user_id` is the profile's primary key (1:1). The site has no `/u/{id}` routes: profiles
live at `/p/{number}` (`deps.NumberPath`, 1 … 2³¹−1: `profiles.number` is INTEGER).

| Table | Key columns / rules |
|---|---|
| `users` | `telegram_id` unique, `username`, `first_name`, `phone`, `is_admin`, `is_banned`, `banned_at`, `notify_telegram` (default true), `consent_at`, `bot_active_conversation_id` (FK conversations, SET NULL), `last_seen_at`, `session_version` (web sessions are valid while it matches the value stored at login), `created_at`. Property `display_name`. |
| `profiles` | `number` (unique, NOT NULL, default `nextval('profiles_number_seq')`, START 500, sequence OWNED BY the column; fetched on INSERT via `eager_defaults`), all fields of spec §3 except the name; **`marital_status`, `children`, `education`, `employment` are nullable** (profiles migrated from the legacy bot), `status`, `moderation_note`, `flagged`, `flag_reason`, `submitted_at`, `approved_at` (NULL while current content is unapproved), `created_at`, `updated_at` |
| `interests` | `from_user_id`, `to_user_id`, `message`, `status`, `flagged`, `flag_reason`, `created_at`, `decided_at`; unique `uq_interests_pending_direction` on `(from_user_id, to_user_id)` WHERE `status = 'pending'` only — every legacy state is valid (both directions pending, accepted in both directions over time, any number of decided rows); the other rules are in `services.interests` |
| `conversations` | ordered pair `user_a_id < user_b_id` (a side becomes NULL when that user deletes the account); **several per pair over time, at most one open** (`uq_conversations_open_pair` WHERE `closed_at IS NULL`; plain index `ix_conversations_pair`), `created_from_interest_id`, `last_message_at`, `closed_at`, `closed_by_id`, `close_reason` (`closed_by_user`/`account_deleted`/`user_banned`/`closed_by_witness`). Helpers: `is_open`, `is_participant(uid)`, `partner_id(uid)` (None = deleted) |
| `messages` | `conversation_id`, `kind` (`user`/`witness`), `sender_id` (CHECK `ck_messages_kind_sender`: user messages have a sender, witness messages none), `text`, `origin` (`web`/`telegram`), `created_at`, `read_at`, `flagged`, `flag_reason` (comma-separated: `contact-leak filter`, stop words, `witness`, `user report`). Properties `is_witness`, `contacts_hidden`. Witness messages are nobody's unread (unread/mark_read compare `sender_id`) |
| `guardians` | `ward_user_id`, `guardian_user_id` (NULL until accepted), `invite_token` unique, `status` (pending/active/revoked), `expires_at` (invite TTL 7 days), `linked_at`, `revoked_at`; at most one active per ward |
| `login_tokens` | `token_hash` (SHA-256; raw tokens are never stored), `kind` (login/magic/widget — a used Login Widget signature, kept so it cannot be replayed), `user_id`, `expires_at` (10 min), `confirmed_at`, `consumed_at` |
| `reports` | `reporter_id`, `target_user_id`, `message_id` (SET NULL), `reason` (ReportReason), `comment`, `status` (open/resolved), `resolution` (ReportAction), `resolved_by_id`, `resolved_at` |
| `outbox` | `kind`, `payload` JSONB, `created_at` (index with `kind`: rate limits), `next_attempt_at`, `attempts`, `last_error`, `sent_at`, `failed_at` |
| `app_meta` | `key` (PK, ≤64), `value` JSONB, `updated_at`: installation facts; `legacy_import` = `{"imported_at", "counts": {table: rows}}` once the legacy import committed (`services.meta`) |

All user FKs are `ON DELETE CASCADE` (except conversation sides / closer / resolver: `SET NULL`):
deleting a `users` row wipes the person's data (152-FZ). Enums are stored as VARCHAR values.
Schema changes: edit models, `alembic revision --autogenerate`, review, `alembic check` must be clean.

## 5. Enums and labels (app/enums.py)

`Gender` (`.opposite`), `MaritalStatus`, `Education`, `Employment`, `Madhab`, `Prayer`,
`NikahTimeline`, `Relocation`, `ProfileStatus`, `InterestStatus`, `ReportReason`, `ReportStatus`,
`ReportAction`, `GuardianStatus`, `LoginTokenKind`, `LoginTokenState`, `MessageOrigin`,
`MessageKind` (`user`/`witness`), `ConversationCloseReason` (+ `closed_by_witness`).

`label(value, gender=None)` → Russian label; pass the gender of the person described for
gender-dependent words (`не был женат`/`не была замужем`, `вдовец`/`вдова`, `готов`/`готова`);
without gender a neutral form is used (`не был(а) в браке`). `choices(EnumCls, gender)` →
`[(value, label)]` for selects and bot keyboards. Values are safe for URLs and callback data.

## 6. Validation (app/schemas.py)

- `ProfileCreate` (new profiles: site wizard, bot registration) — required: `gender, birth_year,
  city, marital_status, children, education, employment, wishes`; optional: `height, weight,
  occupation, nationality, madhab, prayer, nikah_timeline, relocation, about`. Blank strings →
  missing (required) or `None` (optional). Rules: age 18–80 by birth year, height 140–220, weight
  35–200, children 0–15, city 2–60, occupation ≤100, nationality ≤50, wishes 10–1000, about ≤2000;
  whitespace normalized; **links, e-mails, @usernames and phone numbers are rejected in all public
  text fields** (`find_contacts`, the same engine as the chat filter). `extra="ignore"`, so
  `ProfileCreate.model_validate(dict(form))` works.
- `ProfileUpdate` — partial (`.changes()` = fields that were passed; only those are validated);
  **no `gender`** (immutable); a required field cannot be cleared. A form that posts every field
  must use **`ProfileUpdate.from_form(raw: Mapping, current_profile) -> ProfileUpdate`**: values equal
  to the stored ones (after whitespace normalization; blank = None) are dropped before validation,
  so a migrated profile whose legacy texts break today's rules (short wishes, odd city) can still be
  completed. Raises `ValidationError` for the changed fields only.
- `PROFILE_REQUIRED_FIELDS`, `PROFILE_OPTIONAL_FIELDS`, `PROFILE_FREE_TEXT_FIELDS` (`city,
  nationality, occupation, about, wishes`: re-moderated when changed), `FIELD_LABELS` (has `"age"`).
- Age in the bot: `AgeIn(age=text).birth_year(today=None) -> int` (18–80, message «Возраст — от 18
  до 80 лет.», `first_error` → «Возраст: …»); `birth_year_from_age(age, today=None) -> int` =
  current Moscow year − age (`today: date` for tests). The site asks the birth year.
- `CatalogFilters` — `age_min, age_max, city, marital_status, has_children, education` (blank = any).
- `InterestIn(message)` ≤500, contacts rejected; `ChatMessageIn(text)` 1–2000 (contacts are
  hidden by `chat.send_message`, not rejected);
  `ReportIn(reason, comment ≤1000)`; `ModerationNoteIn(note ≤500)`.
- `humanize_errors(exc) -> {field: message}` for forms; `first_error(exc)` → "Поле: сообщение" for
  the bot.
- `app.text`: `hide_contacts(text) -> (text, hidden: bool)` replaces phones (10+ digits joined by
  spaces/brackets/dots/dashes, `+7`/`8` forms, 123-45-67), links (scheme, `www.`, `tg://`), short
  links (`t.me/…`, `т.ме/…`, `wa.me/…`), domains (also «site точка ру», «site dot com», `site[.]ru`,
  spaced dots), e-mails (also «собака», «(at)») and @usernames with `CONTACT_PLACEHOLDER`
  «[скрыто свидетелем]»; matching runs on an NFKC/lowercase fold with every Unicode digit as ASCII
  (fullwidth, Arabic-Indic, circled); dates are set aside, so ages, years, dates, times, heights and
  prices below ten digits stay. `find_contacts(text) -> list[str]` (same engine).
  `profile_title(profile)` → «Анкета №512» («Новая анкета» for an unsaved draft),
  `gender_word(gender)` → «Брат»/«Сестра».
- Services that take raw text (`send_interest`, `send_message`, `create_report`, `reject`) validate
  it themselves and raise `InvalidInput(message)`.

## 7. Services API

Errors are in `app.services.errors`; all subclass `DomainError` with a Russian `.message`
(`str(exc)` is the same). Show it to the user as is in both channels. `NotFound` subclasses are
also used when the caller must not learn whether something exists.

**Consent gate** (152-FZ; migrated users have `consent_at = NULL`): `users.needs_consent(user)`.
Without consent these raise `ConsentRequired`: `interests.send_interest/accept/decline`,
`chat.send_message`, `profiles.create_profile/update_profile/show`, `profiles.list_catalog/
catalog_position` and opening non-counterpart profiles, `moderation.create_report` (except a
message report), `guardians.create_invite`. Allowed without consent: reading rules/help/legal
pages, `users.set_consent`, `users.delete_account`, receiving notifications, reading own chats and
interests and counterparts' profiles, withdrawing (`interests.cancel`, `chat.close_conversation`,
`profiles.hide`, `guardians.revoke`) and reporting a message in one's own chat. The UI should ask
for consent (link `/consent`) when it catches `ConsentRequired` and on the first visit.

**Gating** (PREMODERATION on, legacy rule against fakes collecting sisters' profiles): browsing
(`list_catalog`, `catalog_position`, catalog-visible profiles) needs consent and the viewer's own
ACTIVE profile; `send_interest` always needs an ACTIVE profile. Errors: `ProfileRequired` (no
profile, «Сначала заполните анкету.»), `ProfilePending` («Ваша анкета ещё не одобрена. Смотреть
анкеты и отправлять отклики можно после модерации.»), `ProfileRejected`, `ProfileHidden` — the
last three subclass `ProfileNotActive`. Counterparts (pending/accepted interest or any conversation)
stay viewable whatever the viewer's status (an edited profile must not cut off an open chat).

### users
| Function | Semantics | Raises |
|---|---|---|
| `get_or_create_by_telegram(s, telegram_id, username, first_name) -> User` | upsert on first contact (race-safe), refresh names; `profile` loaded | — |
| `get_by_id(s, user_id)`, `get_by_telegram_id(s, telegram_id) -> User \| None` | `profile` loaded | — |
| `resolve_user_ref(s, ref: str) -> User \| None` | admin commands `/ban`, `/unban`: digits = Telegram id (legacy), «№512» / «#512» = profile number; `profile` loaded; None if nobody | `InvalidInput` (`USER_REF_HINT`) |
| `needs_consent(user) -> bool`, `require_consent(user)` | consent gate (sync) | `ConsentRequired` |
| `set_consent(s, user)` | record consent (first time kept) | — |
| `set_notify(s, user, enabled)` | Telegram notifications on/off | — |
| `touch_last_seen(s, user)` | throttled to once a minute | — |
| `end_web_sessions(s, user)` | raises `session_version`: every site session of the user ends (logout) | — |
| `is_admin(user) -> bool` | `is_admin` flag or `telegram_id in ADMIN_TELEGRAM_IDS` (sync) | — |
| `admin_telegram_ids(s) -> list[int]` | who receives moderation notifications | — |
| `ban(s, user)` | idempotent; cancels pending interests (both directions), closes open conversations (`user_banned`, emits `conversation.closed` + `witness.conversation_closed`), revokes guardian links where the user is ward or guardian; catalog hides banned users; administrators cannot be banned (revoke the rights first) | `AdminBanDenied` |
| `unban(s, user)` | lifts the ban; nothing is reopened; sessions from before the ban stay ended | — |
| `delete_account(s, user)` | closes open conversations for partners (`account_deleted`), deletes orphaned conversations, deletes the user row (cascade wipes profile, interests, own messages, guardians, tokens, reports); `expunge_all()` | — |

Admin rights are checked by callers (web `AdminDep`, bot `users.is_admin`), except in
`moderation.resolve_report` and `services.witness` (called from a Telegram group).

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
| `verify_telegram_widget(data, bot_token, *, max_age=5 min, now=None) -> TelegramWidgetUser \| None` | pure check: HMAC-SHA256 per Telegram docs, constant-time compare, `auth_date` ≤ 5 minutes old; pass exactly Telegram's fields | — |
| `consume_widget_login(s, data, bot_token, *, now=None) -> TelegramWidgetUser \| None` | `verify_telegram_widget` + single use (the signature is stored hashed as a consumed `widget` token): use this in the route | — |
| `purge_expired_tokens(s, older_than=1 day) -> int` | housekeeping | — |

`TelegramWidgetUser(telegram_id, username, first_name)`.

### profiles
| Function | Semantics | Raises |
|---|---|---|
| `create_profile(s, user, data: ProfileCreate) -> Profile` | number from the sequence; pending (+`profile.submitted`) or active if `PREMODERATION=false`; flags stop words | `UserBanned`, `ConsentRequired`, `ProfileAlreadyExists` |
| `update_profile(s, user, data: ProfileUpdate) -> Profile` | applies changed fields. **Re-moderation only when a free-text field changes** (`PROFILE_FREE_TEXT_FIELDS`): active/pending → resubmitted (pending + event; active if no premoderation), hidden stays hidden but loses approval. Choice/number edits (e.g. completing a migrated profile) keep status and approval, no event. A rejected profile is resubmitted by any edit. With premoderation at most `SUBMISSIONS_PER_HOUR` (5) resubmissions an hour | `UserBanned`, `ConsentRequired`, `ProfileRequired`, `ProfileSubmissionLimitReached` |
| `hide(s, user)` / `show(s, user) -> Profile` | active/pending → hidden (no consent needed); hidden → active (or pending + event if content unapproved) | `ProfileRequired`, `ProfileStateError`, (`show`: `UserBanned`, `ConsentRequired`, `ProfileSubmissionLimitReached`) |
| `approve(s, user_id) -> Profile` | pending → active, emits `profile.moderated` | `ProfileNotFound`, `ProfileStateError` |
| `reject(s, user_id, reason) -> Profile` | any non-rejected → rejected with note, emits `profile.moderated` | `InvalidInput`, `ProfileNotFound`, `ProfileStateError` |
| `get_profile(s, user_id)` / `get_profile_by_user(s, user)` / `get_profile_by_number(s, number) -> Profile \| None` | no visibility checks; `user` loaded | — |
| `get_viewable_profile(s, viewer, user_id) -> Profile` | own profile; a catalog-visible profile **if the viewer may browse** (else the gate error instead of `ProfileNotFound`); or a counterpart of a pending/accepted interest or any conversation (also when hidden, whatever the viewer's status) — but only approved content: an edited (pending) or rejected counterpart raises `ProfileUnderReview` (a `ProfileNotFound` with its own message); `user` loaded | `ProfileRequired`, `UserBanned`, `ConsentRequired`, `ProfilePending`/`ProfileRejected`/`ProfileHidden`, `ProfileNotFound`, `ProfileUnderReview` |
| `get_viewable_profile_by_number(s, viewer, number) -> Profile` | the same for `/p/{number}` and `p_<number>`; an unknown number behaves like an invisible profile | same |
| `missing_fields(profile) -> list[str]` / `is_complete(profile) -> bool` | empty required fields (migrated: `marital_status, children, education, employment`); incomplete profiles stay in the catalog (show «не указано»), owners are offered «Дополнить анкету» | — |
| `require_active(profile \| None) -> Profile` | the gate as a check (sync) | `ProfileRequired`, `ProfilePending`, `ProfileRejected`, `ProfileHidden` |
| `advance_number_sequence(s, floor=0) -> int` | importer: after inserting profiles with explicit `number`s, move the sequence past max(number, floor), never back; returns the next number | — |
| `approved_content()` / `has_approved_content(profile)` | SQL / Python check: content a moderator approved (or published without premoderation), not rejected — the only content shown to other people | — |
| `list_catalog(s, viewer, filters=None, page=1, per_page=20) -> (list[Profile], total)` | gated (see above); ACTIVE, opposite gender, not banned, not the viewer, no report between them in either direction; newest approved first; per_page ≤ 50; `user` loaded. Filters on marital status/children/education never match NULL (incomplete) values | `ProfileRequired`, `UserBanned`, `ConsentRequired`, `ProfilePending`/`ProfileRejected`/`ProfileHidden` |
| `catalog_position(s, viewer, filters, user_id) -> int \| None` | 1-based position in `list_catalog` order, None if no longer in the catalog (bot «Дальше» continues after the shown card) | as `list_catalog` |
| `list_pending(s, *, limit, offset) -> list[Profile]` | moderation queue, oldest first | — |
| `public_counts(s) -> {Gender: int}` | published profiles for the landing page | — |

With PREMODERATION on, pending/rejected/hidden profiles neither browse nor appear in the catalog.

### interests
| Function | Semantics | Raises |
|---|---|---|
| `send_interest(s, from_user, to_user_id, message=None) -> Interest` | see rules below; emits `interest.created` | `UserBanned`, `ConsentRequired`, `InterestToSelf`, `InvalidInput`, `ProfileRequired`, `ProfilePending`/`ProfileRejected`/`ProfileHidden` (all `ProfileNotActive`), `ProfileNotFound`, `InterestAlreadyExists`, `InterestBlockedByWitness`, `InterestDeclinedBefore`, `InterestSentBefore`, `InterestLimitReached` |
| `accept(s, interest_id, user) -> Conversation` | recipient only; locks both users rows (ascending id) first; the sender's profile must have approved content (a profile taken down by a moderator gains no chat); refused while the pair has an open conversation or **either person has `MAX_OPEN_CONVERSATIONS` open conversations** («У одного из участников уже есть открытый чат. Завершите его сначала.»); creates the conversation; emits `interest.accepted` + `witness.conversation_opened` | `UserBanned`, `ConsentRequired`, `InterestNotFound`, `InterestNotPending`, `ProfileNotFound`, `InterestAlreadyExists`, `OpenConversationLimit` |
| `decline(s, interest_id, user) -> Interest` | recipient only; emits `interest.declined` | `UserBanned`, `ConsentRequired`, `InterestNotFound`, `InterestNotPending` |
| `cancel(s, interest_id, user) -> Interest` | sender only, pending only; no event; no consent needed; the sender may send this person a new interest later | `InterestNotFound`, `InterestNotPending` |
| `get_interest(s, interest_id, user) -> Interest` | sender or recipient; both users + profiles loaded | `InterestNotFound` |
| `get_relation(s, viewer, other_user_id) -> PairRelation` | `.interest` (latest, either direction), `.conversation` (the open one, else the latest closed) — for buttons on the profile page | — |
| `list_incoming(s, user, statuses=(PENDING,)) -> list[Interest]` | newest first, senders who are banned or whose profile content is not approved are hidden, `from_user.profile` loaded | — |
| `list_outgoing(s, user, limit=100) -> list[Interest]` | all statuses, `to_user.profile` loaded | — |
| `pending_incoming_count(s, user) -> int` | badge | — |

Rules of `send_interest` (each direction of a pair has its own history, like the legacy
`UNIQUE(from, to)`): sender not banned, consented, ACTIVE profile; recipient profile ACTIVE,
opposite gender, owner not banned, no report between them (else `ProfileNotFound`); no **pending**
interest in either direction and no **open** conversation for the pair (`InterestAlreadyExists`);
no conversation of the pair ever closed by the witness (`InterestBlockedByWitness`); the sender's
earlier interest to this person was not declined (`InterestDeclinedBefore`) or accepted
(`InterestSentBefore`, «Вы уже отправляли отклик этой анкете. Повторный отклик невозможен.») — only a
cancelled one may be sent again; the reverse direction is independent (after a closed chat the
other person may send an interest and a new chat may open); at most `INTEREST_DAILY_LIMIT`
interests created by the sender in the last 24 hours (cancelled ones count); message ≤500 chars
without contacts; stop words flag the interest. Both users rows are locked first, so concurrent
opposite sends cannot both succeed.

### chat
| Function | Semantics | Raises |
|---|---|---|
| `list_conversations(s, user) -> list[ConversationSummary]` | most recent activity first | — |
| `get_conversation_for_participant(s, conversation_id, user) -> Conversation` | `user_a`/`user_b` + profiles loaded | `ConversationNotFound` |
| `open_conversations(s, user) -> list[Conversation]` | open ones, most recent activity first, participants + profiles loaded; the bot sends plain text to the only one when there is exactly one | — |
| `count_open_conversations(s, user_ids) -> {user_id: int}` | 0 included | — |
| `send_message(s, conversation_id, sender, text, origin) -> Message` | participant, conversation open, partner exists, sender not banned and consented, at most `CHAT_RATE_LIMIT_PER_MIN` per sender (both channels); **contacts hidden** (`hide_contacts`) → `flagged`, `flag_reason` starts with `contact-leak filter`, `message.contacts_hidden` is True: show `chat.CONTACTS_HIDDEN_NOTICE` («Часть текста была скрыта фильтром (телефоны/ссылки/@). Свидетель уведомлён.») to the sender; adjacent contacts become one placeholder and the stored text stays ≤2000 characters (else `InvalidInput`); stop words only flag; emits `message.created` + `witness.copy` | `UserBanned`, `ConsentRequired`, `ConversationNotFound`, `PartnerDeleted`, `ConversationClosed`, `InvalidInput`, `MessageRateLimited` |
| `list_messages(s, conversation, *, after_id=None, before_id=None, limit=50) -> list[Message]` | ascending; `after_id` = next page after it (resync), else the latest `limit` (before `before_id`) | — |
| `mark_read(s, conversation, reader) -> int` | marks the partner's messages read | — |
| `close_conversation(s, conversation, user)` | «Завершить общение» (no consent needed); emits `conversation.closed` + `witness.conversation_closed` | `ConversationNotFound`, `ConversationClosed` |
| `unread_total(s, user) -> int` | badge | — |
| `set_bot_active_conversation(s, user, conversation_id \| None)` | bot «Ответить» / `/stop` | `ConversationNotFound`, `PartnerDeleted`, `ConversationClosed` |
| `get_conversation_between(s, a, b)` (open, else latest closed), `get_open_conversation_between(s, a, b)`, `create_conversation(...)`, `force_close(...)`, `flag_message(message, reason)` | internal helpers for other services | — |

`ConversationSummary(conversation, partner: User | None (profile loaded; None = deleted),
last_message, unread_count, partner_has_guardian)` + property `partner_profile`. Closing a
conversation clears `bot_active_conversation_id` of users who had it active. Show
"собеседник удалил аккаунт" when `conversation.partner_id(me) is None`. `list_messages` returns
witness messages too (`message.is_witness`: render apart, signed «Свидетель»; on the site the chat
header says «Переписку видит свидетель (махрам/администратор)»).

### witness (`app.services.witness`)
Each function checks `users.is_admin(actor)` itself (`WitnessOnly`, «Только для свидетеля.»);
the bot resolves `actor` from the Telegram user who pressed the button (group or private chat).

| Function | Semantics | Raises |
|---|---|---|
| `witness_flag(s, actor, message_id) -> Message` | «⚠️ Пометить» (`w:flag:<message_id>`): flags a participant message, appends `witness` to `flag_reason`; idempotent; no event | `WitnessOnly`, `MessageNotFound` (unknown or witness message) |
| `witness_say(s, actor, conversation_id, text, origin=TELEGRAM) -> Message` | «✍️ Написать в чат» (`w:say:<conversation_id>`): stores `kind='witness'`, `sender_id` NULL, text 1–2000 (no contact filter); also in a closed conversation; updates `last_message_at`; emits `message.witness` | `WitnessOnly`, `InvalidInput`, `ConversationNotFound` |
| `witness_close(s, actor, conversation_id) -> Conversation` | «🚫 Закрыть чат» (`w:close:<conversation_id>`): `close_reason='closed_by_witness'`, `closed_by_id` = actor; emits `conversation.closed` + `witness.conversation_closed`; the pair can no longer open a chat through interests | `WitnessOnly`, `ConversationNotFound`, `ConversationClosed` («Чат уже закрыт.») |

### meta (`app.services.meta`)
| Function | Semantics | Raises |
|---|---|---|
| `get_value(s, key) -> dict \| None`, `set_value(s, key, value)` | key-value upsert | — |
| `legacy_import_marker(s) -> dict \| None` | `{"imported_at", "counts"}` once imported | — |
| `mark_legacy_import(s, counts: {table: int})` | call in the importer's transaction (counts only); a second import (also a concurrent one) cannot commit | `LegacyImportDone` |

### guardians
| Function | Semantics | Raises |
|---|---|---|
| `create_invite(s, ward) -> str` | deep link `https://t.me/{BOT_USERNAME}?start=wali_{token}`, TTL 7 days; revokes previous pending invites; current guardian stays until a new one accepts | `UserBanned`, `ConsentRequired` |
| `invite_link(token) -> str`, `get_pending_invite(s, ward) -> Guardian \| None` | re-show the link in settings | — |
| `get_invite(s, token) -> Guardian \| None` | valid pending invite, `ward` + `ward.profile` loaded (bot confirmation screen) | — |
| `accept_invite(s, token, guardian_user) -> Guardian` | after explicit confirmation; replaces the previous active guardian; emits `guardian.linked` (also to the ward's open-chat partners) | `GuardianInviteInvalid`, `GuardianSelf`, `UserBanned` |
| `revoke(s, ward) -> bool` | disconnect active guardian + cancel pending invites (no event) | — |
| `get_active_guardian(s, ward_user_id) -> Guardian \| None` | `guardian` (User) loaded | — |
| `has_active_guardian(s, user_id) -> bool`, `users_with_active_guardian(s, ids) -> set[int]` | badge «👁 Вали подключён — переписка видна вали» | — |
| `active_guardians_for(s, ids) -> {ward_id: guardian_user_id}` | batch | — |
| `list_wards(s, guardian_user) -> list[Guardian]` | active links, `ward` + `ward.profile` loaded | — |

### moderation
| Function | Semantics | Raises |
|---|---|---|
| `create_report(s, reporter, target_user_id, reason: ReportReason, comment=None, message_id=None) -> Report` | **message report** («Пожаловаться» under a chat message, legacy `rep:<message_id>`): the message must be the target's participant message in a conversation of the reporter (open or closed); once per message; no other limit, no consent needed — a participant can always report; the message is flagged (`user report`). **Other reports**: consent; the reporter must have met the target (an interest or a conversation between them, or the target's profile is in the reporter's catalog); one open report per pair; at most `REPORT_DAILY_LIMIT` (5) such reports in 24 h. Emits `report.created` + `witness.report` | `UserBanned`, `ReportToSelf`, `InvalidInput`, `UserNotFound`, `MessageNotFound`, `ConsentRequired`, `AlreadyReported`, `ReportLimitReached` |
| `get_report(s, report_id)`, `list_reports(s, status=OPEN, *, limit, offset)` | reporter/target (+profiles) and message loaded | — |
| `resolve_report(s, report_id, admin, action: ReportAction) -> Report` | `dismiss`; `hide_profile` = `profiles.reject` with a standard note; `ban_user` = `users.ban` + resolve all open reports on the target | `PermissionDenied`, `ReportNotFound`, `ReportAlreadyResolved`, `AdminBanDenied` |
| `list_flagged_messages(s, *, limit, offset) -> list[Message]` | flagged messages (stop words, contact filter, witness, reports), newest first, `sender.profile` loaded | — |
| `find_stop_words(text) -> list[str]` | scam markers (money transfers, cards, card-like numbers, crypto, investments, bets, loans, prepayment, webcam; Russian + translit) | — |

Profiles and interests have `flagged`/`flag_reason` too (filter `Profile.flagged` in the admin).
Flagged content is never blocked.

### events
| Function | Semantics |
|---|---|
| `emit(s, kind: EventKind, payload, notify_user_ids=()) -> Outbox` | outbox row + `pg_notify('events', json)` in the same transaction (delivered on commit only) |
| `fetch_pending(s, limit=50) -> list[Outbox]` | due undelivered rows, `FOR UPDATE SKIP LOCKED` (several workers are safe); process them in the same transaction |
| `mark_sent(s, row)` / `mark_failed(s, row, error, *, max_attempts=8)` | retry backoff 10 s·2ⁿ (≤1 h), then `failed_at` (for failures of one row only, see the worker pattern) |
| `count_recent(s, kind, within, **payload) -> int` | events of `kind` whose payload contains `payload` in the last `within` (rate limits) |
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
| `message.created` | `conversation_id, message_id, sender_id, recipient_id, origin, guardians: [{ward_user_id, guardian_user_id}]` | sender, recipient | participant messages only. Recipient (not if the sender was banned meanwhile): «<b>Анкета №N пишет:</b>\nтекст» (legacy wording) with «Пожаловаться» (`rep:<message_id>`) and «Ответить» when the recipient has several open chats, plus the badge «👁 Вали подключён» when the sender has a wali; each guardian: copy marked whose conversation it is (re-check `get_active_guardian(ward)` before sending) |
| `message.witness` | `conversation_id, message_id, recipient_ids: [participants], guardians: [{ward_user_id, guardian_user_id}]` | the participants | each participant in `recipient_ids` (still existing, not banned): «<b>Сообщение от свидетеля:</b>\nтекст»; each guardian of a participant: a copy marked whose conversation it is. On the site it appears in the chat (SSE: re-fetch `after_id`) |
| `interest.created` | `interest_id, from_user_id, to_user_id` | both | `to_user`: «<b>Вами заинтересовалась анкета №N</b>» + card + message + «Принять» / «Отклонить» (`int:acc:<id>` / `int:dec:<id>`, the legacy format) (skipped if the sender's content is no longer approved) |
| `interest.accepted` | `interest_id, conversation_id, from_user_id, to_user_id` | both | `from_user`: the legacy «<b>Чат открыт между анкетами №A и №B.</b>» with «Завершить общение» (wali badge if the acceptor has one); the acceptor gets the same text from the accept handler |
| `interest.declined` | `interest_id, from_user_id, to_user_id` | both | `from_user` (gentle wording) |
| `conversation.closed` | `conversation_id, closed_by_id (null for ban/deletion; the admin for the witness), reason` | both participants | the other participant(s), text by `reason`; for `closed_by_witness` **both**: «Свидетель завершил ваш чат. Если есть вопросы — обратитесь к админу.» |
| `profile.submitted` | `user_id` | — | admins (`users.admin_telegram_ids`): card + «Одобрить» / «Отклонить»; skip if no longer pending |
| `profile.moderated` | `user_id, status` (`active`/`rejected`) | owner | owner; on rejection include `profile.moderation_note` |
| `report.created` | `report_id, reporter_id, target_user_id, message_id` | — | admins, with action buttons (not if the reporter was banned meanwhile) |
| `witness.copy` | `conversation_id, message_id, sender_id, recipient_id, origin` | — | **the witness**: exactly one row per participant message from either channel. «Чат #id \| №A → №B (сайт/Telegram)\nтекст» (stored text, i.e. after the contact filter; numbers via `profile_title`) with «⚠️ Пометить» `w:flag:<message_id>`, «✍️ Написать в чат» `w:say:<conversation_id>`, «🚫 Закрыть чат» `w:close:<conversation_id>`; skip if the message no longer exists |
| `witness.conversation_opened` | `conversation_id, interest_id, user_a_id, user_b_id` | — | the witness: «Открыт чат #id: №A ↔ №B.» |
| `witness.conversation_closed` | `conversation_id, closed_by_id, reason` (as `conversation.closed`) | — | the witness: «Чат #id закрыт: <label(reason)> (№A ↔ №B).» (a deleted side shows as «аккаунт удалён») |
| `witness.report` | `report_id, message_id` | — | **`WITNESS_CHAT_ID` only** (admins already get `report.created`); mark sent when it is unset: «⚠️ Жалоба на сообщение … » with the message text if any |
| `guardian.linked` | `guardian_id, ward_user_id, guardian_user_id` | ward, guardian, partners of the ward's open conversations | ward: «Вали подключён» (with the wali's Telegram name: they know each other); each partner: «К переписке с <b>Анкета №N</b> подключён вали…» (an open chat page shows the notice at once: SSE `wali`) |

Telegram delivery honours `users.notify_telegram` and skips banned/deleted recipients. Guardian
copies are the purpose of the wali link and are sent regardless of the guardian's
`notify_telegram`. If Telegram answers "bot was blocked by the user", mark the row sent (no retry).

**The witness** = `WITNESS_CHAT_ID` when set (one message to the group), otherwise every id of
`users.admin_telegram_ids(s)` privately. `witness.*` rows are separate from the participant rows on
purpose: a failing or blocked witness chat never re-sends anything to participants, and one row per
user message means exactly one copy. Witness notices ignore `notify_telegram`. The buttons under a
copy work only for admins (`services.witness` checks). Witness and participant texts show profile
numbers only (never Telegram names or ids).

Outbox worker pattern (bot): wake on every `EventListener` event and at least every 5 seconds:

```python
async with session_scope() as session:
    for row in await events.fetch_pending(session, limit=20):
        try:
            await deliver(row)  # loads data by IDs, sends via aiogram Bot
        except TELEGRAM_UNAVAILABLE:  # network, 5xx, persistent flood control
            break  # row untouched; the worker pauses 5 s … 5 min
        except Exception as exc:  # a failure of this row only
            await events.mark_failed(session, row, type(exc).__name__)
        else:
            await events.mark_sent(session, row)
```

An outage never counts attempts (it would give up wali copies after ~21 minutes) and stopping the
batch keeps the order of messages. Logs carry no message texts or SQL parameters (`exc_summary`).

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
  `AccountDep` (logged in, not banned, consent not checked — the wizard and `/welcome`);
  `UserDep` (logged in, not banned → else redirect to `/login?next=…` / 403; **and consented** →
  else redirect to `/welcome?next=…`, or `/profile/new` for people without a profile, whose wizard
  asks the same consent); `ProfileUserDep` (also has a profile → else redirect `/profile/new`);
  `AdminDep` (403 unless `users.is_admin`; no consent check: admins work as the operator).
  `ConsentRequired` escaping a route is answered with the same redirect (`main._consent_required`).
- Gates (`app.web.gates.profile_gate`): `ProfileNotActive` from `list_catalog` /
  `get_viewable_profile_by_number` renders `profile/gate.html` (200: pending / rejected with the
  moderator's note / hidden with «Показать анкету»); HTMX requests get `HX-Redirect` to the page.
- Sessions: `login_user(request, user)` (clears the session first: no fixation; rotates CSRF;
  stores `users.session_version`), `logout_user(request)` (this browser);
  `current_user` ends a session whose version no longer matches. `POST /logout` also calls
  `users.end_web_sessions`: logging out ends the session on every device (copied cookies too).
  `safe_next_url(next)` against open redirects.
- Ids in URLs use `IdPath` (1 … 2⁶³−1), profile numbers `NumberPath` (1 … 2³¹−1): larger numbers
  are a 400 page, not a database error.
- Site map additions (legacy integration): `/p/{number}` (+ `/interest`, `/report` POSTs),
  `/me/complete` (only `profiles.missing_fields`, saved with `ProfileUpdate`; choices keep the
  profile published), `/welcome` (consent for migrated accounts), `/chats/{id}/messages/{mid}/report`
  (GET form + POST: message report). Branding: `static/img/logo.png` (+`@2x`), `favicon-32.png`,
  `apple-touch-icon.png`; palette tokens on `:root` in `static/css/app.css` (light + dark).
- Unhandled errors: `UnhandledErrorMiddleware` answers the styled 500 page and logs only
  `exc_summary` (the engine also has `hide_parameters=True`). `/auth/*` query strings and magic
  tokens are removed from uvicorn's access log (`security.AccessLogRedaction`).
- CSRF: every POST/PUT/PATCH/DELETE needs the session token in form field `csrf_token` or header
  `X-CSRF-Token`; templates: `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">`,
  HTMX: `hx-headers='{"X-CSRF-Token": "{{ csrf_token() }}"}'` on `<body>`.
- Templates: `from app.web.templating import templates`; globals `csrf_token()`, `label()`,
  `choices()`, `settings`, `age()`, `plural()`; filters `age`, `plural`, `msk`, `msk_time`, `msk_date`
  (Moscow time). `base.html` has blocks `title`, `head`, `content`.
- Login flows: `/login` creates `auth.create_login_token`, keeps the raw token in the session,
  shows `auth.login_deep_link(token)` and polls (`poll_login_token` / `login_token_state`).
  Magic links: the bot should send them as an inline URL button (no link preview that could consume
  the single-use token). Widget: pass only Telegram's query fields to `auth.consume_widget_login`
  (single use, 5 minutes), then `users.get_or_create_by_telegram`.
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
  is_banned=False, notify_telegram=True)` (`consent=False` = a migrated user),
  `make_profile(user=None, gender=MALE, status=ACTIVE, **fields)` (numbers 500, 501, … restart in
  every test; `number=512` for an explicit one; `marital_status=None, …` for an incomplete migrated
  profile), `make_pair() -> (man, woman)` with active profiles (№500, №501),
  `make_conversation(sender, recipient)` (one open chat per person by default:
  `override_settings(MAX_OPEN_CONVERSATIONS=2)` for more); `notifications` (asyncio.Queue of decoded NOTIFY payloads); `web_client`
  (httpx.AsyncClient on `create_app()`; do not use Starlette's TestClient — it runs another event
  loop and breaks the asyncpg pool); `login_client(client, user) -> csrf_token`;
  helper `tests.conftest.session_cookie(data)`.
- Each test directory needs an `__init__.py` (test packages: `tests/services`, `tests/web`,
  `tests/bot`, `tests/admin`, `tests/e2e`, and the new `tests/import`). Mock the aiogram `Bot`;
  never call api.telegram.org.
- `tests/services/test_migrations.py` runs alembic (subprocess) against a scratch database
  `<test db>_migrations` (created and dropped; the role needs CREATEDB): 0002 data → head →
  `alembic check` → downgrade → head. New revisions must keep it green (one head).
- Every participant message now also emits a `witness.copy` row (and accept/close emit
  `witness.*` rows): count outbox rows by kind, not in total.
- `tests/e2e` (skipped unless `E2E=1`): **empties** `E2E_DATABASE_URL` (default `nikah_e2e_test`; the
  name must contain `test`), migrates it, imports a synthetic legacy `data.db`
  (`python -m tests.e2e.prepare`, rows built with `tests/import/conftest.LegacyDb`) with
  `scripts/import_legacy.py --commit`, then runs a real uvicorn on port 8000 with `WITNESS_CHAT_ID`
  set, Playwright for Node (`cross_channel.js`) and the bot's real handlers/outbox worker with a
  recording Bot API (`python -m tests.e2e.telegram_side relay|witness-say|wali-accept|drain`). It
  proves site ↔ bot sync, witness copies from both channels, the witness writing into a live site
  chat, the contact filter in every channel, the one-open-chat rule, and that a migrated user meets
  the consent gate, keeps the old number and chat history and is offered «Дополнить анкету».

## 11. File ownership (legacy integration phase)

| Owner | Files |
|---|---|
| core (done first) | `app/config.py`, `app/db.py`, `app/clock.py`, `app/enums.py`, `app/models.py`, `app/schemas.py`, `app/text.py`, `app/services/**`, `migrations/**`, `alembic.ini`, `requirements.txt`, `pyproject.toml`, `tests/conftest.py`, `tests/services/**`, `docs/ARCHITECTURE.md` |
| web agent | `app/web/**` except `app/web/routes/admin.py`, `app/web/templates/admin/**`, `app/web/static/css/admin.css`; `tests/web/**` |
| bot agent | `app/bot/**`, `tests/bot/**` |
| ops agent | `scripts/**`, `tests/import/**`, `app/web/routes/admin.py`, `app/web/templates/admin/**`, `app/web/static/css/admin.css`, `tests/admin/**`, `Dockerfile`, `docker-compose*.yml`, `Caddyfile`, `deploy/**`, `.env.example`, `docs/DEPLOY.md`, `docs/CUTOVER.md`, `docs/LEGACY_IMPORT.md` |

Shared core may only be **extended** by the build agents (append new functions, fields, fixtures,
dependencies) — never changed incompatibly. A new column needs a new Alembic revision after 0003
(coordinate: one head; `tests/services/test_migrations.py` must stay green). `tests/e2e/**` belongs
to whoever changes a flow it drives (it is skipped without `E2E=1`: run it before handing over).

### Contracts per agent

- **web**: `/p/{number}` via `profiles.get_viewable_profile_by_number`; catch `ConsentRequired` →
  consent page (then `users.set_consent`), `ProfileNotActive` subclasses → friendly page;
  «Дополнить анкету» from `profiles.missing_fields` (choices/numbers only: no re-moderation);
  show `chat.CONTACTS_HIDDEN_NOTICE` when `message.contacts_hidden`; render `message.is_witness`
  messages apart («Свидетель») and the chat header «Переписку видит свидетель (махрам/администратор)»;
  SSE: `message.witness` like `message.created`; report buttons under messages →
  `create_report(..., message_id=…)`.
- **bot**: legacy menu/commands/callbacks (LEGACY_BOT.md §3); age question → `AgeIn`;
  plain text → `chat.open_conversations(user)` when exactly one (else the active chat);
  `send_message` + `CONTACTS_HIDDEN_NOTICE`; witness buttons → `services.witness` (also from the
  `WITNESS_CHAT_ID` group); deliver the `witness.*` / `message.witness` kinds (§8); admin
  `/ban`/`/unban` → `users.resolve_user_ref`; `TELEGRAM_PROXY` → `AiohttpSession(proxy=…)`; start
  polling without `drop_pending_updates`.
- **ops / import**: profiles with explicit `number = legacy id`, then
  `profiles.advance_number_sequence(s, floor=sqlite_sequence.profiles)`; `setval` for ids of
  `interests`, `conversations`, `messages`; users with `consent_at = NULL`; NULL for the four new
  required fields; texts as they are; `messages.kind='user'`; map legacy `witness_note` to codes
  only (`contact-leak filter` stays, `user_report` → `user report`, `flagged by <id>` → `witness`:
  no Telegram ids in `flag_reason`); `meta.mark_legacy_import(s, counts)` in the same transaction,
  refuse when `meta.legacy_import_marker` exists. No events are emitted by the import.

## 12. Decisions where the spec was silent

1. ~~Public id = `users.id`~~ (LEGACY_BOT.md §2.1): the public id is the profile number; profile PK
   = `user_id`, internal only.
2. Gender cannot be changed after the profile is created (delete the account to start over).
3. Contacts are rejected in public profile texts and interest messages; in chats they are hidden
   by the contact filter (LEGACY_BOT.md §2.9) and the witness is told (flagged message).
4. A decline is final for that sender → recipient direction; the recipient may still write first.
5. ~~One conversation per pair, ever~~ (superseded by the legacy bot's rules, decision 17): each
   direction of a pair has its own history; an accepted interest is also final for its sender, so
   after a chat closes only the other person can start a new one.
6. Any report (even dismissed) hides the two users from each other's catalog and blocks interests.
7. Ban cancels pending interests, closes open chats and revokes wali links; unban reopens nothing.
8. Account deletion is a hard delete; the partner keeps their own messages in a closed chat.
   No record is kept to stop a banned person from re-registering with the same Telegram account
   (question for the lawyer / product owner).
9. Login tokens are stored hashed; banned users cannot log in to the site.
10. Wali invites expire after 7 days; a wali cannot be one's own wali.
11. ~~Editing a profile always goes back to moderation~~ (LEGACY_BOT.md §2.4): only a change of
    free text (city, nationality, occupation, about, wishes) goes back to moderation (also
    re-submitting a pending one); choice and number edits keep the status; any edit of a rejected
    profile re-submits it.
12. Profiles with stop words are flagged (not blocked), like messages and interests; an admin can
    take down a published one from `/admin/flagged` or `/admin/users/{id}`.
13. Other people only ever see approved profile content: an edited (pending) or rejected profile is
    hidden from interest and chat counterparts too, and its pending interests cannot be accepted
    until it is approved again.
14. Flood limits on actions that reach the admins or the shared delivery queue: 5 reports a day
    (only about people one has met), 5 profile submissions an hour, 20 chat messages a minute.
15. Logging out of the site ends all of the person's site sessions (stateless cookies + version).
16. In the bot, a Telegram «Reply» goes to the chat of the replied message, ending an open dialog;
    a reply to a wali copy, an interest or profile card is refused with a hint (never sent to the
    active chat). Editing a profile in the bot asks which field to change.

Legacy integration (LEGACY_BOT.md is the source; these fill its gaps):

17. Interests mirror the legacy `UNIQUE(from, to)` per direction (accepted/declined are final for
    that sender, cancelled is not); the database enforces only one pending interest per direction,
    services the rest. Accepting the second of two crossed pending interests is refused while the
    chat of the first is open; the other one stays pending.
18. A conversation closed by the witness blocks new interests between the two people in both
    directions (otherwise a moderation action could be undone at once); an admin may still talk to them.
19. With PREMODERATION on, counterparts (pending/accepted interest, any conversation) stay viewable
    while one's own profile is pending/rejected/hidden — gating protects the catalog, not existing
    contacts. An unknown profile number behaves like an invisible one (gate error first).
20. Consent (migrated users): withdrawing actions (cancel an interest, close a chat, hide the
    profile, revoke the wali, delete the account) and reporting a message in one's own chat work
    without consent; everything that creates or shows data needs it, including browsing and
    inviting a wali.
21. Message reports: once per message, not limited otherwise and no consent needed (a participant
    can always report a message in their own chat, also after it was closed); profile/person
    reports keep the wf1 limits (one open per pair, 5 a day — message reports do not count).
22. The witness may write into a closed conversation (to explain a closing); witness messages are
    not filtered, not rate-limited, not anyone's unread, and cannot be flagged or reported.
23. `witness.report` goes to `WITNESS_CHAT_ID` only (admins already get `report.created`); all other
    witness notices fall back to each admin when `WITNESS_CHAT_ID` is unset.
24. Contact filter: any run of 10+ digits joined by separators is hidden (phones, but also card
    numbers or tax ids); dates are excluded first; contacts separated only by spaces/punctuation
    share one placeholder, and a message that would exceed 2000 characters after hiding is refused. Stop words are matched on the filtered text, so
    a hidden number never lands in `flag_reason`.
25. The 0003 downgrade restores the 0002 schema and deletes witness messages; it refuses (changing
    nothing) when the data cannot be represented in 0002 (incomplete migrated profiles, several
    chats or active interests per pair). Profiles get `name = 'Анкета <number>'` on downgrade.
26. Legacy env names: a non-empty new name wins, an empty new value does not hide the legacy one.

Website (legacy integration; web agent):

- **W1.** Consent gate: every page behind `UserDep` (all logged-in pages except legal pages, the
  landing page, `/logout`, `/me/delete`, `/welcome`, the wizard and `/admin`) sends a person without
  consent to `/welcome` (with a profile) or to the wizard (without one). Withdrawing actions the
  services allow without consent stay reachable in the bot; on the site the person consents first
  or deletes the account from `/welcome`.
- **W2.** Gate pages are ordinary 200 pages explaining the state (not error pages); a counterpart's
  profile and `/interests` (answering incoming interests) stay available while one's own profile
  is pending, rejected or hidden (decision 19). An unknown `/p/{number}` and a POST on a profile
  the person may not see both answer 404, so existence of numbers is not probed through forms.
- **W3.** «Дополнить анкету» asks all missing required fields at once and needs all of them (they
  are required for new profiles too); the banner shows on `/` and `/me`, «не указано» everywhere else.
- **W4.** Message reports use a separate page per message (works without JavaScript); witness
  messages and one's own cannot be reported. After a filtered message the sender sees
  `CONTACTS_HIDDEN_NOTICE` next to the composer (or as a notice without JS) and a small mark under
  that own message («часть текста скрыта фильтром»); the partner sees only the placeholder.
- **W5.** Send errors (rate limit, closed chat) are shown next to the composer and keep the typed
  text; a refused accept (`OpenConversationLimit` etc.) is shown on the interest card (409 page
  without JS).
- **W6.** The privacy policy says that copies already delivered to the witness and the wali in
  Telegram stay in their chats after an account is deleted (the platform cannot delete them).
- **W7.** Contrast: text colours are checked for WCAG AA (≥4.5:1) on every background they are used
  on in both themes; `--primary-base` (#558095, the logo's blue) is decorative only, links and
  white-text buttons use the darkened `--primary` (#3f6779, 6.1:1 with white).

Telegram bot (legacy integration; bot agent):

- **B1.** Legacy buttons keep their callback data as the new bot's own format where the meaning is
  the same (`int:acc|dec:<id>`, `rep:<message_id>`, `w:flag|say|close:<id>`), so old and new
  buttons share one handler; `adm:appr|rej:<number>`, `br:*`, `reg:*` and `chat:close` have legacy
  handlers (`handlers.legacy`, `handlers.admin`). Anything unknown answers «Кнопка устарела».
  Buttons of members name people by profile number, never by `users.id` (admin cards keep it).
- **B2.** A legacy `adm:appr` acts only if the profile was not re-submitted after the card's
  message was sent (else the current card is sent instead); `adm:rej` asks for the reason like a
  new card. `br:p|n:<i>` reopen browsing at that position, `br:int:<number>` starts an interest
  (all service checks apply), `chat:close` asks for confirmation before closing the only open chat.
- **B3.** Consent gate in the bot applies to accounts with a profile and no consent (migrated).
  Exempt: /start (with deep links), /help, /rules and their labels, /cancel, /delete and account
  deletion, the consent button, site login and wali confirmation, and the withdrawals services allow
  without consent (hide the profile, cancel an interest, close a chat, report a message). A gated
  menu section, command or chat text is carried out right after «Даю согласие»; a gated button
  must be pressed again. People without a profile consent at the end of the profile form.
- **B4.** Registration keeps the legacy order and wording and inserts the new required fields as
  buttons after nationality; nationality and «О себе» stay skippable; «Готово, остальное позже»
  appears only after the (required) wishes. The bot asks the age everywhere (also when editing).
- **B5.** The card shows the legacy fields and the four new required ones with «не указано» when
  empty; optional extras (height, weight, occupation, madhab, prayer, nikah, relocation) only when
  filled. «Дополнить анкету» saves each button press at once (stateless, survives restarts).
- **B6.** «Мой чат» with one open chat shows the partner's card only if its content is approved
  (as on the site); /stop with exactly one open chat explains that text always goes there. Closing
  a chat asks for confirmation (also from the legacy button).
- **B7.** Witness group: only callbacks `w:*` and the witness's answer after «✍️ Написать в чат»
  are handled there; accounts are created only for ADMIN_TELEGRAM_IDS (never for other members).
  The text prompt is a ForceReply so it reaches the bot in privacy mode. If Telegram refuses the
  witness chat for good (bot removed, wrong id) a copy goes to every admin instead; flood control
  only delays copies (the row stays pending), it never drops one.
- **B8.** `TELEGRAM_PROXY=https://…` is refused at startup with a clear message (aiogram's proxy
  connector supports http CONNECT and socks only; the Telegram traffic inside an http CONNECT tunnel
  is TLS anyway). The message never contains the URL.
- **B9.** Witness messages and «Чат закрыт свидетелем» honour `notify_telegram` like other
  notifications (the site shows them in the chat).

Legacy import, deployment and admin panel (legacy integration; ops agent):

- **O1.** `scripts/import_legacy.py --import` follows LEGACY_BOT.md §4 and reads only a copy of
  `data.db` (`mode=ro`, `query_only`). An age that gives a birth year outside 18–80 (only a manual
  edit of the legacy DB can produce it) is clamped into the range, the profile is flagged
  `moderation.LEGACY_AGE_FLAG_REASON` (`legacy age`) and a published one goes back to `pending`
  (`approved_at` NULL), so a possibly underage profile is never shown unchecked.
- **O2.** Rows that reference a missing user/profile/chat (SQLite FKs are off), and messages whose
  sender is not in the chat, are skipped and counted. Unparseable timestamps refuse the whole import
  (the error names table.column and a count only). Texts are imported as they are; only a value longer
  than its VARCHAR column is cut (city 60, nationality 50, username 64). Blank nationality/about → NULL.
- **O3.** Two open legacy chats of one pair (an accept race): the newest stays open, the older is closed
  at the newer one's `opened_at` (`closed_by_user`). A person with open chats with different partners
  keeps them all. `created_from_interest_id`: the pair's accepted interests (either direction) in time
  order, each used once — the earliest unused one created no later than the chat opened; none → NULL.
- **O4.** Users: `is_admin` = legacy `role='admin'` or `ADMIN_TELEGRAM_IDS`; a legacy-banned admin is
  imported unbanned (admins cannot be banned). Banned users' pending interests and open chats are kept
  as in the legacy DB (counted in the report). Existing target users (no profile, e.g. from
  `make_admin.py`) are merged by `telegram_id`: admin right and consent kept, an empty username filled,
  `created_at` = the earlier one.
- **O5.** The importer locks `users, profiles, interests, conversations, messages, app_meta`
  (SHARE ROW EXCLUSIVE, `lock_timeout` 15 s) before checking that the target is empty. Sequences move
  only with `--commit` (`setval` is not transactional, so a dry run leaves them alone; the users id
  sequence may skip values): profile numbers via `profiles.advance_number_sequence`, ids of interests,
  conversations and messages past max(legacy max id incl. skipped rows, `sqlite_sequence`). Database
  errors are reported by type and constraint name only (PostgreSQL DETAIL can hold personal data).
- **O6.** Admin panel: people are shown by profile number («Анкета №512», «аккаунт без анкеты»);
  internal ids appear only in admin URLs. Search: `№N`/`#N` = profile number (as `/ban`), bare digits =
  Telegram id or number, `@name` = username prefix. Flag reasons are labelled (фильтр контактов /
  свидетель / жалоба / возраст при переносе), the rest are stop words. `/admin/chats` and
  `/admin/chats/{id}` show whole histories including witness messages: admins are the witness and
  already receive a copy of every message. The dashboard shows the `legacy_import` marker next to
  today's counts, users without consent and incomplete profiles. Witness actions stay in Telegram.
- **O7.** Deployment variants are compose override files chosen by `COMPOSE_FILE` in `.env`, so every
  documented command stays `docker compose …`: `docker-compose.nginx.yml` (Caddy gets a never-activated
  profile, `web` published on `127.0.0.1:${WEB_HOST_PORT:-8000}`, nginx template
  `deploy/nginx/nikah.conf`), `docker-compose.hostproxy.yml` (bot `network_mode: host`, Postgres
  published on `127.0.0.1:${POSTGRES_HOST_PORT:-5432}` only), `docker-compose.hostgateway.yml` (bot
  `extra_hosts: host.docker.internal:host-gateway`).

Integration (legacy integration; integration pass):

- **I1.** Message lists (site chat and chat list, the bot's chat history) show the year whenever a
  message is not from the current year (`clock.msk_day_time`): history moved from the legacy bot is
  years old, and «01.04 13:05» alone would read as this spring.
- **I2.** The e2e run uses its own database and empties it, since the legacy import refuses a
  non-empty target. Its accounts and the synthetic legacy rows stay afterwards for inspection.
