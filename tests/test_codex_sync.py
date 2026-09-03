"""Codex's memory is read, never written — and reading must not disturb it."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from parallel_memory import codex_sync as cx  # noqa: E402
from parallel_memory.store import Store  # noqa: E402

# The shape Codex actually uses (codex-cli 0.150.1, memories_1.sqlite).
STAGE1_DDL = """
CREATE TABLE stage1_outputs (
    thread_id TEXT PRIMARY KEY,
    source_updated_at INTEGER NOT NULL,
    raw_memory TEXT NOT NULL,
    rollout_summary TEXT NOT NULL,
    rollout_slug TEXT,
    generated_at INTEGER NOT NULL,
    usage_count INTEGER,
    last_usage INTEGER,
    selected_for_phase2 INTEGER NOT NULL DEFAULT 0,
    selected_for_phase2_source_updated_at INTEGER
);
"""


@pytest.fixture
def codex_db(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    db = home / "memories_1.sqlite"
    con = sqlite3.connect(db)
    con.executescript(STAGE1_DDL)
    con.executemany(
        "INSERT INTO stage1_outputs (thread_id, source_updated_at, raw_memory, "
        "rollout_summary, rollout_slug, generated_at, usage_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("t1", 1, "the auth gate is split in two", "how auth works", "auth", 1, 3),
         ("t2", 2, "token refresh retries twice", "token refresh", "token", 2, 1)],
    )
    con.commit()
    con.close()
    return home, db


def test_finds_the_newest_memories_db(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    for name in ("memories_1.sqlite", "memories_2.sqlite"):
        (home / name).write_bytes(b"")
    assert cx.memories_db(home).name == "memories_2.sqlite"


def test_missing_store_is_not_an_error(tmp_path):
    assert cx.memories_db(tmp_path / "nope") is None
    assert cx.read_memories(tmp_path / "nope.sqlite") == []


def test_reads_distilled_memories(codex_db):
    _, db = codex_db
    rows = cx.read_memories(db)
    assert {r["thread_id"] for r in rows} == {"t1", "t2"}
    assert rows[0]["raw_memory"]


def test_import_is_attributed_and_idempotent(codex_db, tmp_path):
    _, db = codex_db
    store = Store(path=tmp_path / "m.db", writer="test")

    first = cx.sync(store, db_path=db)
    assert first["rows"] == 2 and first["imported"] == 2
    assert store.count() == 2
    obs = {o["meta"]["thread_id"]: o for o in store.all()}
    assert obs["t1"]["meta"]["origin"] == "codex"
    assert obs["t1"]["question"] == "how auth works"
    assert "split in two" in obs["t1"]["answer"]

    second = cx.sync(store, db_path=db)
    assert second["imported"] == 0
    assert store.count() == 2


def test_reading_never_modifies_codex(codex_db, tmp_path):
    """The file must be byte-identical afterwards."""
    _, db = codex_db
    before = db.read_bytes()
    store = Store(path=tmp_path / "m.db", writer="test")
    cx.sync(store, db_path=db)
    assert db.read_bytes() == before, "reading Codex's memory mutated its database"


def test_a_foreign_schema_is_ignored_rather_than_crashing(tmp_path):
    db = tmp_path / "memories_9.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE something_else (a TEXT)")
    con.commit()
    con.close()
    assert cx.read_memories(db) == []


def test_agents_markdown_surface(tmp_path):
    store = Store(path=tmp_path / "m.db", writer="worktree-a")
    store.remember("how does auth work", "it is split in two", outcome="useful")
    out = tmp_path / "CODEX_MEMORY.md"

    cx.export_agents_memory(store.all(), out)
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Shared observations")
    assert "## how does auth work" in text
    assert "`worktree-a`" in text

    mtime = out.stat().st_mtime_ns
    cx.export_agents_memory(store.all(), out)
    assert out.stat().st_mtime_ns == mtime, "an unchanged export rewrote the file"
