"""Pure starboard rules and storage. Unit-tested in tests/test_starboard.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

STAR = "⭐"
THRESHOLD = 3


def star_count(reactor_ids: list[int], author_id: int, bot_ids: set[int]) -> int:
    """Stars that count: one per person, never the author's own, never a bot's."""
    return len({uid for uid in reactor_ids if uid != author_id and uid not in bot_ids})


def board_action(count: int, posted: bool, threshold: int = THRESHOLD) -> str:
    """'post' | 'update' | 'remove' | 'none'."""
    if count >= threshold:
        return "update" if posted else "post"
    return "remove" if posted else "none"


class StarStore:
    """original message id -> starboard message id."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("CREATE TABLE IF NOT EXISTS stars (original_id INTEGER PRIMARY KEY, board_id INTEGER NOT NULL)")
        self.db.commit()

    def get(self, original_id: int) -> int | None:
        row = self.db.execute("SELECT board_id FROM stars WHERE original_id = ?", (original_id,)).fetchone()
        return row[0] if row else None

    def put(self, original_id: int, board_id: int):
        self.db.execute("INSERT OR REPLACE INTO stars VALUES (?, ?)", (original_id, board_id))
        self.db.commit()

    def drop(self, original_id: int):
        self.db.execute("DELETE FROM stars WHERE original_id = ?", (original_id,))
        self.db.commit()

    def is_board_message(self, message_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM stars WHERE board_id = ?", (message_id,)).fetchone() is not None
