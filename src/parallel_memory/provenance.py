"""Which source an observation was about.

Two worktrees of one repository can sit hundreds of commits apart. An
observation written in one ("the auth gate is split in two") may be false in
the other, and nothing in the text says so. So every observation records the
checkout it was made against, and recall compares that against the checkout it
is being read from.

The comparison is by blob, not by commit: what matters is whether the *files
the observation names* have changed, not whether any commit happened. A commit
that touched unrelated files leaves the observation fresh; an uncommitted edit
to a named file makes it stale immediately.

All of this is best effort. A store outside a git repository, or a
``source_node`` that is not a path, simply records nothing — a write never
fails because provenance could not be gathered.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .writer import repo_root


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                           text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _blob(path: str, cwd: Path) -> str | None:
    """The blob the caller is actually looking at: HEAD's, or the dirty file's."""
    p = Path(path)
    rel = str(p.relative_to(cwd)) if p.is_absolute() and p.is_relative_to(cwd) else path
    if (cwd / rel).is_file():
        dirty = _git(["status", "--porcelain", "--", rel], cwd)
        if dirty:
            return _git(["hash-object", "--", rel], cwd)
    return _git(["rev-parse", "--quiet", "--verify", f"HEAD:{rel}"], cwd)


def capture(source_nodes: list[str] | None, cwd: "str | Path | None" = None) -> dict[str, Any]:
    """Snapshot of the checkout an observation is being written against."""
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    root = repo_root(cwd)
    if root is None:
        return {}
    head = _git(["rev-parse", "HEAD"], cwd)
    if head is None:
        return {}
    prov: dict[str, Any] = {
        "repo": str(root),
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or "",
        "head": head,
        "dirty": bool(_git(["status", "--porcelain", "--untracked-files=no"], cwd)),
    }
    blobs = {}
    for node in source_nodes or []:
        sha = _blob(node, cwd)
        if sha:
            blobs[node] = sha
    if blobs:
        prov["blobs"] = blobs
    return prov


def staleness(prov: dict[str, Any], cwd: "str | Path | None" = None) -> dict[str, Any]:
    """Compare a recorded snapshot with the checkout recall is running in.

    Returns ``{"stale": bool, "changed": [paths], "same_head": bool}``; empty
    when there is nothing to compare (no provenance, or no named files).
    """
    if not prov or not prov.get("blobs"):
        return {}
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    head = _git(["rev-parse", "HEAD"], cwd)
    changed = [p for p, sha in prov["blobs"].items() if _blob(p, cwd) != sha]
    return {"stale": bool(changed), "changed": changed,
            "same_head": head == prov.get("head")}
