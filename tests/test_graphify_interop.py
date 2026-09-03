"""What we export must be readable by graphify's own parser, byte-for-byte fields.

If graphify is importable the real parser is used; otherwise the vendored copy
of its grammar stands in, so the suite still runs in a bare checkout.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from parallel_memory import graphify_sync  # noqa: E402
from parallel_memory.store import Store  # noqa: E402

try:  # the real thing, when it is installed alongside
    from graphify.reflect import load_memory_docs as _graphify_load
    HAVE_GRAPHIFY = True
except ImportError:  # pragma: no cover
    _graphify_load = None
    HAVE_GRAPHIFY = False


def _load(memory_dir):
    if HAVE_GRAPHIFY:
        return _graphify_load(Path(memory_dir))
    return graphify_sync.import_dir(memory_dir)


AWKWARD = [
    ('a "quoted" question', ["mod:auth"]),
    ("tab\there", []),
    ("line\nbreak", []),
    ("unicode   separator", ["fn:x", "fn:y"]),
    ("日本語の質問", ["ノード:1"]),
]


def test_exported_docs_parse_back_identically(tmp_path):
    store = Store(path=tmp_path / "m.db", writer="agent-a")
    for question, nodes in AWKWARD:
        store.remember(question, "an answer", outcome="useful", source_nodes=nodes)

    memory_dir = tmp_path / "graphify-out" / "memory"
    graphify_sync.export(store.all(), memory_dir)

    docs = _load(memory_dir)
    assert len(docs) == len(AWKWARD)
    assert {d["question"] for d in docs} == {q for q, _ in AWKWARD}
    by_q = {d["question"]: d for d in docs}
    for question, nodes in AWKWARD:
        assert by_q[question]["source_nodes"] == nodes
        assert by_q[question]["outcome"] == "useful"
        assert by_q[question]["contributor"] == "agent-a"


def test_export_is_idempotent(tmp_path):
    store = Store(path=tmp_path / "m.db", writer="agent-a")
    store.remember("q", "a")
    memory_dir = tmp_path / "memory"

    first = graphify_sync.export(store.all(), memory_dir)
    mtimes = {p: p.stat().st_mtime_ns for p in first}
    second = graphify_sync.export(store.all(), memory_dir)

    assert [p.name for p in first] == [p.name for p in second]
    assert {p: p.stat().st_mtime_ns for p in second} == mtimes, (
        "an unchanged export rewrote files — graphify would see spurious churn"
    )


def test_filenames_survive_the_case_graphify_loses(tmp_path):
    """Same second, same first 50 characters: graphify collapses these to one file."""
    store = Store(path=tmp_path / "m.db", writer="agent-a")
    prefix = "how does the authentication subsystem resolve tokens"
    for i in range(10):
        store.remember(f"{prefix} in module {i}", f"answer {i}")

    memory_dir = tmp_path / "memory"
    written = graphify_sync.export(store.all(), memory_dir)

    assert len({p.name for p in written}) == 10
    assert len(list(memory_dir.glob("*.md"))) == 10
    assert len(_load(memory_dir)) == 10


def test_two_writers_never_share_a_filename(tmp_path):
    a = Store(path=tmp_path / "m.db", writer="worktree-a")
    b = Store(path=tmp_path / "m.db", writer="worktree-b")
    a.remember("how does auth work", "answer from a")
    b.remember("how does auth work", "answer from b")

    memory_dir = tmp_path / "memory"
    written = graphify_sync.export(a.all(), memory_dir)
    assert len({p.name for p in written}) == 2
    assert {d["contributor"] for d in _load(memory_dir)} == {"worktree-a", "worktree-b"}
