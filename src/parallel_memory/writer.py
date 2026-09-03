"""Writer identity and store location.

Two agents must never be told they are the same writer, and two worktrees of
one repository must never be given different stores. Both rules are decided
here, once, so every entry point agrees.
"""
from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path


def repo_root(start: Path | None = None) -> Path | None:
    """The shared root of the repository, identical from every linked worktree.

    ``--git-common-dir`` points at the ORIGINAL ``.git`` even when called from
    ``<repo>/.worktrees/<name>``, so a store anchored here is shared instead of
    forked per worktree. ``--git-dir`` would fork it: in a linked worktree it
    resolves to ``.git/worktrees/<name>``.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(start or Path.cwd()),
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    if not out:
        return None
    common = Path(out)
    # <root>/.git -> <root>;  a bare repo stays where it is.
    return common.parent if common.name == ".git" else common


def store_path() -> Path:
    """Where the SQLite store lives.

    ``PARALLEL_MEMORY_DB`` wins; otherwise the repository root, so every
    worktree of the repo opens the same file.
    """
    env = os.environ.get("PARALLEL_MEMORY_DB")
    if env:
        return Path(env).expanduser()
    root = repo_root()
    if root is None:
        return Path.cwd() / ".parallel-memory" / "memory.db"
    return root / ".parallel-memory" / "memory.db"


def writer_id() -> str:
    """A stable, human-legible identity for this writer.

    ``PARALLEL_MEMORY_WRITER`` wins. Otherwise host + the checkout directory,
    which distinguishes ``.worktrees/feature-a`` from ``.worktrees/feature-b``
    while both still write into the one shared store.
    """
    env = os.environ.get("PARALLEL_MEMORY_WRITER")
    if env:
        return _slug(env)
    host = socket.gethostname().split(".")[0]
    return _slug(f"{host}-{Path.cwd().name}")


def _slug(s: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in s]
    return "".join(keep).strip("-").lower()[:64] or "anonymous"
