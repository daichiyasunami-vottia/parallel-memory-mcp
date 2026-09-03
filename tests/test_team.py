"""Two machines, one repository: neither may lose the other's observations."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from parallel_memory import team  # noqa: E402
from parallel_memory.store import Store  # noqa: E402


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "README.md").write_text("x\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


def test_export_then_import_round_trips(repo, tmp_path):
    shared = repo / "agent-memory"
    a = Store(path=tmp_path / "a.db", writer="alice")
    a.remember("how does auth work", "it is split in two", outcome="useful")
    team.sync(a, shared)

    b = Store(path=tmp_path / "b.db", writer="bob")
    imported = team.import_shared(b, shared)

    assert imported == 1
    got = b.all()[0]
    assert got["question"] == "how does auth work"
    assert "split in two" in got["answer"]
    assert got["meta"]["contributor"] == "alice"


def test_import_is_idempotent(repo, tmp_path):
    shared = repo / "agent-memory"
    a = Store(path=tmp_path / "a.db", writer="alice")
    a.remember("q", "a")
    team.sync(a, shared)

    b = Store(path=tmp_path / "b.db", writer="bob")
    assert team.import_shared(b, shared) == 1
    assert team.import_shared(b, shared) == 0
    assert b.count() == 1


def test_a_writer_does_not_reimport_its_own_export(repo, tmp_path):
    shared = repo / "agent-memory"
    a = Store(path=tmp_path / "a.db", writer="alice")
    a.remember("q", "a")

    first = team.sync(a, shared)
    second = team.sync(a, shared)

    assert first["imported"] == 0
    assert second["imported"] == 0
    assert a.count() == 1


def test_two_writers_never_collide_on_a_path(repo, tmp_path):
    """No merge driver is needed because no path is ever written by both."""
    shared = repo / "agent-memory"
    a = Store(path=tmp_path / "a.db", writer="alice")
    b = Store(path=tmp_path / "b.db", writer="bob")
    a.remember("how does auth work", "answer from alice")
    b.remember("how does auth work", "answer from bob")

    a_files = {p.name for p in team.export_shared(a, shared)}
    b_files = {p.name for p in team.export_shared(b, shared)}

    assert not (a_files & b_files), "two writers produced the same filename"
    assert len(list(shared.glob("*.md"))) == 2


def test_sync_merges_both_directions(repo, tmp_path):
    shared = repo / "agent-memory"
    a = Store(path=tmp_path / "a.db", writer="alice")
    b = Store(path=tmp_path / "b.db", writer="bob")
    a.remember("from alice", "a")
    b.remember("from bob", "b")

    team.sync(a, shared)
    team.sync(b, shared)     # bob absorbs alice, then publishes his own
    team.sync(a, shared)     # alice absorbs bob

    assert {o["question"] for o in a.all()} == {"from alice", "from bob"}
    assert {o["question"] for o in b.all()} == {"from alice", "from bob"}


def test_commit_writes_a_commit_then_stops(repo, tmp_path):
    shared = repo / "agent-memory"
    a = Store(path=tmp_path / "a.db", writer="alice")
    a.remember("q", "a")

    first = team.sync(a, shared, commit=True)
    assert first["committed"] is True

    second = team.sync(a, shared, commit=True)
    assert second["committed"] is False, "an unchanged sync made an empty commit"

    log = subprocess.run(["git", "log", "--oneline"], cwd=str(repo),
                         capture_output=True, text=True).stdout
    assert log.count("chore(memory)") == 1


def test_hooks_are_installed_once_and_append(repo):
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "post-merge").write_text("#!/bin/sh\necho existing\n")

    installed = team.install_hooks(root=repo)
    assert {p.name for p in installed} == {"post-merge", "post-checkout"}

    body = (hooks / "post-merge").read_text()
    assert "echo existing" in body, "an existing hook was clobbered"
    assert team.HOOK_MARKER in body

    assert team.install_hooks(root=repo) == [], "hooks were installed twice"


# --- the shared HTTP server -------------------------------------------------

def _call_gate(gate, headers):
    """Drive the ASGI gate directly, so the test needs no server."""
    import asyncio
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    gate.app = inner
    scope = {"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers]}
    asyncio.run(gate(scope, receive, send))
    return sent[0]["status"]


def test_http_rejects_a_missing_or_wrong_key():
    from parallel_memory.server import _ApiKeyGate
    gate = _ApiKeyGate(None, "s3cret")
    assert _call_gate(gate, []) == 401
    assert _call_gate(gate, [("authorization", "Bearer wrong")]) == 401
    assert _call_gate(gate, [("x-api-key", "wrong")]) == 401


def test_http_accepts_the_key_either_way():
    from parallel_memory.server import _ApiKeyGate
    gate = _ApiKeyGate(None, "s3cret")
    assert _call_gate(gate, [("authorization", "Bearer s3cret")]) == 200
    assert _call_gate(gate, [("x-api-key", "s3cret")]) == 200
