-- Схема базы действующего бота @nikah_istina_bot (SQLite), только DDL, без данных.
-- Используется тестами переноса (scripts/import_legacy.py) для синтетической базы.

CREATE TABLE users (
    user_id     INTEGER PRIMARY KEY,
    tg_username TEXT,
    role        TEXT NOT NULL DEFAULT 'user',
    banned      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL UNIQUE,
    gender          TEXT NOT NULL CHECK (gender IN ('m','f')),
    age             INTEGER NOT NULL,
    city            TEXT NOT NULL,
    nationality     TEXT NOT NULL,
    about           TEXT NOT NULL,
    partner_wishes  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected','paused')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
CREATE TABLE interests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    from_profile_id  INTEGER NOT NULL,
    to_profile_id    INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','accepted','declined')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (from_profile_id, to_profile_id),
    FOREIGN KEY (from_profile_id) REFERENCES profiles(id),
    FOREIGN KEY (to_profile_id)   REFERENCES profiles(id)
);
CREATE TABLE chats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_a_id    INTEGER NOT NULL,
    profile_b_id    INTEGER NOT NULL,
    opened_at       TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at       TEXT,
    FOREIGN KEY (profile_a_id) REFERENCES profiles(id),
    FOREIGN KEY (profile_b_id) REFERENCES profiles(id)
);
CREATE TABLE messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id           INTEGER NOT NULL,
    from_profile_id   INTEGER NOT NULL,
    tg_message_id_src INTEGER,
    tg_message_id_dst INTEGER,
    text              TEXT NOT NULL,
    sent_at           TEXT NOT NULL DEFAULT (datetime('now')),
    flagged           INTEGER NOT NULL DEFAULT 0,
    witness_note      TEXT,
    FOREIGN KEY (chat_id) REFERENCES chats(id)
);
CREATE TABLE muted (
    user_id     INTEGER PRIMARY KEY,
    until       TEXT NOT NULL,
    reason      TEXT
);
CREATE INDEX idx_profiles_gender_status
    ON profiles(gender, status);
CREATE INDEX idx_chats_open
    ON chats(profile_a_id, profile_b_id, closed_at);
CREATE INDEX idx_messages_chat_time
    ON messages(chat_id, sent_at);
