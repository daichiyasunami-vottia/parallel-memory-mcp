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


# What the loader actually enforces, read off the shipped binaries
# (2.1.267 / 268 / 269, identical in all three):
#
#     lineCount > 200 || byteCount > 25000
#
# with `byteCount: n.length` — a JS string length, i.e. **characters**, not
# UTF-8 bytes. Measuring bytes overstates a CJK index by ~1.4x and reports an
# index that loads fine as truncated; measuring only size misses the line cap,
# which is what a store of short pointer lines hits first.
INDEX_BUDGET_CHARS = 25_000
INDEX_BUDGET_LINES = 200


def target_filename(obs: dict[str, Any]) -> str:
    """The file a memory lives in.

    The original filename is authoritative when the memory came from a store
    (kept in ``meta.file``). Frontmatter ``name`` is human-written and on real
    stores contains slashes, backticks and globs — it is a label, not a path —
    so it is only ever used, sanitised, for a memory that never had a file.
    """
    meta = obs.get("meta") or {}
    if meta.get("file"):
        return Path(meta["file"]).name
    return _slug(meta.get("name") or obs["question"]) + ".md"


def derived_line(obs: dict[str, Any]) -> tuple[str, str]:
    """(target filename, index line) for one machine-derived memory."""
    meta = obs.get("meta") or {}
    target = target_filename(obs)
    title = meta.get("title") or obs["question"]
    hook = meta.get("hook") or _first_sentence(obs["answer"])
    return target, f"- [{title}]({target})" + (f" — {hook}" if hook else "")


def rebuild_index(existing_text: str, observations: Iterable[dict[str, Any]],
                  newly_arrived: set[str], memory_dir: Path) -> tuple[str, dict[str, Any]]:
    """Repair the index without deciding for the human what belongs in it.

    A curated MEMORY.md is deliberately a subset: on real stores roughly one
    memory in five has a top-level line and the rest are reached through hub
    notes. Deriving one line per file inverts that, and at a few hundred files
    produces a 200–400 KB index that the loader truncates — the same invisible
    memory this whole design exists to remove. So the index stays authoritative
    for WHICH memories are listed. This function only:

    * rewrites the line of every entry already present, from frontmatter;
    * removes lines whose target file no longer exists;
    * adds a line for a memory that this sync itself brought in (a worktree or
      stranded store) — never for a local file the curator left unlisted;
    * keeps every other line verbatim (headings, prose, multi-target entries,
      hub notes without frontmatter), in place — structure lives between the
      pointers, and a per-pointer rule cannot see it.

    Anything unlisted is reported, not added.
    """
    by_target = {t: line for t, line in (derived_line(o) for o in observations)}
    out, seen, removed = [], set(), []
    for raw in existing_text.split("\n"):
        m = _INDEX_RE.match(raw.strip())
        if not m:
            out.append(raw)                      # heading / prose / anything else
            continue
        target = m.group("file")
        if not (memory_dir / target).is_file():
            removed.append(target); continue     # dangling pointer
        seen.add(target)
        out.append(by_target.get(target, raw))   # derived → refreshed; curated → verbatim
    added = []
    for target in sorted(newly_arrived):
        if target in by_target and target not in seen:
            out.append(by_target[target]); added.append(target)
    while out and out[-1] == "":
        out.pop()
    if not any(l.strip() for l in out):
        out = [INDEX_HEADER, ""]
    text = "\n".join(out) + "\n"
    unlisted = sorted(t for t in by_target if t not in seen and t not in added)
    trimmed = text.strip()
    chars = len(trimmed)
    lines = trimmed.count("\n") + 1 if trimmed else 0
    return text, {"added": added, "removed": removed, "unlisted": unlisted,
                  "bytes": len(text.encode("utf-8")), "chars": chars, "lines": lines,
                  "over_budget": chars > INDEX_BUDGET_CHARS or lines > INDEX_BUDGET_LINES}


def write_dir(observations: list[dict[str, Any]], memory_dir: "str | Path",
              newly_arrived: set[str] | None = None) -> dict[str, Any]:
    """Write memory files, then repair (not regenerate) the index.

    Only files this store owns are rewritten; a byte-identical file is not
    touched. Returns what happened to the index so a caller can surface
    ``unlisted`` and ``over_budget`` to a human.
    """
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for obs in observations:
        target, _ = derived_line(obs)
        path = memory_dir / target
        content = render_memory_file(obs)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
        written.append(path)
    index_path = memory_dir / INDEX_NAME
    existing = index_path.read_text(encoding="utf-8") if index_path.exists() else INDEX_HEADER + "\n\n"
    text, report = rebuild_index(existing, observations, set(newly_arrived or ()), memory_dir)
    if text != existing:
        index_path.write_text(text, encoding="utf-8")
    report["written"] = len(written)
    return report


# --- the round trip ----------------------------------------------------------

def sync(store, repo_root: "str | Path", *, write_back: bool = True) -> dict[str, Any]:
    """Fold every worktree's Claude memory into the store, then redistribute it.

    A memory is added to a live store's index only if it arrived from
    somewhere else (another worktree, a stranded store). Files a curator left
    unlisted in their own store stay unlisted and are reported instead.
    """
    live = _live_dirs(repo_root)
    stranded = stranded_dirs(repo_root)
    dirs = live + stranded
    known = {(o.get("meta") or {}).get("name") for o in store.all()}
    present: dict[Path, set[str]] = {d: {m["_path"] for m in read_dir(d)} for d in live}
    imported = 0
    for d in dirs:
        for mem in read_dir(d):
            if mem["name"] in known:
                continue
            index = read_index(d).get(mem["_path"], {})
            store.remember(
                mem.get("description") or mem["name"], mem.get("body", ""),
                meta={"origin": "claude", "name": mem["name"], "file": mem["_path"],
                      "type": mem.get("meta_type", "project"),
                      "title": index.get("title", ""), "hook": index.get("hook", ""),
                      "originSessionId": mem.get("meta_originSessionId", "")},
            )
            known.add(mem["name"]); imported += 1

    claude_owned = [o for o in store.all() if (o.get("meta") or {}).get("origin") == "claude"]
    all_targets = {derived_line(o)[0] for o in claude_owned}
    reports = {}
    if write_back:
        for d in live:
            arrived = all_targets - present.get(d, set())      # new to THIS store
            reports[str(d)] = write_dir(claude_owned, d, newly_arrived=arrived)
    return {
        "repo_root": str(repo_root),
        "memory_dirs": [str(d) for d in live],
        "stranded_dirs": [str(d) for d in stranded],
        "imported": imported,
        "unified": len(claude_owned),
        "index": reports,
    }


def _slug(s: str) -> str:
    return re.sub(r"[^\w]+", "_", s.strip().lower())[:60].strip("_") or "memory"


def _first_sentence(body: str, limit: int = 120) -> str:
    """One line for the index. Always capped: a hook is a hint, not the memory."""
    text = " ".join(body.strip().split())
    for sep, end in (("。", "。"), (". ", ".")):
        if sep in text:
            text = text.split(sep)[0] + end
            break
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
