"""Subagents from different vendors, one store, nothing lost."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from parallel_memory import agents  # noqa: E402
from parallel_memory.store import Store  # noqa: E402


def fake_runner(delay: float = 0.0, code: int = 0):
    """Stand in for a model call: records the env it was handed."""
    seen = []

    def run(argv, env, cwd):
        if delay:
            time.sleep(delay)
        seen.append({"argv": argv, "writer": env.get("PARALLEL_MEMORY_WRITER"),
                     "db": env.get("PARALLEL_MEMORY_DB"), "cwd": cwd})
        return subprocess.CompletedProcess(argv, code, stdout=f"ran {argv[0]}", stderr="")

    run.seen = seen
    return run


def test_backend_argv_shapes():
    assert agents.BACKENDS["claude"].argv("do it") == ["claude", "-p", "do it"]
    assert agents.BACKENDS["codex"].argv("do it") == [
        "codex", "exec", "--skip-git-repo-check", "do it"]


def test_unknown_backend_is_rejected(tmp_path):
    store = Store(path=tmp_path / "m.db", writer="test")
    with pytest.raises(ValueError, match="unknown backend"):
        agents.run_agent(store, "t", backend="nope", runner=fake_runner())


def test_run_records_an_attributable_observation(tmp_path):
    store = Store(path=tmp_path / "m.db", writer="test")
    runner = fake_runner()
    result = agents.run_agent(store, "find the auth gate", backend="codex",
                              cwd=tmp_path, runner=runner)

    assert result.ok and result.backend == "codex"
    assert runner.seen[0]["writer"] == "codex-subagent"
    assert runner.seen[0]["db"] == str(store.path)

    obs = store.all()
    assert len(obs) == 1
    assert obs[0]["writer"] == "codex-subagent"
    assert obs[0]["meta"]["backend"] == "codex"
    assert obs[0]["outcome"] == "useful"


def test_failure_is_recorded_as_a_dead_end(tmp_path):
    store = Store(path=tmp_path / "m.db", writer="test")
    result = agents.run_agent(store, "t", backend="claude",
                              runner=fake_runner(code=3))
    assert not result.ok
    assert store.all()[0]["outcome"] == "dead_end"


def test_fan_out_across_vendors_keeps_every_result(tmp_path):
    """Twelve subagents finishing together; every finding survives, attributed."""
    store = Store(path=tmp_path / "m.db", writer="test")
    tasks = [("the same question", "codex" if i % 2 else "claude") for i in range(12)]
    results = agents.fan_out(store, tasks, cwd=tmp_path, runner=fake_runner(delay=0.02))

    assert len(results) == 12
    assert all(r.ok for r in results)
    assert store.count() == 12
    writers = {w["writer"] for w in store.writers()}
    assert len(writers) == 12
    assert sum(1 for w in writers if w.startswith("codex-")) == 6
    assert sum(1 for w in writers if w.startswith("claude-")) == 6


def test_fan_out_preserves_task_order(tmp_path):
    store = Store(path=tmp_path / "m.db", writer="test")
    tasks = [(f"task {i}", "claude") for i in range(6)]
    results = agents.fan_out(store, tasks, cwd=tmp_path, runner=fake_runner())
    assert [r.task for r in results] == [f"task {i}" for i in range(6)]


def test_register_a_custom_backend(tmp_path):
    agents.register(agents.Backend("myagent", ["echo"]))
    try:
        store = Store(path=tmp_path / "m.db", writer="test")
        runner = fake_runner()
        agents.run_agent(store, "t", backend="myagent", runner=runner)
        assert runner.seen[0]["argv"] == ["echo", "t"]
    finally:
        agents.BACKENDS.pop("myagent", None)
