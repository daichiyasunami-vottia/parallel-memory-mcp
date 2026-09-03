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
