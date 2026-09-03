"""The cases that make other stores lose data must not lose data here."""
from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from parallel_memory.store import Store  # noqa: E402

N = 20


def _write(job):
    db, question, answer, writer = job
    Store(path=db, writer=writer).remember(question, answer)
    return True


def test_distinct_questions_in_threads(tmp_path):
    db = tmp_path / "m.db"
    Store(path=db, writer="setup")
    jobs = [(db, f"question {i}", f"answer {i}", "agent-a") for i in range(N)]
    with ThreadPoolExecutor(max_workers=N) as ex:
        assert all(ex.map(_write, jobs))
    assert Store(path=db).count() == N


def test_identical_question_same_instant(tmp_path):
    """server-memory keeps 1 of 20 here; graphify's memory docs keep 1 of 20."""
    db = tmp_path / "m.db"
    Store(path=db, writer="setup")
    jobs = [(db, "how does auth work", f"answer {i}", "agent-a") for i in range(N)]
    with ThreadPoolExecutor(max_workers=N) as ex:
        assert all(ex.map(_write, jobs))
    assert Store(path=db).count() == N


def test_questions_sharing_a_long_prefix(tmp_path):
    """graphify truncates the filename slug at 50 chars, so these collide there."""
    db = tmp_path / "m.db"
    Store(path=db, writer="setup")
    prefix = "how does the authentication subsystem resolve tokens"
    jobs = [(db, f"{prefix} in module {i}", f"answer {i}", "agent-a") for i in range(N)]
    with ThreadPoolExecutor(max_workers=N) as ex:
        assert all(ex.map(_write, jobs))
    assert Store(path=db).count() == N


def test_separate_processes(tmp_path):
    """Distinct OS processes, one file: the store serialises, not the process."""
    db = tmp_path / "m.db"
    Store(path=db, writer="setup")
    jobs = [(db, f"question {i}", f"answer {i}", f"worktree-{i % 4}") for i in range(N)]
    with ProcessPoolExecutor(max_workers=8) as ex:
        assert all(ex.map(_write, jobs))
    store = Store(path=db)
    assert store.count() == N
    assert len(store.writers()) == 4


def test_every_observation_keeps_its_own_answer(tmp_path):
    """Survival is not enough: the surviving rows must not be duplicates."""
    db = tmp_path / "m.db"
    Store(path=db, writer="setup")
    jobs = [(db, "same question", f"answer {i}", "agent-a") for i in range(N)]
    with ThreadPoolExecutor(max_workers=N) as ex:
        list(ex.map(_write, jobs))
    answers = {o["answer"] for o in Store(path=db).all()}
    assert answers == {f"answer {i}" for i in range(N)}
