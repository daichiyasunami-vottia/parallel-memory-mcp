"""A linked worktree must open the same store, not a fresh one."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from parallel_memory.writer import repo_root  # noqa: E402


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


def test_worktree_resolves_to_the_main_root(repo):
    """The convention here is <repo>/.worktrees/<name>."""
    wt = repo / ".worktrees" / "feature-a"
    _git("worktree", "add", "-q", "-b", "feature-a", str(wt), cwd=repo)

    assert repo_root(repo) == repo.resolve()
    assert repo_root(wt) == repo.resolve(), (
        "a linked worktree resolved to its own store — memory would fork per worktree"
    )


def test_two_worktrees_share_one_root(repo):
    a = repo / ".worktrees" / "a"
    b = repo / ".worktrees" / "b"
    _git("worktree", "add", "-q", "-b", "a", str(a), cwd=repo)
    _git("worktree", "add", "-q", "-b", "b", str(b), cwd=repo)
    assert repo_root(a) == repo_root(b) == repo.resolve()
