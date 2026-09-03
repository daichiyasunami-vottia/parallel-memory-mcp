"""Run subagents from more than one vendor against one shared memory.

Which model runs a subtask should be a per-task choice, not a property of the
harness you happen to be sitting in. This module dispatches a task to a headless
agent CLI — Claude Code, Codex, or anything else you register — and records the
result as an observation.

The part that matters is that every backend writes into the SAME store:

* each subagent is given its own ``PARALLEL_MEMORY_WRITER``, so its findings are
  attributable;
* every subagent inherits one ``PARALLEL_MEMORY_DB``, so nothing is lost when
  several of them finish at once.

That combination is only safe because the store serialises writes. Fanning out
across vendors on a store that does whole-file read-modify-write loses most of
the run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .store import Store


@dataclass(frozen=True)
class Backend:
    """How to run one headless agent CLI."""

    name: str
    command: Sequence[str]
    prompt_as_argument: bool = True
    env: dict[str, str] = field(default_factory=dict)

    def available(self) -> bool:
        return shutil.which(self.command[0]) is not None

    def argv(self, prompt: str) -> list[str]:
        argv = list(self.command)
        if self.prompt_as_argument:
            argv.append(prompt)
        return argv


BACKENDS: dict[str, Backend] = {
    "claude": Backend("claude", ["claude", "-p"]),
    # `codex exec` refuses to run outside a git repository unless told not to
    # check. A subagent is often pointed at a scratch directory, so the check is
    # the caller's to make; drop the flag if you want codex to enforce it.
    "codex": Backend("codex", ["codex", "exec", "--skip-git-repo-check"]),
}


def register(backend: Backend) -> None:
    """Add a backend, so a project can point at its own agent CLI."""
    BACKENDS[backend.name] = backend


def available_backends() -> list[str]:
    return sorted(n for n, b in BACKENDS.items() if b.available())


@dataclass
class AgentResult:
    backend: str
    writer: str
    task: str
    output: str
    exit_code: int
    cwd: str
    observation_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def run_agent(store: Store, task: str, *, backend: str = "claude",
              cwd: "str | Path | None" = None, writer: str | None = None,
              timeout: float | None = 900.0,
              record: bool = True,
              runner: Callable[[list[str], dict[str, str], str], subprocess.CompletedProcess]
              | None = None) -> AgentResult:
    """Run one subtask on the chosen backend and record what it found.

    ``runner`` is injectable so the tests do not have to spend a model call.
    """
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; have {sorted(BACKENDS)}")
    be = BACKENDS[backend]
    work_dir = str(Path(cwd) if cwd else Path.cwd())
    writer_id = writer or f"{backend}-subagent"

    env = {**os.environ, **be.env,
           "PARALLEL_MEMORY_WRITER": writer_id,
           "PARALLEL_MEMORY_DB": str(store.path)}

    argv = be.argv(task)
    if runner is not None:
        proc = runner(argv, env, work_dir)
    else:
        # stdin must be closed: these CLIs read a piped stdin as extra prompt
        # input, so an inherited pipe makes the agent wait on its parent.
        proc = subprocess.run(argv, cwd=work_dir, env=env, capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.returncode and proc.stderr else "")
    result = AgentResult(backend=backend, writer=writer_id, task=task,
                         output=output.strip(), exit_code=proc.returncode, cwd=work_dir)

    if record:
        obs = Store(path=store.path, writer=writer_id).remember(
            task, result.output,
            outcome="useful" if result.ok else "dead_end",
            meta={"origin": "subagent", "backend": backend,
                  "exit_code": str(proc.returncode), "cwd": work_dir},
        )
        result.observation_id = obs["id"]
    return result


def fan_out(store: Store, tasks: Sequence[tuple[str, str]], *,
            cwd: "str | Path | None" = None, max_workers: int | None = None,
            **kwargs: Any) -> list[AgentResult]:
    """Run ``(task, backend)`` pairs at once, all writing into one store.

    Returns results in the order the tasks were given.
    """
    if not tasks:
        return []
    workers = max_workers or min(len(tasks), 8)

    def one(item):
        index, (task, backend) = item
        return index, run_agent(store, task, backend=backend, cwd=cwd,
                                writer=f"{backend}-subagent-{index}", **kwargs)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        done = list(ex.map(one, enumerate(tasks)))
    return [r for _, r in sorted(done, key=lambda p: p[0])]
