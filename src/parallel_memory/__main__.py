"""CLI: serve over stdio or HTTP, or inspect the store."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .store import Store
from .writer import store_path, writer_id
from .graphify_sync import export as graphify_export


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="parallel-memory")
    p.add_argument("--db", default=None, help="store path (default: <repo>/.parallel-memory/memory.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the MCP server")
    s.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8931)
    s.add_argument("--api-key", default=None,
                   help="require this key on the HTTP transport (Bearer or X-API-Key)")

    sub.add_parser("status", help="show where the store is and who has written")

    e = sub.add_parser("export", help="write graphify memory docs")
    e.add_argument("--memory-dir", default="graphify-out/memory")

    r = sub.add_parser("run", help="run one subtask on a chosen agent backend")
    r.add_argument("task")
    r.add_argument("--agent", default="claude", help="backend name (claude, codex, ...)")
    r.add_argument("--cwd", default=None)

    f = sub.add_parser("fanout", help="run one task on several backends at once")
    f.add_argument("task")
    f.add_argument("--agent", action="append", default=None,
                   help="repeatable; defaults to every available backend")
    f.add_argument("--cwd", default=None)

    sub.add_parser("backends", help="list agent backends found on this machine")

    t = sub.add_parser("team", help="share observations with a team over git")
    tsub = t.add_subparsers(dest="team_cmd", required=True)
    ts = tsub.add_parser("sync", help="import teammates' observations, export ours")
    ts.add_argument("--dir", default=None, help="default: <repo>/agent-memory")
    ts.add_argument("--commit", action="store_true", help="git commit the result")
    th = tsub.add_parser("hooks", help="install git hooks that re-import after pull")
    th.add_argument("--dir", default=None)

    x = sub.add_parser("sync-codex", help="import Codex's distilled memories (read-only)")
    x.add_argument("--db", dest="codex_db", default=None)

    a = sub.add_parser("export-agents",
                       help="write the markdown memory surface AGENTS.md points at")
    a.add_argument("--out", default=".parallel-memory/CODEX_MEMORY.md")

    c = sub.add_parser("sync-claude",
                       help="unify Claude Code auto-memory across worktrees")
    c.add_argument("--repo-root", default=None)
    c.add_argument("--dry-run", action="store_true",
                   help="import only; do not write back")

    args = p.parse_args(argv)
    store = Store(path=args.db)

    if args.cmd == "serve":
        from . import server
        if args.http:
            server.run_http(store, args.host, args.port, api_key=args.api_key)
        else:
            asyncio.run(server.run_stdio(store))
        return 0

    if args.cmd == "status":
        print(json.dumps({
            "store": str(store.path),
            "resolved_from": str(store_path()),
            "writer_id": writer_id(),
            "observations": store.count(),
            "writers": store.writers(),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "backends":
        from .agents import BACKENDS
        print(json.dumps({n: {"command": list(b.command), "available": b.available()}
                          for n, b in sorted(BACKENDS.items())}, indent=2))
        return 0

    if args.cmd == "run":
        from .agents import run_agent
        res = run_agent(store, args.task, backend=args.agent, cwd=args.cwd)
        print(json.dumps({"backend": res.backend, "writer": res.writer,
                          "exit_code": res.exit_code,
                          "observation_id": res.observation_id,
                          "output": res.output}, ensure_ascii=False, indent=2))
        return 0 if res.ok else 1

    if args.cmd == "fanout":
        from .agents import available_backends, fan_out
        agents = args.agent or available_backends()
        if not agents:
            print("no agent backend found on this machine", file=sys.stderr)
            return 2
        results = fan_out(store, [(args.task, a) for a in agents], cwd=args.cwd)
        print(json.dumps([{"backend": r.backend, "writer": r.writer,
                           "exit_code": r.exit_code,
                           "observation_id": r.observation_id,
                           "output": r.output} for r in results],
                         ensure_ascii=False, indent=2))
        return 0 if all(r.ok for r in results) else 1

    if args.cmd == "team":
        from . import team as team_mod
        if args.team_cmd == "sync":
            print(json.dumps(team_mod.sync(store, args.dir, commit=args.commit),
                             ensure_ascii=False, indent=2))
            return 0
        installed = team_mod.install_hooks(directory=args.dir)
        print(json.dumps({"installed": [str(p) for p in installed]}, indent=2)
              if installed else "hooks already installed")
        return 0

    if args.cmd == "sync-codex":
        from . import codex_sync
        print(json.dumps(codex_sync.sync(store, db_path=args.codex_db),
                         ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "export-agents":
        from . import codex_sync
        path = codex_sync.export_agents_memory(store.all(), args.out)
        print(f"{store.count()} observations -> {path}")
        return 0

    if args.cmd == "sync-claude":
        from . import claude_sync
        from .writer import repo_root
        root = args.repo_root or repo_root()
        if root is None:
            print("not inside a git repository; pass --repo-root", file=sys.stderr)
            return 2
        print(json.dumps(claude_sync.sync(store, root, write_back=not args.dry_run),
                         ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "export":
        written = graphify_export(store.all(), args.memory_dir)
        print(f"{len(written)} docs -> {args.memory_dir}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
