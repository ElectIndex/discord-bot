"""Pure pieces of moderation: duration parsing, the role-hierarchy rule, and the
case store. Unit-tested in tests/test_moderation_core.py."""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

MAX_TIMEOUT = timedelta(days=28)  # Discord's limit
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> timedelta:
    """'10m', '1h30m', '2d', '1w' → timedelta. Bare numbers are minutes."""
    text = text.strip().lower().replace(" ", "")
    if text.isdigit():
        return timedelta(minutes=int(text))
    parts = re.fullmatch(r"(?:\d+[smhdw])+", text)
    if not parts:
        raise ValueError(f"Couldn't read duration '{text}'. Try 10m, 2h, 1d or 1w.")
    seconds = sum(int(n) * _UNITS[u] for n, u in re.findall(r"(\d+)([smhdw])", text))
    if seconds <= 0:
        raise ValueError("Duration must be longer than zero.")
    return timedelta(seconds=seconds)


def human_duration(delta: timedelta) -> str:
    seconds, out = int(delta.total_seconds()), []
    for label, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            out.append(f"{seconds // size}{label}")
            seconds %= size
    return " ".join(out) or "0s"


def hierarchy_problem(
    actor_id: int, actor_top: int, target_id: int, target_top: int, bot_id: int, bot_top: int, owner_id: int
) -> str | None:
    """Why the actor can't moderate the target, or None if they can.
    `*_top` are top-role positions."""
    if target_id == actor_id:
        return "You can't do that to yourself."
    if target_id == bot_id:
        return "I can't do that to myself."
    if target_id == owner_id:
        return "You can't do that to the server owner."
    if actor_id != owner_id and actor_top <= target_top:
        return "They have a role as high as or higher than yours."
    if bot_top <= target_top:
        return "Their highest role is above mine, so I can't act on them."
    return None


@dataclass
class Case:
    id: int
    action: str
    user_id: int
    user_name: str
    moderator_id: int
    reason: str
    duration_seconds: int | None
    created_at: int
    active: bool


class CaseStore:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                duration_seconds INTEGER,
                created_at INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )"""
        )
        self.db.commit()

    def add(self, action: str, user_id: int, user_name: str, moderator_id: int, reason: str,
            duration: timedelta | None = None) -> Case:
        cur = self.db.execute(
            "INSERT INTO cases (action, user_id, user_name, moderator_id, reason, duration_seconds, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (action, user_id, user_name, moderator_id, reason,
             int(duration.total_seconds()) if duration else None, int(time.time())),
        )
        self.db.commit()
        return self.get(cur.lastrowid)

    def get(self, case_id: int) -> Case | None:
        row = self.db.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return Case(*row[:8], bool(row[8])) if row else None

    def for_user(self, user_id: int, action: str | None = None, active_only: bool = False) -> list[Case]:
        sql, args = "SELECT * FROM cases WHERE user_id = ?", [user_id]
        if action:
            sql += " AND action = ?"
            args.append(action)
        if active_only:
            sql += " AND active = 1"
        rows = self.db.execute(sql + " ORDER BY id DESC", args).fetchall()
        return [Case(*r[:8], bool(r[8])) for r in rows]

    def deactivate(self, case_id: int) -> bool:
        cur = self.db.execute("UPDATE cases SET active = 0 WHERE id = ? AND active = 1", (case_id,))
        self.db.commit()
        return cur.rowcount == 1


def split_duration(first: str | None, rest: str) -> tuple[timedelta | None, str]:
    """For `!mute @user [duration] reason`: if the first word isn't a duration,
    it's the start of the reason."""
    if not first:
        return None, rest
    try:
        return parse_duration(first), rest
    except ValueError:
        return None, f"{first} {rest}".strip()


class MuteStore:
    """Who is muted, and until when (None = until unmuted). Survives restarts and
    re-joins, so leaving and rejoining can't shake a mute."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.execute("CREATE TABLE IF NOT EXISTS mutes (user_id INTEGER PRIMARY KEY, until INTEGER)")
        self.db.commit()

    def set(self, user_id: int, until: int | None):
        self.db.execute("INSERT OR REPLACE INTO mutes VALUES (?, ?)", (user_id, until))
        self.db.commit()

    def clear(self, user_id: int) -> bool:
        cur = self.db.execute("DELETE FROM mutes WHERE user_id = ?", (user_id,))
        self.db.commit()
        return cur.rowcount == 1

    def is_muted(self, user_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM mutes WHERE user_id = ?", (user_id,)).fetchone() is not None

    def due(self, now: int) -> list[int]:
        return [r[0] for r in self.db.execute("SELECT user_id FROM mutes WHERE until IS NOT NULL AND until <= ?", (now,))]
