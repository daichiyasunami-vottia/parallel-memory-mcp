"""Bridge to Claude Code's auto-memory, across every worktree of a repository.

Claude Code keeps its own memory under
``~/.claude/projects/<cwd-slug>/memory/`` — one markdown file per memory with
YAML frontmatter, plus a ``MEMORY.md`` index that is loaded into context each
session. The directory is keyed by the working directory, so a repository and
each of its linked worktrees get SEPARATE stores: parallelising the work
fragments the memory.

This module treats that layout as a peripheral. It reads every slug belonging
to one repository (the root plus ``<repo>/.worktrees/*``), folds them into the
single store, and writes the union back to each — so what one worktree learned
is visible from all of them.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
INDEX_NAME = "MEMORY.md"
INDEX_HEADER = "# Memory Index"
KNOWN_TYPES = ("user", "feedback", "project", "reference")

_SCALAR_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][\w-]*):\s*(?P<val>.*?)\s*$")
_INDEX_RE = re.compile(r"^-\s*\[(?P<title>[^\]]*)\]\((?P<file>[^)]+)\)\s*(?:—\s*(?P<hook>.*))?$")


def project_slug(path: "str | Path") -> str:
    """Claude's directory key for a working directory: ``/`` and ``.`` become ``-``."""
    return str(Path(path)).replace("/", "-").replace(".", "-")


def stranded_dirs(repo_root: "str | Path") -> list[Path]:
    """Memory directories for worktrees of this repo that no longer exist.

    Deleting a worktree removes the checkout, not the store Claude Code keyed to
    its cwd. Transcript cleanup does not touch `memory/` either, so those files
    stay on disk forever: never loaded, never collected. Walking the live
    filesystem cannot find them — the directory they were named after is gone —
    so they are discovered from the project keys instead.

    A hidden subdirectory of the repo slugs to ``<repo slug>--<name>``, because
    the ``/`` and the ``.`` each become ``-``. Requiring that doubled dash is
    what keeps a sibling checkout (``<repo>-1467``) from matching.
    """
    prefix = project_slug(repo_root) + "--"
    if not CLAUDE_PROJECTS.is_dir():
        return []
    live = {d.resolve() for d in _live_dirs(repo_root)}
    found = []
    for project in sorted(CLAUDE_PROJECTS.iterdir()):
        if not project.is_dir() or not project.name.startswith(prefix):
            continue
        memory = project / "memory"
        if memory.is_dir() and memory.resolve() not in live:
            found.append(memory)
    return found


def _live_dirs(repo_root: "str | Path") -> list[Path]:
    """Memory directories for the repo root and its current worktrees."""
    repo_root = Path(repo_root)
    candidates = [repo_root]
    worktrees = repo_root / ".worktrees"
    if worktrees.is_dir():
        candidates += sorted(p for p in worktrees.iterdir() if p.is_dir())
    dirs = [CLAUDE_PROJECTS / project_slug(c) / "memory" for c in candidates]
    return [d for d in dirs if d.is_dir()]


def memory_dirs(repo_root: "str | Path", *,
                include_stranded: bool = True) -> list[Path]:
    """Every Claude memory directory belonging to one repository.

    The repository root first, then its current worktrees, then any store left
    behind by a worktree that has since been deleted.
    """
    dirs = _live_dirs(repo_root)
    if include_stranded:
        dirs += stranded_dirs(repo_root)
    return dirs


# --- reading -----------------------------------------------------------------

def parse_memory_file(text: str) -> dict[str, Any] | None:
    """Parse one Claude memory file into ``{name, description, type, body}``."""
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fields: dict[str, Any] = {}
    in_metadata = False
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
        m = _SCALAR_RE.match(line)
        if not m:
            continue
        indent, key = m.group("indent"), m.group("key")
        val = m.group("val").strip().strip('"')
        # `metadata:` opens a nested block; its children are indented.
        if key == "metadata" and not val:
            in_metadata = True
            continue
        if indent:
            if in_metadata and key in ("type", "node_type", "originSessionId"):
                fields[f"meta_{key}"] = val
            continue
        in_metadata = False
        if key in ("name", "description"):
            fields[key] = val
    if end is None or "name" not in fields:
        return None
    fields["body"] = "\n".join(lines[end + 1:]).strip()
    return fields


def read_dir(memory_dir: "str | Path") -> list[dict[str, Any]]:
    """Every memory in one Claude memory directory (the index itself excluded)."""
    memory_dir = Path(memory_dir)
    if not memory_dir.is_dir():
        return []
    out = []
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == INDEX_NAME:
            continue
        try:
            parsed = parse_memory_file(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if parsed is None:
            continue
        parsed["_path"] = path.name
        parsed["_dir"] = str(memory_dir)
        out.append(parsed)
    return out


def read_index(memory_dir: "str | Path") -> dict[str, dict[str, str]]:
    """The ``MEMORY.md`` index, keyed by the file each line points at."""
    path = Path(memory_dir) / INDEX_NAME
    if not path.is_file():
        return {}
    entries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _INDEX_RE.match(line.strip())
        if m:
            entries[m.group("file")] = {
                "title": m.group("title"),
                "hook": (m.group("hook") or "").strip(),
            }
    return entries


# --- writing -----------------------------------------------------------------

def render_memory_file(obs: dict[str, Any]) -> str:
    meta = obs.get("meta") or {}
    name = meta.get("name") or _slug(obs["question"])
    mem_type = meta.get("type") if meta.get("type") in KNOWN_TYPES else "project"
    lines = [
        "---",
        f"name: {name}",
        f"description: {obs['question']}",
        "metadata:",
        "  node_type: memory",
        f"  type: {mem_type}",
    ]
    if meta.get("originSessionId"):
        lines.append(f"  originSessionId: {meta['originSessionId']}")
    lines += ["---", "", obs["answer"].strip(), ""]
    return "\n".join(lines)


def render_index(observations: Iterable[dict[str, Any]]) -> str:
    lines = [INDEX_HEADER, ""]
    for obs in observations:
        meta = obs.get("meta") or {}
        name = meta.get("name") or _slug(obs["question"])
        title = meta.get("title") or obs["question"]
        hook = meta.get("hook") or _first_sentence(obs["answer"])
        lines.append(f"- [{title}]({name}.md)" + (f" — {hook}" if hook else ""))
    return "\n".join(lines) + "\n"


def write_dir(observations: list[dict[str, Any]], memory_dir: "str | Path") -> list[Path]:
    """Write the union into one Claude memory directory, index included.

    Only files this store owns are rewritten; anything else in the directory is
    left alone, and a byte-identical file is not touched.
    """
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for obs in observations:
        meta = obs.get("meta") or {}
        name = meta.get("name") or _slug(obs["question"])
        path = memory_dir / f"{name}.md"
        content = render_memory_file(obs)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
        written.append(path)
    index_path = memory_dir / INDEX_NAME
    index = render_index(observations)
    if not index_path.exists() or index_path.read_text(encoding="utf-8") != index:
        index_path.write_text(index, encoding="utf-8")
    return written


# --- the round trip ----------------------------------------------------------

def sync(store, repo_root: "str | Path", *, write_back: bool = True) -> dict[str, Any]:
    """Fold every worktree's Claude memory into the store, then redistribute it.

    Returns what happened, per directory. ``write_back=False`` imports only.
    """
    live = _live_dirs(repo_root)
    stranded = stranded_dirs(repo_root)
    dirs = live + stranded
    known = {(o.get("meta") or {}).get("name") for o in store.all()}
    imported = 0
    for d in dirs:
        for mem in read_dir(d):
            if mem["name"] in known:
                continue
            index = read_index(d).get(mem["_path"], {})
            store.remember(
                mem.get("description") or mem["name"],
                mem.get("body", ""),
                meta={
                    "origin": "claude",
                    "name": mem["name"],
                    "type": mem.get("meta_type", "project"),
                    "title": index.get("title", ""),
                    "hook": index.get("hook", ""),
                    "originSessionId": mem.get("meta_originSessionId", ""),
                },
            )
            known.add(mem["name"])
            imported += 1

    claude_owned = [o for o in store.all() if (o.get("meta") or {}).get("origin") == "claude"]
    # Only live stores are written back. Writing into a store no session will
    # ever open again would just move the files nobody reads.
    distributed = {}
    if write_back:
        for d in live:
            distributed[str(d)] = len(write_dir(claude_owned, d))
    return {
        "repo_root": str(repo_root),
        "memory_dirs": [str(d) for d in live],
        "stranded_dirs": [str(d) for d in stranded],
        "imported": imported,
        "unified": len(claude_owned),
        "written": distributed,
    }


def _slug(s: str) -> str:
    return re.sub(r"[^\w]+", "_", s.strip().lower())[:60].strip("_") or "memory"


def _first_sentence(body: str) -> str:
    text = " ".join(body.strip().split())
    for sep in ("。", ". "):
        if sep in text:
            return text.split(sep)[0] + ("。" if sep == "。" else "")
    return text[:120]
