"""SQLite 存储：群消息、抓到的网页、生成过的日报。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id   INTEGER PRIMARY KEY,
    group_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    sender_name  TEXT    NOT NULL DEFAULT '',
    sender_role  TEXT    NOT NULL DEFAULT 'member',
    is_teacher   INTEGER NOT NULL DEFAULT 0,
    ts           INTEGER NOT NULL,           -- unix 秒
    text         TEXT    NOT NULL DEFAULT '',-- 纯文本化后的内容
    raw          TEXT    NOT NULL DEFAULT '[]',
    urls         TEXT    NOT NULL DEFAULT '[]',
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(group_id, ts);

CREATE TABLE IF NOT EXISTS pages (
    url          TEXT PRIMARY KEY,
    final_url    TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    depth        INTEGER NOT NULL DEFAULT 0,
    parent_url   TEXT,
    truncated    INTEGER NOT NULL DEFAULT 0,
    children     TEXT    NOT NULL DEFAULT '[]',
    error        TEXT,
    fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    day        TEXT PRIMARY KEY,             -- YYYY-MM-DD
    payload    TEXT NOT NULL,                -- 模型返回的结构化 JSON
    usage      TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(pages)")}
        if "children" not in cols:      # 兼容早期建的库
            self._conn.execute("ALTER TABLE pages ADD COLUMN children TEXT NOT NULL DEFAULT '[]'")
        self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    # ---------- messages ----------

    def save_message(self, m: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO messages
                   (message_id, group_id, user_id, sender_name, sender_role,
                    is_teacher, ts, text, raw, urls, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    m["message_id"], m["group_id"], m["user_id"],
                    m.get("sender_name", ""), m.get("sender_role", "member"),
                    int(bool(m.get("is_teacher"))), m["ts"], m.get("text", ""),
                    json.dumps(m.get("raw", []), ensure_ascii=False),
                    json.dumps(m.get("urls", []), ensure_ascii=False),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    def messages_between(
        self, group_ids: list[int], start_ts: int, end_ts: int, teachers_only: bool
    ) -> list[dict[str, Any]]:
        ph = ",".join("?" * len(group_ids))
        sql = (
            f"SELECT * FROM messages WHERE group_id IN ({ph}) AND ts >= ? AND ts < ?"
        )
        args: list[Any] = [*group_ids, start_ts, end_ts]
        if teachers_only:
            sql += " AND is_teacher = 1"
        sql += " ORDER BY ts ASC"
        rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["raw"] = json.loads(d["raw"])
            d["urls"] = json.loads(d["urls"])
            out.append(d)
        return out

    def has_message(self, message_id: int) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            is not None
        )

    # ---------- pages ----------

    def get_page(self, url: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()
        return dict(r) if r else None

    def save_page(self, page: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute(
                """INSERT OR REPLACE INTO pages
                   (url, final_url, title, content, content_type, depth,
                    parent_url, truncated, children, error, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    page["url"], page.get("final_url", ""), page.get("title", ""),
                    page.get("content", ""), page.get("content_type", ""),
                    page.get("depth", 0), page.get("parent_url"),
                    int(bool(page.get("truncated"))),
                    json.dumps(page.get("children") or [], ensure_ascii=False),
                    page.get("error"),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    # ---------- digests ----------

    def save_digest(self, day: str, payload: dict, usage: dict) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO digests (day, payload, usage, created_at) VALUES (?,?,?,?)",
                (
                    day,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(usage, ensure_ascii=False),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    def get_digest(self, day: str) -> dict | None:
        r = self._conn.execute("SELECT * FROM digests WHERE day = ?", (day,)).fetchone()
        if not r:
            return None
        return {"day": r["day"], "payload": json.loads(r["payload"]), "usage": json.loads(r["usage"])}

    def stats(self) -> dict[str, int]:
        q = lambda s: self._conn.execute(s).fetchone()[0]  # noqa: E731
        return {
            "messages": q("SELECT COUNT(*) FROM messages"),
            "teacher_messages": q("SELECT COUNT(*) FROM messages WHERE is_teacher=1"),
            "pages": q("SELECT COUNT(*) FROM pages"),
            "digests": q("SELECT COUNT(*) FROM digests"),
        }

    def close(self) -> None:
        self._conn.close()
