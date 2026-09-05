# AGENTS.md

Instructions for agents working in this repository. Claude Code, Codex, Cursor
and Gemini CLI all read this file (Claude Code additionally reads `CLAUDE.md`,
which points here).

## What this repository is

A memory MCP server that does not lose writes when several agents run at once,
plus the benchmark that shows what the alternatives lose. Correctness here means
*concurrency* correctness, so the tests are the product as much as the server is.

## Norms vs observations

This repository draws a line that the code also implements, and contributions
should respect it:

- **Norms** — invariants that must hold, where being wrong is harmful. Few,
  slow to rot, worth reviewing. They live in the repository, in this file and in
  path-scoped rule files. Do not put them in the memory store.
- **Observations** — what an agent looked up and what it found. Many,
  perishable, worth capturing the instant they are learned, not worth reviewing.
  They go in the store, via the `remember` tool.

If you are about to write a durable rule into the memory store, it is a norm:
put it here in a pull request instead.

## Working agreements

- **Run the tests before proposing a change.** `pytest tests -q`. Every test
  either pins a concurrency guarantee or an interop format; a failure is a real
  regression, not flakiness to retry.
- **Never derive an identity from a timestamp or from content.** That is the
  exact defect this repository exists to demonstrate. New records get a UUID.
- **Do not add a lock to work around a lost update.** Fix the identity or the
  transaction boundary. A lock that papers over a read-modify-write is how the
  reference implementation got where it is.
- **Keep writes inside `Store._write`.** It opens `BEGIN IMMEDIATE` so writers
  queue instead of failing at COMMIT. A bare `INSERT` outside it will pass tests
  on an idle machine and lose data under load.
- **Interop formats are contracts.** `graphify_sync` and `claude_sync` emit
  files that other tools parse. Changing a field name or the escaping is a
  breaking change; the round-trip tests must be updated deliberately, never
  loosened to make a diff pass.
- **`MEMORY.md` is repaired, never regenerated.** It is a curated subset under a
  25 KB load cap. Refresh existing lines, drop dangling ones, add lines only for
  memories this sync brought in from elsewhere, keep everything else in place.
  Deriving one line per file inverts the curation and truncates the tail.
- **A memory's file is where it was found, never derived from `name`.** On real
  stores `name` ≠ filename in 123 of 187 files and can contain path characters.
- **Nothing in `agent-memory/` is ever rewritten or renamed.** The directory is
  append-only and that is what removes the need for a merge driver. A change that
  makes two writers able to produce one filename reintroduces exactly the defect
  this repository documents in other tools.
- **Coordination state never goes in the store or in `agent-memory/`.** A lease
  changes hands; the store is append-only and the export is committed. Leases
  live in git's own worktree lock (`worktree_lease.py`). Putting "who holds
  what" into the observation store would make two holders look like two facts.
- **Never write into another product's database.** Read it `mode=ro&immutable=1`
  or not at all. `codex_sync` is import-only for this reason, and a test asserts
  the file is byte-identical afterwards. Where a tool's store is documented files
  (`claude_sync`, `graphify_sync`), writing is fine — that is a published format,
  not an internal one.
- **Benchmarks measure other people's software.** Keep `bench/` honest: report
  what the code does, cite the version, and never tune a case to flatter this
  repository.

## Layout

```
src/parallel_memory/
  store.py           SQLite store — WAL, BEGIN IMMEDIATE, UUID identity
  writer.py          writer identity, and the git-common-dir store location
  graphify_sync.py   graphify memory-doc export/import
  claude_sync.py     Claude Code auto-memory bridge, unified across worktrees
  team.py            git-based sharing: export, import, commit, hooks
  worktree_lease.py  who holds which worktree — git worktree lock + expiry
  provenance.py      branch / head / blob hashes an observation was made against; staleness on recall
  codex_sync.py      Codex memory reader (read-only) + the markdown surface
  agents.py          multi-vendor subagent runner (claude, codex, ...)
  server.py          MCP tools, stdio and streamable HTTP
tests/               concurrency, worktree resolution, both interop round-trips
bench/               the comparison against server-memory and graphify
```

## Environment

| variable | meaning |
|---|---|
| `PARALLEL_MEMORY_DB` | store path; otherwise `<repo>/.parallel-memory/memory.db` |
| `PARALLEL_MEMORY_WRITER` | writer identity; otherwise host + checkout directory |

The store is anchored with `git rev-parse --git-common-dir`, so every worktree
under `<repo>/.worktrees/` opens the same file. Do not replace that with
`--git-dir`: in a linked worktree it resolves to `.git/worktrees/<name>` and the
memory forks per worktree, which is the bug this repository is about.
