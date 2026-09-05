"""An observation must say which source it was about, and recall must say if that moved."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from parallel_memory import provenance  # noqa: E402
from parallel_memory.store import Store  # noqa: E402


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"; root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root); _git("config", "user.name", "t", cwd=root)
    (root / "auth.ts").write_text("export const gate = 1;\n"); (root / "other.ts").write_text("x\n")
    _git("add", "-A", cwd=root); _git("commit", "-qm", "init", cwd=root)
    _git("checkout", "-q", "-b", "staging", cwd=root)
    (root / "auth.ts").write_text("export const gate = 2;\n")
    _git("commit", "-qam", "staging changes auth", cwd=root)
    _git("checkout", "-q", "main", cwd=root)
    return root


def _in(cwd):
    class _cd:
        def __enter__(self): self.old = os.getcwd(); os.chdir(cwd)
        def __exit__(self, *a): os.chdir(self.old)
    return _cd()


def test_remember_records_branch_head_and_blobs(repo, tmp_path):
    with _in(repo):
        store = Store(path=tmp_path / "m.db", writer="a")
        obs = store.remember("how does the gate work", "gate is 1", source_nodes=["auth.ts"])
    prov = obs["meta"]["provenance"]
    assert prov["branch"] == "main" and len(prov["head"]) == 40 and prov["dirty"] is False
    assert set(prov["blobs"]) == {"auth.ts"}


def test_recall_from_another_branch_marks_it_stale(repo, tmp_path):
    with _in(repo):
        store = Store(path=tmp_path / "m.db", writer="a")
        store.remember("how does the gate work", "gate is 1", source_nodes=["auth.ts"])
        assert store.recall("gate")[0]["stale"] is False
    _git("checkout", "-q", "staging", cwd=repo)
    with _in(repo):
        got = Store(path=tmp_path / "m.db", writer="b").recall("gate")[0]
    assert got["stale"] is True and got["changed_since"] == ["auth.ts"]


def test_unrelated_commit_does_not_make_it_stale(repo, tmp_path):
    with _in(repo):
        store = Store(path=tmp_path / "m.db", writer="a")
        store.remember("q", "a", source_nodes=["auth.ts"])
    (repo / "other.ts").write_text("y\n"); _git("commit", "-qam", "touch other", cwd=repo)
    with _in(repo):
        got = Store(path=tmp_path / "m.db", writer="a").recall("q")[0]
    assert got["stale"] is False


def test_uncommitted_edit_makes_it_stale_immediately(repo, tmp_path):
    with _in(repo):
        store = Store(path=tmp_path / "m.db", writer="a")
        store.remember("q", "a", source_nodes=["auth.ts"])
        (repo / "auth.ts").write_text("export const gate = 3;\n")
        got = store.recall("q")[0]
    assert got["stale"] is True


def test_outside_a_repo_records_nothing_and_never_fails(tmp_path):
    with _in(tmp_path):
        store = Store(path=tmp_path / "m.db", writer="a")
        obs = store.remember("q", "a", source_nodes=["auth.ts"])
        got = store.recall("q")[0]
    assert "provenance" not in obs["meta"] and "stale" not in got


def test_export_carries_head_and_branch(repo, tmp_path):
    from parallel_memory import graphify_sync
    with _in(repo):
        store = Store(path=tmp_path / "m.db", writer="a")
        store.remember("q", "a", source_nodes=["auth.ts"])
    paths = graphify_sync.export(store.all(), tmp_path / "mem")
    fm = paths[0].read_text().split("---")[1]
    assert 'branch: "main"' in fm and "head: " in fm
