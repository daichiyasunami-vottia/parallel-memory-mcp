"""Claude Code's per-worktree memory must end up unified, and round-trip losslessly."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from parallel_memory import claude_sync as cs  # noqa: E402
from parallel_memory.store import Store  # noqa: E402

MEMORY = """---
name: {name}
description: {desc}
metadata: 
  node_type: memory
  type: {mtype}
  originSessionId: 0000-{name}
---

{body}

**Why:** because.
**How to apply:** like this.
"""


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A repo with two worktrees, and a fake ~/.claude/projects for each slug."""
    projects = tmp_path / "claude-projects"
    projects.mkdir()
    monkeypatch.setattr(cs, "CLAUDE_PROJECTS", projects)

    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "README.md").write_text("x\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    for name in ("a", "b"):
        _git("worktree", "add", "-q", "-b", name, str(root / ".worktrees" / name), cwd=root)
    return root, projects


def _memdir(projects, path):
    d = projects / cs.project_slug(path) / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_parses_indented_metadata():
    parsed = cs.parse_memory_file(MEMORY.format(
        name="feedback_x", desc="a one-line summary", mtype="feedback", body="the fact"))
    assert parsed["name"] == "feedback_x"
    assert parsed["description"] == "a one-line summary"
    assert parsed["meta_type"] == "feedback"
    assert parsed["meta_originSessionId"] == "0000-feedback_x"
    assert parsed["body"].startswith("the fact")


def test_memory_dirs_cover_root_and_worktrees(world):
    root, projects = world
    for p in (root, root / ".worktrees" / "a", root / ".worktrees" / "b"):
        _memdir(projects, p)
    dirs = cs.memory_dirs(root)
    assert len(dirs) == 3
    assert dirs[0] == projects / cs.project_slug(root) / "memory"


def test_worktree_memories_are_unified(world, tmp_path):
    """What one worktree learned becomes visible from all of them."""
    root, projects = world
    root_dir = _memdir(projects, root)
    a_dir = _memdir(projects, root / ".worktrees" / "a")
    b_dir = _memdir(projects, root / ".worktrees" / "b")

    (root_dir / "from_root.md").write_text(MEMORY.format(
        name="from_root", desc="learned on main", mtype="project", body="root body"))
    (a_dir / "from_a.md").write_text(MEMORY.format(
        name="from_a", desc="learned in worktree a", mtype="feedback", body="a body"))
    (b_dir / "from_b.md").write_text(MEMORY.format(
        name="from_b", desc="learned in worktree b", mtype="reference", body="b body"))

    store = Store(path=tmp_path / "m.db", writer="test")
    result = cs.sync(store, root)

    assert result["imported"] == 3
    assert result["unified"] == 3
    for d in (root_dir, a_dir, b_dir):
        names = {p.stem for p in d.glob("*.md")} - {"MEMORY"}
        assert names == {"from_root", "from_a", "from_b"}, f"{d} was not unified"
        index = (d / "MEMORY.md").read_text(encoding="utf-8")
        assert index.startswith("# Memory Index")
        assert index.count("\n- ") == 3


def test_round_trip_preserves_type_and_session(world, tmp_path):
    root, projects = world
    d = _memdir(projects, root)
    (d / "feedback_x.md").write_text(MEMORY.format(
        name="feedback_x", desc="a one-line summary", mtype="feedback", body="the fact"))

    store = Store(path=tmp_path / "m.db", writer="test")
    cs.sync(store, root)

    reparsed = cs.parse_memory_file((d / "feedback_x.md").read_text(encoding="utf-8"))
    assert reparsed["name"] == "feedback_x"
    assert reparsed["description"] == "a one-line summary"
    assert reparsed["meta_type"] == "feedback"
    assert reparsed["meta_originSessionId"] == "0000-feedback_x"
    assert "the fact" in reparsed["body"]


def test_sync_is_idempotent(world, tmp_path):
    root, projects = world
    d = _memdir(projects, root)
    (d / "feedback_x.md").write_text(MEMORY.format(
        name="feedback_x", desc="s", mtype="feedback", body="b"))

    store = Store(path=tmp_path / "m.db", writer="test")
    cs.sync(store, root)
    mtimes = {p: p.stat().st_mtime_ns for p in d.glob("*.md")}
    second = cs.sync(store, root)

    assert second["imported"] == 0, "a second sync re-imported what it had already stored"
    assert {p: p.stat().st_mtime_ns for p in d.glob("*.md")} == mtimes


def test_foreign_files_are_left_alone(world, tmp_path):
    root, projects = world
    d = _memdir(projects, root)
    (d / "feedback_x.md").write_text(MEMORY.format(
        name="feedback_x", desc="s", mtype="feedback", body="b"))
    (d / "notes.md").write_text("no frontmatter here\n")

    store = Store(path=tmp_path / "m.db", writer="test")
    cs.sync(store, root)
    assert (d / "notes.md").read_text(encoding="utf-8") == "no frontmatter here\n"


# --- stores left behind by deleted worktrees --------------------------------

def test_finds_a_store_whose_worktree_is_gone(world, tmp_path):
    """Deleting a worktree removes the checkout, not the memory keyed to it."""
    root, projects = world
    wt = root / ".worktrees" / "a"
    stranded = _memdir(projects, wt)
    (stranded / "from_a.md").write_text(MEMORY.format(
        name="from_a", desc="learned in worktree a", mtype="feedback", body="a body"))

    # the worktree goes away; its project key does not
    _git("worktree", "remove", "--force", str(wt), cwd=root)
    assert not wt.exists()

    assert cs._live_dirs(root) == [] or stranded not in cs._live_dirs(root)
    assert stranded in cs.stranded_dirs(root)
    assert stranded in cs.memory_dirs(root)


def test_stranded_memories_are_imported(world, tmp_path):
    root, projects = world
    root_dir = _memdir(projects, root)
    wt = root / ".worktrees" / "a"
    stranded = _memdir(projects, wt)
    (stranded / "from_a.md").write_text(MEMORY.format(
        name="from_a", desc="learned in worktree a", mtype="feedback", body="a body"))
    _git("worktree", "remove", "--force", str(wt), cwd=root)

    store = Store(path=tmp_path / "m.db", writer="test")
    result = cs.sync(store, root)

    assert result["imported"] == 1
    assert result["stranded_dirs"] == [str(stranded)]
    assert {o["meta"]["name"] for o in store.all()} == {"from_a"}
    # and it is now reachable from the live store
    assert (root_dir / "from_a.md").exists()


def test_stranded_stores_are_not_written_back_to(world, tmp_path):
    """Writing into a store nothing will open again just moves unread files."""
    root, projects = world
    _memdir(projects, root)
    wt = root / ".worktrees" / "a"
    stranded = _memdir(projects, wt)
    (stranded / "from_a.md").write_text(MEMORY.format(
        name="from_a", desc="s", mtype="feedback", body="b"))
    _git("worktree", "remove", "--force", str(wt), cwd=root)

    store = Store(path=tmp_path / "m.db", writer="test")
    store.remember("unrelated", "x", meta={"origin": "claude", "name": "unrelated"})
    cs.sync(store, root)

    assert not (stranded / "unrelated.md").exists()
    assert str(stranded) not in cs.sync(store, root)["memory_dirs"]


def test_a_sibling_checkout_is_not_mistaken_for_a_worktree(world, tmp_path):
    """<repo>-1467 slugs to <repo slug>-1467, which must not match."""
    root, projects = world
    sibling = root.parent / (root.name + "-1467")
    sibling.mkdir()
    d = _memdir(projects, sibling)
    (d / "other.md").write_text(MEMORY.format(
        name="other", desc="s", mtype="feedback", body="b"))

    assert d not in cs.stranded_dirs(root)
    assert d not in cs.memory_dirs(root)



def test_curated_index_lines_survive_regeneration(world, tmp_path):
    """A hand-written hub note has no frontmatter; its index line must be kept."""
    root, projects = world
    d = _memdir(projects, root)
    (d / "feedback_x.md").write_text(MEMORY.format(
        name="feedback_x", desc="a fact", mtype="feedback", body="the fact"))
    (d / "hub-evidence.md").write_text("# Evidence hub\n\nGroups a dozen memories.\n")
    (d / "MEMORY.md").write_text(
        "# Memory Index\n\n"
        "- [a fact](feedback_x.md) — hook\n"
        "- [Evidence hub](hub-evidence.md) — start here for anything about proof\n")

    store = Store(path=tmp_path / "m.db", writer="test")
    cs.sync(store, root)

    index = (d / "MEMORY.md").read_text(encoding="utf-8")
    assert "(feedback_x.md)" in index
    assert "- [Evidence hub](hub-evidence.md) — start here for anything about proof" in index, (
        "a curated line whose target exists was dropped by regeneration"
    )
    # and a line whose target is gone is still removed
    (d / "MEMORY.md").write_text(index + "- [ghost](ghost.md) — no such file\n")
    cs.sync(store, root)
    assert "ghost.md" not in (d / "MEMORY.md").read_text(encoding="utf-8")
