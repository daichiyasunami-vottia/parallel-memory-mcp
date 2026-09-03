"""Coordination state for worktrees: who holds which one, until when.

This is deliberately NOT stored in the observation store. Observations are
append-only and survive by never being overwritten; a lease is the opposite —
its whole meaning is that it changes hands, and if two agents both "hold" a
worktree the state is simply wrong. Append-only cannot express that.

git already has the primitive: ``git worktree lock --reason`` is persisted,
visible to every tool through ``git worktree list --porcelain``, and a second
lock on a locked worktree is refused — a compare-and-swap. What it lacks is an
expiry and an owner check on ``unlock``. Both are supplied here by putting a
small JSON payload in the reason string, so anything that runs plain git still
sees a readable reason, and nothing here needs a database or a server.
"""
from __future__ import annotations

import base64
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .writer import repo_root, writer_id

DEFAULT_TTL = timedelta(hours=2)
MARKER = "parallel-memory"


class LeaseHeld(Exception):
    """The worktree is held by someone else and the lease has not expired."""


class NotHolder(Exception):
    """A release or renew was attempted by someone who does not hold the lease."""


@dataclass(frozen=True)
class Lease:
    worktree: str
    holder: str
    task: str
    until: datetime
    raw_reason: str

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.until

    def to_dict(self) -> dict[str, Any]:
        return {"worktree": self.worktree, "holder": self.holder, "task": self.task,
                "until": self.until.isoformat(), "expired": self.expired}


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=30)


def _root(root: "str | Path | None") -> Path:
    r = Path(root) if root is not None else repo_root()
    if r is None:
        raise ValueError("not inside a git repository")
    return r


def _reason(holder: str, task: str, until: datetime) -> str:
    """Encode the lease so git never has to quote it.

    ``git worktree list --porcelain`` C-quotes a reason that contains spaces,
    quotes or non-ASCII (non-ASCII becomes octal escapes), which would make a
    JSON payload unparseable. So the payload has no spaces at all: ``key=value``
    pairs joined by ``;``, holder and expiry kept readable, and only the task
    (which may be anything, including Japanese) base64url-encoded.
    """
    task_b64 = base64.urlsafe_b64encode(task.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{MARKER};holder={holder};until={until.isoformat()};task={task_b64}"


def _parse(worktree: str, reason: str) -> Lease | None:
    """A lock we did not write has no holder or expiry; treat it as held forever."""
    reason = reason.strip()
    if reason.startswith('"') and reason.endswith('"'):
        reason = reason[1:-1]                # git quoted it anyway; strip once
    if not reason.startswith(MARKER + ";"):
        return Lease(worktree, "(foreign lock)", reason,
                     datetime.max.replace(tzinfo=timezone.utc), reason)
    fields = dict(part.split("=", 1) for part in reason.split(";")[1:] if "=" in part)
    try:
        until = datetime.fromisoformat(fields["until"])
        pad = "=" * (-len(fields.get("task", "")) % 4)
        task = base64.urlsafe_b64decode(fields.get("task", "") + pad).decode("utf-8")
        return Lease(worktree, fields["holder"], task, until, reason)
    except (KeyError, ValueError, UnicodeDecodeError):
        return Lease(worktree, "(foreign lock)", reason,
                     datetime.max.replace(tzinfo=timezone.utc), reason)


def holders(root: "str | Path | None" = None) -> list[Lease]:
    """Every locked worktree of the repository, with holder and expiry."""
    r = _root(root)
    out = _git(["worktree", "list", "--porcelain"], r).stdout
    leases, path = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("locked") and path:
            reason = line[len("locked"):].strip()
            leases.append(_parse(path, reason))
    return leases


def _lease_for(worktree: Path, root: Path) -> Lease | None:
    target = worktree.resolve()
    for lease in holders(root):
        if Path(lease.worktree).resolve() == target:
            return lease
    return None


def claim(worktree: "str | Path", *, task: str = "", holder: str | None = None,
          ttl: timedelta = DEFAULT_TTL, root: "str | Path | None" = None) -> Lease:
    """Take the worktree, or fail if someone else holds an unexpired lease.

    An expired lease is reclaimed: the previous holder crashed or forgot, and
    the expiry is exactly what makes that recoverable without a human.
    Re-claiming a worktree you already hold renews it.
    """
    r = _root(root)
    wt = Path(worktree)
    me = _slug(holder or writer_id())
    current = _lease_for(wt, r)
    if current is not None:
        if current.holder != me and not current.expired:
            raise LeaseHeld(f"{wt} is held by {current.holder} until "
                            f"{current.until.isoformat()} (task: {current.task})")
        _git(["worktree", "unlock", str(wt)], r)      # ours, or expired: take over
    until = datetime.now(timezone.utc) + ttl
    done = _git(["worktree", "lock", "--reason", _reason(me, task, until), str(wt)], r)
    if done.returncode != 0:
        # Lost a race to another claimer between unlock and lock.
        now = _lease_for(wt, r)
        raise LeaseHeld(f"{wt} was claimed concurrently"
                        + (f" by {now.holder}" if now else "") + f": {done.stderr.strip()}")
    return Lease(str(wt), me, task, until, _reason(me, task, until))


def renew(worktree: "str | Path", *, holder: str | None = None,
          ttl: timedelta = DEFAULT_TTL, root: "str | Path | None" = None) -> Lease:
    r = _root(root)
    me = _slug(holder or writer_id())
    current = _lease_for(Path(worktree), r)
    if current is None or current.holder != me:
        raise NotHolder(f"{worktree} is not held by {me}")
    return claim(worktree, task=current.task, holder=me, ttl=ttl, root=r)


def _slug(s: str) -> str:
    keep = [c if (c.isalnum() or c in "-_.") else "-" for c in s]
    return "".join(keep).strip("-") or "anonymous"


def release(worktree: "str | Path", *, holder: str | None = None,
            root: "str | Path | None" = None, force: bool = False) -> bool:
    """Give the worktree back. Only the holder may, unless ``force``.

    Returns False when there was nothing to release.
    """
    r = _root(root)
    me = _slug(holder or writer_id())
    current = _lease_for(Path(worktree), r)
    if current is None:
        return False
    if current.holder != me and not force and not current.expired:
        raise NotHolder(f"{worktree} is held by {current.holder}, not {me}")
    _git(["worktree", "unlock", str(worktree)], r)
    return True


def reap(root: "str | Path | None" = None) -> list[Lease]:
    """Drop every expired lease. Safe to run from any agent at any time."""
    r = _root(root)
    dropped = []
    for lease in holders(r):
        if lease.expired and lease.holder != "(foreign lock)":
            _git(["worktree", "unlock", lease.worktree], r)
            dropped.append(lease)
    return dropped
