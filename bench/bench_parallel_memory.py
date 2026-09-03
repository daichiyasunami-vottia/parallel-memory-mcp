"""The same four cases against this store."""
from __future__ import annotations

import shutil
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from parallel_memory.store import Store

N = 20


def _write(job):
    db, question = job
    Store(path=db, writer="bench").remember(question, f"answer for {question}")
    return True


def run(label, questions, executor_cls):
    tmp = Path(tempfile.mkdtemp(prefix="bench-pm-"))
    db = tmp / "memory.db"
    Store(path=db, writer="bench")
    try:
        with executor_cls(max_workers=min(len(questions), 20)) as ex:
            list(ex.map(_write, [(db, q) for q in questions]))
        kept = Store(path=db).count()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{label:46s} sent={len(questions):3d} kept={kept:3d} lost={len(questions)-kept:3d}")


if __name__ == "__main__":
    prefix = "how does the authentication subsystem resolve tokens"
    run("distinct questions / threads",
        [f"how does module {i} resolve imports" for i in range(N)], ThreadPoolExecutor)
    run("distinct questions / processes",
        [f"how does module {i} resolve imports" for i in range(N)], ProcessPoolExecutor)
    run("identical question / threads",
        ["how does auth work"] * N, ThreadPoolExecutor)
    run("questions sharing a 50-char prefix / threads",
        [f"{prefix} in module {i}" for i in range(N)], ThreadPoolExecutor)
