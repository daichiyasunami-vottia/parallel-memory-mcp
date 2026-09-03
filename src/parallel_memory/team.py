"""Sharing observations with a team, over git.

The store itself is SQLite, which cannot be merged, so it stays local and
ignored. What gets committed is the text export: one markdown file per
observation, named with the writer and the observation's UUID.

That naming is what makes this work without a merge driver. Two people never
write the same path, so a pull is always a fast-forward of *files* — git never
has to reconcile the contents of one. Nothing is ever rewritten in place
either, so history stays append-only.

The SQLite store is then a local cache of the union: `sync` imports every
teammate's file it has not seen, and exports its own.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import graphify_sync
from .writer import repo_root

DEFAULT_SHARED_DIR = "agent-memory"


def shared_dir(root: "str | Path | None" = None,
               name: str = DEFAULT_SHARED_DIR) -> Path | None:
    root = Path(root) if root is not None else repo_root()
    return None if root is None else Path(root) / name


def import_shared(store, directory: "str | Path") -> int:
    """Absorb every committed observation this store has not seen."""
    directory = Path(directory)
    if not directory.is_dir():
        return 0
    known = {o["id"] for o in store.all()}
    known |= {(o.get("meta") or {}).get("memory_id") for o in store.all()}
    imported = 0
    for path in sorted(directory.glob("*.md")):
        try:
            doc = graphify_sync.parse_doc(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if not doc or not doc.get("question"):
            continue
        memory_id = doc.get("memory_id") or ""
        if memory_id and memory_id in known:
            continue
        store.remember(
            doc["question"],
            graphify_sync._answer_section(path.read_text(encoding="utf-8")),
            outcome=doc.get("outcome") or None,
            correction=doc.get("correction") or None,
            source_nodes=doc.get("source_nodes") or [],
            meta={"origin": "team", "memory_id": memory_id,
                  "contributor": doc.get("contributor", ""),
                  "file": path.name},
        )
        known.add(memory_id)
        imported += 1
    return imported


def export_shared(store, directory: "str | Path") -> list[Path]:
    """Write this store's observations as committed files. Idempotent."""
    return graphify_sync.export(store.all(), directory)


def sync(store, directory: "str | Path | None" = None, *,
         commit: bool = False, root: "str | Path | None" = None) -> dict[str, Any]:
    """Import what teammates committed, then export what this machine has."""
    directory = Path(directory) if directory is not None else shared_dir(root)
    if directory is None:
        raise ValueError("not inside a git repository; pass a directory")
    imported = import_shared(store, directory)
    written = export_shared(store, directory)
    result = {"dir": str(directory), "imported": imported,
              "exported": len(written), "committed": False}
    if commit:
        result["committed"] = _commit(directory, imported, len(written))
    return result


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=30)


def _commit(directory: Path, imported: int, exported: int) -> bool:
    """Commit the shared directory. Returns False when there was nothing to do."""
    root = repo_root(directory)
    if root is None:
        return False
    rel = directory.relative_to(root) if directory.is_relative_to(root) else directory
    _git(["add", "--", str(rel)], root)
    staged = _git(["diff", "--cached", "--quiet", "--", str(rel)], root)
    if staged.returncode == 0:          # exit 0 means no staged changes
        return False
    message = (f"chore(memory): share {exported} observation(s)"
               + (f", absorbed {imported}" if imported else ""))
    done = _git(["commit", "-m", message, "--", str(rel)], root)
    return done.returncode == 0


# --- git hooks ---------------------------------------------------------------

HOOKS = {
    "post-merge": "after a pull or merge brings in teammates' observations",
    "post-checkout": "after switching branches or creating a worktree",
}

HOOK_MARKER = "# parallel-memory"


def install_hooks(root: "str | Path | None" = None,
                  directory: "str | Path | None" = None) -> list[Path]:
    """Re-import after any git operation that can bring in new files.

    Appends to an existing hook rather than replacing it, and does nothing if
    the marker is already present.
    """
    root = Path(root) if root is not None else repo_root()
    if root is None:
        raise ValueError("not inside a git repository")
    hooks_dir = Path(_git(["rev-parse", "--path-format=absolute",
                           "--git-path", "hooks"], root).stdout.strip() or root / ".git/hooks")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = str(directory) if directory else DEFAULT_SHARED_DIR

    installed = []
    for name, why in HOOKS.items():
        path = hooks_dir / name
        body = (f"\n{HOOK_MARKER}: {why}\n"
                f"command -v parallel-memory >/dev/null 2>&1 && "
                f"parallel-memory team sync --dir {target} >/dev/null 2>&1 || true\n")
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if HOOK_MARKER in existing:
            continue
        if not existing:
            existing = "#!/bin/sh\n"
        path.write_text(existing.rstrip("\n") + "\n" + body, encoding="utf-8")
        path.chmod(0o755)
        installed.append(path)
    return installed
