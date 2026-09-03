"""Measure graphify's memory-doc writer under concurrency.

graphify writes one markdown file per memory, which is why unrelated writes do
not collide. The filename is `query_<second>_<question-slug-50>.md`, which is
why some related ones do.

Run with graphify importable:  PYTHONPATH=/path/to/graphify python bench_graphify.py
"""
from __future__ import annotations

import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from graphify.ingest import save_query_result

N = 20


def _write(job):
    memdir, question = job
    save_query_result(question, f"answer for {question}", Path(memdir))
    return True


def run(label, questions, executor_cls):
    memdir = tempfile.mkdtemp(prefix="bench-graphify-")
    try:
        with executor_cls(max_workers=min(len(questions), 20)) as ex:
            list(ex.map(_write, [(memdir, q) for q in questions]))
        kept = len(list(Path(memdir).glob("*.md")))
    finally:
        shutil.rmtree(memdir, ignore_errors=True)
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
