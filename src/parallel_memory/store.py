"""SQLite-backed observation store.

The whole point of this file is that concurrent writers do not lose each
other's work. Three decisions carry that:

1. **WAL** — a writer does not block readers, so serialising writes costs
   nothing on the read side.
2. **BEGIN IMMEDIATE + busy_timeout** — the write lock is taken when the
   transaction opens, not at first write, so two writers queue instead of one
   discovering at COMMIT that it must roll back (SQLITE_BUSY on upgrade).
3. **Identity is assigned, never derived** — every observation gets a UUID.
   A store that derives an id from timestamp+content silently merges two
   distinct observations that happen to collide; this one cannot.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .writer import store_path, writer_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id            TEXT PRIMARY KEY,
    writer        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    question      TEXT NOT NULL,
    answer        TEXT NOT NULL,
    outcome       TEXT,
    correction    TEXT,
    source_nodes  TEXT NOT NULL DEFAULT '[]',
    meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS observations_writer  ON observations(writer);
CREATE INDEX IF NOT EXISTS observations_created ON observations(created_at);
"""

OUTCOMES = ("useful", "dead_end", "corrected")


class Store:
    def __init__(self, path: "str | Path | None" = None, writer: str | None = None):
        self.path = Path(path) if path is not None else store_path()
        self.writer = writer or writer_id()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)
            self._migrate(con)

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        """Additive migrations, so a store written by an older build still opens."""
        cols = {r["name"] for r in con.execute("PRAGMA table_info(observations)")}
        if "meta" not in cols:
            con.execute("ALTER TABLE observations ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA busy_timeout=30000")
            yield con
        finally:
            con.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that takes the lock up front and waits its turn."""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")

    # --- writes ---------------------------------------------------------

    def remember(self, question: str, answer: str, *, outcome: str | None = None,
                 correction: str | None = None,
                 source_nodes: list[str] | None = None,
                 meta: dict[str, Any] | None = None) -> dict[str, Any]:
        if outcome is not None and outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
        row = {
            "id": uuid.uuid4().hex,
            "writer": self.writer,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "question": question,
            "answer": answer,
            "outcome": outcome,
            "correction": correction,
            "source_nodes": json.dumps(list(source_nodes or []), ensure_ascii=False),
            "meta": json.dumps(dict(meta or {}), ensure_ascii=False),
        }
        with self._write() as con:
            con.execute(
                "INSERT INTO observations "
                "(id, writer, created_at, question, answer, outcome, correction, "
                "source_nodes, meta) "
                "VALUES (:id, :writer, :created_at, :question, :answer, :outcome, "
                ":correction, :source_nodes, :meta)", row)
        return _decode(row)

    def forget(self, observation_id: str) -> bool:
        with self._write() as con:
            cur = con.execute("DELETE FROM observations WHERE id = ?", (observation_id,))
            return cur.rowcount > 0

    # --- reads ----------------------------------------------------------

    def recall(self, query: str = "", *, writer: str | None = None,
               outcome: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM observations"
        clauses, params = [], []
        if query:
            clauses.append("(question LIKE ? OR answer LIKE ? OR correction LIKE ?)")
            params += [f"%{query}%"] * 3
        if writer:
            clauses.append("writer = ?")
            params.append(writer)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as con:
            return [_decode(dict(r)) for r in con.execute(sql, params)]

    def all(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            return [_decode(dict(r)) for r in
                    con.execute("SELECT * FROM observations ORDER BY created_at, id")]

    def writers(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            return [dict(r) for r in con.execute(
                "SELECT writer, COUNT(*) AS n, MAX(created_at) AS last_write "
                "FROM observations GROUP BY writer ORDER BY n DESC")]

    def count(self) -> int:
        with self._connect() as con:
            return con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if isinstance(out.get("source_nodes"), str):
        try:
            out["source_nodes"] = json.loads(out["source_nodes"])
        except json.JSONDecodeError:
            out["source_nodes"] = []
    if isinstance(out.get("meta"), str):
        try:
            out["meta"] = json.loads(out["meta"])
        except json.JSONDecodeError:
            out["meta"] = {}
    out.setdefault("meta", {})
    return out
