"""MCP server exposing the store.

Two transports, deliberately:

* **stdio** — the per-developer default. Safe here because the store, not the
  process, owns serialisation: N stdio processes across N worktrees share one
  SQLite file and queue on its write lock.
* **streamable HTTP** — one shared server for a team or a CI fleet.

That difference is the point. A store whose safety depends on there being a
single process has no stdio answer at all; this one does not care how many
processes there are.
"""
from __future__ import annotations

import json
from typing import Any

from . import __version__
from .store import OUTCOMES, Store
from .graphify_sync import export as graphify_export, import_dir as graphify_import
from . import claude_sync, codex_sync, team
from .writer import repo_root

TOOLS: list[dict[str, Any]] = [
    {
        "name": "remember",
        "description": "Record an observation (a question and what was found). "
                       "Safe to call from any number of agents at once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "answer": {"type": "string"},
                "outcome": {"type": "string", "enum": list(OUTCOMES)},
                "correction": {"type": "string"},
                "source_nodes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["question", "answer"],
        },
    },
    {
        "name": "recall",
        "description": "Search observations across every writer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "writer": {"type": "string"},
                "outcome": {"type": "string", "enum": list(OUTCOMES)},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "forget",
        "description": "Delete one observation by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"observation_id": {"type": "string"}},
            "required": ["observation_id"],
        },
    },
    {
        "name": "writers",
        "description": "List writers that have contributed, with counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sync_claude",
        "description": "Fold Claude Code's per-worktree auto-memory "
                       "(~/.claude/projects/<cwd-slug>/memory/) into this store and "
                       "write the union back to every worktree, so what one worktree "
                       "learned is visible from all of them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_root": {"type": "string",
                              "description": "defaults to the current repository"},
                "write_back": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "sync_team",
        "description": "Import observations teammates committed to the shared "
                       "directory, then export this store's own. The directory is "
                       "text, one file per observation, safe to commit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "default": "agent-memory"},
                "commit": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "sync_codex",
        "description": "Import Codex's distilled memories "
                       "($CODEX_HOME/memories_*.sqlite) into this store. Read-only "
                       "on Codex's side; the write-back surface is a markdown file "
                       "referenced from AGENTS.md, not Codex's database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "db_path": {"type": "string",
                            "description": "defaults to the newest memories_*.sqlite"},
                "agents_memory_path": {
                    "type": "string",
                    "description": "if given, also write the markdown surface here"},
            },
        },
    },
    {
        "name": "sync_graphify",
        "description": "Mirror observations into graphify memory docs "
                       "(graphify-out/memory/*.md), and absorb any docs already there.",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_dir": {"type": "string",
                                          "default": "graphify-out/memory"}},
        },
    },
]


def dispatch(store: Store, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Pure request handling, so the tests do not need a transport."""
    if name == "remember":
        return store.remember(
            args["question"], args["answer"],
            outcome=args.get("outcome"), correction=args.get("correction"),
            source_nodes=args.get("source_nodes"))
    if name == "recall":
        return {"observations": store.recall(
            args.get("query", ""), writer=args.get("writer"),
            outcome=args.get("outcome"), limit=int(args.get("limit", 50)))}
    if name == "forget":
        return {"deleted": store.forget(args["observation_id"])}
    if name == "writers":
        return {"writers": store.writers()}
    if name == "sync_claude":
        root = args.get("repo_root") or repo_root()
        if root is None:
            raise ValueError("not inside a git repository; pass repo_root")
        return claude_sync.sync(store, root,
                                write_back=bool(args.get("write_back", True)))
    if name == "sync_team":
        return team.sync(store, args.get("directory"),
                         commit=bool(args.get("commit", False)))
    if name == "sync_codex":
        result = codex_sync.sync(store, db_path=args.get("db_path"))
        target = args.get("agents_memory_path")
        if target:
            result["agents_memory"] = str(
                codex_sync.export_agents_memory(store.all(), target))
        return result
    if name == "sync_graphify":
        memory_dir = args.get("memory_dir", "graphify-out/memory")
        absorbed = graphify_import(memory_dir)
        known = {(o["question"], o["created_at"]) for o in store.all()}
        added = 0
        for doc in absorbed:
            key = (doc.get("question", ""), doc.get("date", ""))
            if not doc.get("question") or key in known:
                continue
            store.remember(doc["question"], doc.get("answer", ""),
                           outcome=doc.get("outcome") or None,
                           correction=doc.get("correction") or None,
                           source_nodes=doc.get("source_nodes") or [])
            added += 1
        written = graphify_export(store.all(), memory_dir)
        return {"memory_dir": str(memory_dir), "imported": added,
                "exported": len(written)}
    raise ValueError(f"unknown tool: {name}")


def build_server(store: Store):
    """Build the low-level MCP server shared by both transports.

    The `mcp` package moved tool registration from 1.x decorators
    (``@server.list_tools()``, handlers taking ``(name, arguments)``) to 2.x
    constructor callbacks (``on_list_tools=``, handlers taking a request context
    and typed params, returning result objects). Both are supported so this runs
    against whichever version the host has installed.
    """
    from mcp.server import Server          # imported lazily: the [mcp] extra
    import mcp.types as types

    def _tools() -> list["types.Tool"]:
        return [types.Tool(**t) for t in TOOLS]

    def _content(name: str, arguments: dict[str, Any] | None):
        result = dispatch(store, name, arguments or {})
        return [types.TextContent(type="text",
                                  text=json.dumps(result, ensure_ascii=False, indent=2))]

    # --- mcp 2.x: constructor callbacks ---------------------------------
    async def on_list_tools(ctx, params=None):
        return types.ListToolsResult(tools=_tools())

    async def on_call_tool(ctx, params):
        return types.CallToolResult(content=_content(params.name, params.arguments))

    try:
        return Server("parallel-memory", version=__version__,
                      on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    except TypeError:
        pass

    # --- mcp 1.x: decorators --------------------------------------------
    server = Server("parallel-memory")

    @server.list_tools()
    async def _list_tools():
        return _tools()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None):
        return _content(name, arguments)

    return server


async def run_stdio(store: Store) -> None:
    from mcp.server.stdio import stdio_server
    server = build_server(store)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


class _ApiKeyGate:
    """Reject requests without the shared key, before the MCP session starts.

    A team server is reachable by anything that can open the port, so the key
    is checked in ASGI rather than per-tool. Constant-time compare, because the
    key is a secret and the check is on an open port.
    """

    def __init__(self, app, api_key: str):
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        supplied = headers.get(b"authorization", b"").decode("latin-1")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:]
        if not supplied:
            supplied = headers.get(b"x-api-key", b"").decode("latin-1")
        import hmac
        if not hmac.compare_digest(supplied, self.api_key):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        await self.app(scope, receive, send)


def run_http(store: Store, host: str = "127.0.0.1", port: int = 8931,
             path: str = "/mcp", api_key: str | None = None) -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    server = build_server(store)
    manager = StreamableHTTPSessionManager(app=server, json_response=False)

    async def handle(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    endpoint = _ApiKeyGate(handle, api_key) if api_key else handle
    app = Starlette(routes=[Mount(path, app=endpoint)], lifespan=lifespan)
    uvicorn.run(app, host=host, port=port)
