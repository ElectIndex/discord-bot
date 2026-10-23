"""SQLite state.

seen   (poll_id, race ident) already turned into a card, or skipped.
       Per race, so a Governor question the NYT adds later to a poll whose
       Senate card already went out gets its own card.
cards  one row per Discord card message: the text that will be tweeted and
       where it is in its life (pending -> posting -> posted / rejected).
meta   key/value (initialized_at).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    poll_id TEXT NOT NULL, ident TEXT NOT NULL, reason TEXT, at TEXT NOT NULL,
    PRIMARY KEY (poll_id, ident));
CREATE TABLE IF NOT EXISTS cards (
    message_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
    text TEXT NOT NULL, source TEXT NOT NULL, poll_id TEXT, ident TEXT,
    status TEXT NOT NULL DEFAULT 'pending', tweet_ids TEXT, decided_by INTEGER,
    error TEXT, created_at TEXT NOT NULL, decided_at TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
# Columns added after the first deploy; ALTERed onto an existing database.
CARD_COLUMNS = {
    "card_json": "TEXT",                          # the Card, so edits can redraw the image
    "image": "BLOB",                              # PNG attached to the tweet
    "image_on": "INTEGER NOT NULL DEFAULT 1",     # staff can turn the image off
    "image_custom": "INTEGER NOT NULL DEFAULT 0", # staff replaced it: edits don't redraw
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(cards)")}
        for col, decl in CARD_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE cards ADD COLUMN {col} {decl}")
        self.db.commit()

    # ---- meta
    def get_meta(self, key: str) -> str | None:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()

    # ---- seen
    def is_seen(self, poll_id: str, ident: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE poll_id=? AND ident=?",
                               (poll_id, ident)).fetchone() is not None

    def mark_seen(self, rows, reason: str = "") -> None:
        """rows: iterable of (poll_id, ident)."""
        at = now_iso()
        self.db.executemany("INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?)",
                            [(pid, ident, reason, at) for pid, ident in rows])
        self.db.commit()

    # ---- cards
    def add_card(self, message_id: int, channel_id: int, text: str, source: str,
                 poll_id: str | None = None, ident: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO cards (message_id, channel_id, text, source, poll_id, ident, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, channel_id, text, source, poll_id, ident, now_iso()))
        self.db.commit()

    def card(self, message_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM cards WHERE message_id=?",
                               (message_id,)).fetchone()

    def claim(self, message_id: int, user_id: int, frm: tuple[str, ...], to: str) -> bool:
        """Atomically move a card from one of `frm` to `to`. False if someone
        else got there first (two approvers clicking at once)."""
        cur = self.db.execute(
            f"UPDATE cards SET status=?, decided_by=?, decided_at=? WHERE message_id=?"
            f" AND status IN ({','.join('?' * len(frm))})",
            (to, user_id, now_iso(), message_id, *frm))
        self.db.commit()
        return cur.rowcount == 1

    def finish(self, message_id: int, status: str, tweet_ids=None, error: str | None = None):
        self.db.execute("UPDATE cards SET status=?, tweet_ids=?, error=? WHERE message_id=?",
                        (status, json.dumps(tweet_ids) if tweet_ids is not None else None,
                         error, message_id))
        self.db.commit()

    def posting_ids(self) -> set[int]:
        """Cards mid-tweet: the nightly clear leaves these alone."""
        return {r["message_id"] for r in
                self.db.execute("SELECT message_id FROM cards WHERE status='posting'")}

    def clear_open(self, message_ids) -> int:
        """Cards deleted by the nightly clear before anyone decided: 'cleared'."""
        ids = list(message_ids)
        cur = self.db.executemany(
            "UPDATE cards SET status='cleared', decided_at=? WHERE message_id=? "
            "AND status IN ('pending', 'failed')", [(now_iso(), i) for i in ids])
        self.db.commit()
        return cur.rowcount

    def set_text(self, message_id: int, text: str, card_json: str | None = None) -> None:
        self.db.execute("UPDATE cards SET text=?, card_json=COALESCE(?, card_json) "
                        "WHERE message_id=?", (text, card_json, message_id))
        self.db.commit()

    def set_card_json(self, message_id: int, card_json: str) -> None:
        self.db.execute("UPDATE cards SET card_json=? WHERE message_id=?", (card_json, message_id))
        self.db.commit()

    def set_image(self, message_id: int, image: bytes | None, *, on: bool | None = None,
                  custom: bool | None = None) -> None:
        self.db.execute(
            "UPDATE cards SET image=?, image_on=COALESCE(?, image_on), "
            "image_custom=COALESCE(?, image_custom) WHERE message_id=?",
            (image, None if on is None else int(on), None if custom is None else int(custom),
             message_id))
        self.db.commit()

    def set_image_on(self, message_id: int, on: bool) -> None:
        self.db.execute("UPDATE cards SET image_on=? WHERE message_id=?", (int(on), message_id))
        self.db.commit()
