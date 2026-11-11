"""SQLite-хранилище: люди, уведомления (для правок), цели, заметки, настройки."""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
    user_id  INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    username TEXT,
    added_ts INTEGER NOT NULL
);

-- Каждое уведомление, которое бот прислал владельцу. Нужно, чтобы при правке
-- сообщения найти исходное уведомление и дописать к нему пометку.
CREATE TABLE IF NOT EXISTS msgs (
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    bot_msg_id INTEGER NOT NULL,
    sender_id  INTEGER NOT NULL,
    name       TEXT NOT NULL,
    username   TEXT,
    date_ts    INTEGER NOT NULL,
    orig_text  TEXT NOT NULL,
    cur_text   TEXT NOT NULL,
    edits      TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (chat_id, msg_id)
);

CREATE TABLE IF NOT EXISTS goals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    due_ts     INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    fired      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    created_ts INTEGER NOT NULL
);
"""

KEEP_MESSAGES_DAYS = 30


class Storage:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---------- настройки ----------
    def get_setting(self, key, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    # ---------- люди ----------
    def add_person(self, user_id, name, username):
        self.conn.execute(
            "INSERT OR REPLACE INTO people(user_id,name,username,added_ts) VALUES(?,?,?,?)",
            (user_id, name, username, int(time.time())),
        )
        self.conn.commit()

    def remove_person(self, user_id):
        self.conn.execute("DELETE FROM people WHERE user_id=?", (user_id,))
        self.conn.commit()

    def list_people(self):
        return self.conn.execute("SELECT * FROM people ORDER BY added_ts").fetchall()

    def is_person(self, user_id) -> bool:
        return self.conn.execute("SELECT 1 FROM people WHERE user_id=?", (user_id,)).fetchone() is not None

    # ---------- уведомления о сообщениях ----------
    def save_message(self, *, chat_id, msg_id, bot_msg_id, sender_id, name, username, date_ts, text):
        self.conn.execute(
            "INSERT OR REPLACE INTO msgs(chat_id,msg_id,bot_msg_id,sender_id,name,username,"
            "date_ts,orig_text,cur_text,edits) VALUES(?,?,?,?,?,?,?,?,?,'[]')",
            (chat_id, msg_id, bot_msg_id, sender_id, name, username, date_ts, text, text),
        )
        self.conn.commit()

    def get_message(self, chat_id, msg_id):
        row = self.conn.execute(
            "SELECT * FROM msgs WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["edits"] = json.loads(d["edits"])
        return d

    def update_message(self, chat_id, msg_id, cur_text, edits):
        self.conn.execute(
            "UPDATE msgs SET cur_text=?, edits=? WHERE chat_id=? AND msg_id=?",
            (cur_text, json.dumps(edits, ensure_ascii=False), chat_id, msg_id),
        )
        self.conn.commit()

    def purge_old_messages(self, days=KEEP_MESSAGES_DAYS):
        cutoff = int(time.time()) - days * 86400
        self.conn.execute("DELETE FROM msgs WHERE date_ts < ?", (cutoff,))
        self.conn.commit()

    # ---------- цели ----------
    def add_goal(self, title, due_ts) -> int:
        cur = self.conn.execute(
            "INSERT INTO goals(title,due_ts,created_ts) VALUES(?,?,?)",
            (title, int(due_ts), int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_goals(self):
        """Активные (ещё не сработавшие) цели по времени."""
        return self.conn.execute(
            "SELECT * FROM goals WHERE fired=0 ORDER BY due_ts, id"
        ).fetchall()

    def delete_goal(self, goal_id):
        self.conn.execute("DELETE FROM goals WHERE id=?", (goal_id,))
        self.conn.commit()

    def due_goals(self, now_ts):
        return self.conn.execute(
            "SELECT * FROM goals WHERE fired=0 AND due_ts<=? ORDER BY due_ts, id", (int(now_ts),)
        ).fetchall()

    def mark_goal_fired(self, goal_id):
        self.conn.execute("UPDATE goals SET fired=1 WHERE id=?", (goal_id,))
        self.conn.commit()

    # ---------- заметки ----------
    def add_note(self, text) -> int:
        cur = self.conn.execute(
            "INSERT INTO notes(text,created_ts) VALUES(?,?)", (text, int(time.time()))
        )
        self.conn.commit()
        return cur.lastrowid

    def list_notes(self, limit=30):
        return self.conn.execute(
            "SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def count_notes(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]

    def get_note(self, note_id):
        return self.conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()

    def delete_note(self, note_id):
        self.conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
        self.conn.commit()
