# parallel-memory-mcp

A memory MCP server for agents that run in parallel — and a benchmark showing
what the alternatives lose when they do.

Writes are serialised by SQLite, not by luck. Observations are mirrored into
[graphify](https://github.com/Graphify-Labs/graphify) memory docs, so
`graphify --update` and `graphify reflect` read them with no changes on their side.

## The measurement

Twenty writes issued at once. `kept` is how many survived; every store reported
success for all twenty, and none raised.

| case | `@modelcontextprotocol/server-memory` | graphify memory docs | this repo |
|---|---|---|---|
| 20 writes, strictly one at a time | 20 / 20 | — | 20 / 20 |
| 20 **distinct** writes at once | **1 / 20** | 20 / 20 | 20 / 20 |
| same question, same instant | **1 / 20** | **1 / 20** | 20 / 20 |
| questions sharing a 50-char prefix | — | **1 / 20** | 20 / 20 |
| error responses / raised exceptions | 0 | 0 | 0 |

Reproduce it yourself — see [`bench/`](bench/).

### Why each number comes out that way

**server-memory** (v2026.8.31) loses almost everything the moment writes
overlap. Every mutation is a whole-graph read-modify-write:

```js
async createEntities(entities) {
    const graph = await this.loadGraph();   // read the entire file
    graph.entities.push(...newEntities);
    await this.saveGraph(graph);            // write the entire file back
}
```

There is no lock anywhere in the file. The write itself is atomic — a temp file
plus `rename(2)`, and the source says so — but atomicity is not isolation. Two
overlapping calls both load the same graph and the second one's save erases the
first one's work. Nothing errors, because from each caller's side nothing went
wrong. It is also stdio-only, so "just run one shared process" is not available
as a workaround, and even a single agent issuing two tool calls concurrently
hits it.

This is a reference implementation of the protocol. Read it as one.

**graphify** does much better, by construction: one markdown file per memory
under `graphify-out/memory/`, folded on read. Unrelated writes touch different
paths, so they cannot interfere — no lock required. What remains is the
filename:

```python
slug = re.sub(r"[^\w]", "_", question.lower())[:50].strip("_")
filename = f"query_{now.strftime('%Y%m%d_%H%M%S')}_{slug}.md"
...
out_path.write_text(content, encoding="utf-8")
```

One-second granularity, a slug truncated at 50 characters, no writer in the
name, and a plain overwrite. Two agents that reach the same question in the same
second collide — and so do questions that merely *begin* the same way, which is
the common case when several agents sweep one subsystem
(`"how does the authentication subsystem resolve tokens in module 7"`).

**This repo** assigns every observation a UUID and lets SQLite order the writes.
Identity is never derived from content or from the clock, so nothing can collide.

## Design

Three decisions do the work:

1. **WAL + `BEGIN IMMEDIATE` + `busy_timeout`.** The write lock is taken when
   the transaction opens rather than at first write, so concurrent writers queue
   instead of failing at COMMIT. Readers never block.
2. **Assigned identity.** Every observation gets a UUID. A store that derives an
   id from timestamp and content silently merges two observations that happen to
   collide; this one cannot.
3. **The store serialises, not the process.** N stdio servers across N worktrees
   share one SQLite file. Safety does not depend on there being a single process,
   which is why stdio is still offered.

### Worktrees share one store

Per-`cwd` state forks memory exactly when you parallelise. The store is anchored
with `git rev-parse --git-common-dir`, which points at the original `.git` from
inside a linked worktree, so with the `<repo>/.worktrees/<name>` layout:

```
repo/                      -> repo/.parallel-memory/memory.db
repo/.worktrees/feature-a  -> repo/.parallel-memory/memory.db   (same file)
repo/.worktrees/feature-b  -> repo/.parallel-memory/memory.db   (same file)
```

Each worktree still gets its own `writer` identity, so you can tell contributions
apart without splitting the store. (`--git-dir` would fork it: in a linked
worktree it resolves to `.git/worktrees/<name>`.)

## Install

```bash
pip install -e ".[mcp]"
```

Register it with an MCP client:

```json
{
  "mcpServers": {
    "parallel-memory": {
      "type": "stdio",
      "command": "parallel-memory",
      "args": ["serve"]
    }
  }
}
```

Or run one shared server for a team or a CI fleet:

```bash
parallel-memory serve --http --port 8931
```

## Tools

| tool | what it does |
|---|---|
| `remember` | record an observation (question, answer, optional `outcome` / `correction` / `source_nodes`) |
| `recall` | search observations across every writer |
| `forget` | delete one observation by id |
| `writers` | list contributing writers with counts |
| `sync_claude` | fold Claude Code's per-worktree auto-memory into the store and write the union back to every worktree |
| `sync_codex` | import Codex's distilled memories (read-only), optionally writing the markdown surface |
| `sync_team` | import observations teammates committed, then export this store's own |
| `sync_graphify` | absorb existing `graphify-out/memory/*.md`, then mirror the store back into it |

`outcome` is one of `useful` / `dead_end` / `corrected` — the same vocabulary
`graphify reflect` aggregates.

## graphify interop

`sync_graphify` writes graphify's own memory-doc format: YAML frontmatter with
`type` / `date` / `question` / `contributor` / `outcome` / `correction` /
`source_nodes`, then `## Answer` and `## Outcome` sections. The `contributor`
field carries the writer id, so a team graph shows who found what.

The test suite asserts this against **graphify's own parser** when graphify is
importable, including quotes, tabs, newlines, U+2028/U+2029 and non-ASCII
questions:

```bash
PYTHONPATH=/path/to/graphify pytest tests -q
```

Without graphify installed the suite still runs, against a vendored copy of the
same grammar.

## Claude Code auto-memory

Claude Code keeps its own memory under `~/.claude/projects/<cwd-slug>/memory/` —
one markdown file per memory plus a `MEMORY.md` index loaded into context each
session. The directory is keyed by the working directory, so the repository and
each of its worktrees get separate stores: parallelising the work fragments the
memory, silently, and the symptom ("it does not remember") is indistinguishable
from the model simply not recalling.

`sync_claude` reads every slug belonging to one repository — the root plus
`<repo>/.worktrees/*` — folds them into the store, and writes the union back to
each:

```bash
parallel-memory sync-claude --dry-run   # import only, change nothing
parallel-memory sync-claude             # unify across every worktree
```

Frontmatter (`name`, `description`, `metadata.type`, `originSessionId`) survives
the round trip, the `MEMORY.md` index is regenerated, and files without
recognisable frontmatter are left untouched.

**Stores left behind by deleted worktrees are picked up too.** Removing a
worktree removes the checkout, not the memory directory Claude Code keyed to its
cwd — and transcript cleanup (`cleanupPeriodDays`, 30 by default) deletes
`*.jsonl` while leaving `memory/` alone. Those files then sit on disk
permanently: never loaded, never collected. Walking the live filesystem cannot
find them, because the directory they were named after is gone, so they are
discovered from the project keys instead:

```
<repo>/.worktrees/a   ->  ~/.claude/projects/<repo slug>--worktrees-a/memory/
```

The doubled dash comes from `/.` and is what keeps a sibling checkout
(`<repo>-1467`) from matching. Stranded stores are imported; they are never
written back to, since nothing will open them again.

## Subagents, from more than one vendor

Which model runs a subtask should be a per-task choice. Backends are just a
command plus how the prompt is passed:

```bash
parallel-memory backends                       # what is installed here
parallel-memory run --agent codex  "audit the token refresh path"
parallel-memory fanout "summarise how auth works"   # every backend at once
```

```python
from parallel_memory.agents import Backend, register
register(Backend("my-agent", ["my-agent-cli", "--headless"]))
```

Every subagent inherits one `PARALLEL_MEMORY_DB` and is given its own
`PARALLEL_MEMORY_WRITER`, so findings are attributable and none are lost when
several finish together:

```console
$ parallel-memory fanout "Reply with one word: who made you?"
[
  { "backend": "claude", "writer": "claude-subagent-0", "output": "Anthropic" },
  { "backend": "codex",  "writer": "codex-subagent-1",  "output": "OpenAI"    }
]
```

Both answers are stored, and both survive the export to graphify memory docs —
the same question at nearly the same second, which graphify's own writer would
collapse into one file.

Fanning out like this across a store that does whole-file read-modify-write
loses most of the run.

### Codex

```bash
codex mcp add parallel-memory -- /path/to/.venv/bin/parallel-memory serve
codex mcp list
```

Codex keeps its own distilled memories in `$CODEX_HOME/memories_*.sqlite`
(`stage1_outputs`: one row per thread), and runs its background work through a
`jobs` table with ownership tokens and leases.

```bash
parallel-memory sync-codex                      # import, read-only
parallel-memory export-agents --out .parallel-memory/CODEX_MEMORY.md
```

**Why this direction is asymmetric.** Claude Code's memory is synced both ways
because its store *is* a directory of markdown files with a documented shape —
writing there is writing the same kind of file a person would. Codex's is
another product's internal database, versioned by its own `_sqlx_migrations`
table and guarded by a lease protocol this process is not part of. Inserting
rows would mean claiming a thread id we do not own and racing a worker holding
the lease — the exact class of bug this repository is about — and a migration on
Codex's side could drop whatever was written. So it is opened `mode=ro&immutable=1`
(no locking, so a running Codex is neither blocked nor disturbed), and the
write-back surface is a markdown file you reference from your `AGENTS.md`.

A test asserts the database is byte-identical after a sync.

`codex exec` refuses to run outside a git repository, so the bundled backend
passes `--skip-git-repo-check`; drop it if you want that check enforced. Its
stdin is closed, because these CLIs read a piped stdin as extra prompt input.

Working agreements for agents live in [AGENTS.md](AGENTS.md), which Codex,
Cursor and Gemini CLI read directly and `CLAUDE.md` points at.

## Sharing with a team

SQLite cannot be merged, so the store stays local and ignored. What gets
committed is the text export — one markdown file per observation, named with the
writer and the observation's UUID:

```
agent-memory/
  query_20260903T051507_alice_9c7e2564_how_does_auth_work.md
  query_20260903T051509_bob_1f0ab233_how_does_auth_work.md
```

**No merge driver is needed, because no path is ever written by two people.**
The writer and a UUID are both in the filename, so a pull only ever adds files;
git never has to reconcile the contents of one. Nothing is rewritten in place
either, so the directory is append-only.

```bash
parallel-memory team sync                 # absorb teammates', publish ours
parallel-memory team sync --commit        # and commit the result
parallel-memory team hooks                # re-import automatically after a pull
```

`team hooks` appends to `post-merge` and `post-checkout` — it does not replace an
existing hook, and it is a no-op if already installed. Commit `agent-memory/`;
keep `.parallel-memory/` (the SQLite cache) ignored.

The alternative is one shared server, which needs a key on an open port:

```bash
parallel-memory serve --http --host 0.0.0.0 --port 8931 --api-key "$SECRET"
```

Clients send `Authorization: Bearer $SECRET` or `X-API-Key: $SECRET`. The check
is a constant-time compare in ASGI, before the MCP session starts.

Which to pick is the tradeoff this repository is about: the git route is
reviewable and needs no operations, the server route gives one authoritative
copy and real queries.

## Who holds which worktree

Observations and coordination are different kinds of state, and the store above
is built for only one of them. An observation is append-only and survives by
never being overwritten. A lease is the opposite: its whole meaning is that it
changes hands, and if two agents both "hold" a worktree the state is simply
wrong. Append-only cannot express that, so leases do not go in the store, and
they never go in `agent-memory/` either — they change by the minute and differ
per checkout, so committing them would only ever conflict.

git already has the primitive. `git worktree lock --reason` is persisted, shown
to every tool by `git worktree list --porcelain`, and a second lock on a locked
worktree is refused — a compare-and-swap with no server. What it lacks is an
expiry and an owner check on `unlock`; both are supplied by the reason string:

```
locked parallel-memory;holder=agent-a;until=2026-09-03T12:48:38+00:00;task=6KqN6Ki8…
```

`holder` and `until` are plain text, so anyone running plain git sees who has it
and for how long. The task is base64url, because git C-quotes a reason that
contains spaces or non-ASCII (octal escapes), which would make it unparseable.

```bash
parallel-memory worktree claim .worktrees/5136 --task "fix auth" --ttl-minutes 120
parallel-memory worktree holders          # drops expired leases first
parallel-memory worktree release .worktrees/5136
```

As MCP tools: `claim_worktree`, `release_worktree`, `worktree_holders`. Rules:

- a claim on a worktree someone else holds fails, unless their lease has expired — a crashed holder must not pin a worktree forever
- only the holder may release or renew; `--force` exists for a human
- a lock written with plain git (no `parallel-memory;` prefix) has no expiry and is never taken over

Claude Code's own `activeWorktreeSession` in `~/.claude.json` records one
session's worktree for that session; it is not a shared registry, which is why
this exists.

## Scope

This holds **observations** — what an agent looked up and what it found. Large,
perishable, worth capturing the moment it is learned, not worth reviewing.

It is deliberately not the place for **norms** — the invariants that must hold,
where being wrong is harmful. Those are few, they do not rot, and they should
live in the repository under review, in path-scoped rule files
(`.claude/rules/*.md`, Cursor Rules with globs, Copilot `applyTo`) that fire on
file paths rather than on a model deciding to search.

## Tests

```bash
pip install -e ".[dev]"
pytest tests -q
```

Covers the four concurrency cases above, real `git worktree` resolution, both
interop round-trips, and multi-vendor fan-out. The agent tests inject a fake
runner, so the suite spends no model calls.

## License

Apache-2.0
