"""Two agents, one worktree: exactly one of them may hold it at a time."""
from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from parallel_memory import worktree_lease as wl  # noqa: E402


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
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    wt = root / ".worktrees" / "5136"
    _git("worktree", "add", "-q", "-b", "fix-5136", str(wt), cwd=root)
    return root, wt


def test_claim_then_second_claimer_is_refused(repo):
    root, wt = repo
    a = wl.claim(wt, task="fix auth", holder="agent-a", root=root)
    assert a.holder == "agent-a" and not a.expired

    with pytest.raises(wl.LeaseHeld) as e:
        wl.claim(wt, task="something else", holder="agent-b", root=root)
    assert "agent-a" in str(e.value)

    held = wl.holders(root)
    assert len(held) == 1 and held[0].holder == "agent-a" and held[0].task == "fix auth"


def test_the_lock_is_visible_to_plain_git(repo):
    """Anything that runs `git worktree list` sees who holds it — no server needed."""
    root, wt = repo
    wl.claim(wt, task="fix auth", holder="agent-a", root=root)
    out = _git("worktree", "list", "--porcelain", cwd=root).stdout
    # holder and expiry are plain text; the task is base64url so git never has
    # to C-quote the reason (which would octal-escape non-ASCII and break parsing)
    assert "locked parallel-memory;holder=agent-a;until=" in out
    assert "fix auth" not in out and wl.holders(root)[0].task == "fix auth"


def test_only_the_holder_may_release(repo):
    root, wt = repo
    wl.claim(wt, holder="agent-a", root=root)
    with pytest.raises(wl.NotHolder):
        wl.release(wt, holder="agent-b", root=root)
    assert wl.release(wt, holder="agent-a", root=root) is True
    assert wl.holders(root) == []
    assert wl.release(wt, holder="agent-a", root=root) is False


def test_expired_lease_is_reclaimable(repo):
    """A crashed holder must not pin the worktree forever."""
    root, wt = repo
    wl.claim(wt, holder="agent-a", ttl=timedelta(seconds=-1), root=root)
    assert wl.holders(root)[0].expired

    b = wl.claim(wt, task="took over", holder="agent-b", root=root)
    assert b.holder == "agent-b"
    assert wl.holders(root)[0].holder == "agent-b"


def test_reap_drops_only_expired(repo):
    root, wt = repo
    wt2 = root / ".worktrees" / "5137"
    _git("worktree", "add", "-q", "-b", "fix-5137", str(wt2), cwd=root)
    wl.claim(wt, holder="agent-a", ttl=timedelta(seconds=-1), root=root)
    wl.claim(wt2, holder="agent-b", root=root)

    dropped = wl.reap(root)
    assert [l.holder for l in dropped] == ["agent-a"]
    assert [l.holder for l in wl.holders(root)] == ["agent-b"]


def test_reclaiming_your_own_lease_renews_it(repo):
    root, wt = repo
    first = wl.claim(wt, holder="agent-a", ttl=timedelta(minutes=1), root=root)
    second = wl.renew(wt, holder="agent-a", ttl=timedelta(hours=3), root=root)
    assert second.until > first.until
    with pytest.raises(wl.NotHolder):
        wl.renew(wt, holder="agent-b", root=root)


def test_a_foreign_lock_is_respected_not_stolen(repo):
    """A lock written by a human with plain git has no expiry; never take it."""
    root, wt = repo
    _git("worktree", "lock", "--reason", "manual: do not touch", str(wt), cwd=root)
    held = wl.holders(root)
    assert held[0].holder == "(foreign lock)" and not held[0].expired
    with pytest.raises(wl.LeaseHeld):
        wl.claim(wt, holder="agent-a", root=root)
    assert wl.reap(root) == []
