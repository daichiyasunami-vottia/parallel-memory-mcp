// Lost-update harness for @modelcontextprotocol/server-memory.
// Never touches a real store: MEMORY_FILE_PATH is a fresh temp file per run.
import { spawn } from "node:child_process";
import { writeFileSync, readFileSync, rmSync, existsSync } from "node:fs";

// Resolve the installed reference server. Run `npm i @modelcontextprotocol/server-memory`
// in this directory first, or point SERVER_ENTRY at your own checkout.
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
const require_ = createRequire(import.meta.url);
const SERVER = process.env.SERVER_ENTRY ??
  require_.resolve("@modelcontextprotocol/server-memory/dist/index.js");

function startServer(memFile) {
  const p = spawn("node", [SERVER], {
    env: { ...process.env, MEMORY_FILE_PATH: memFile },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const pending = new Map();
  let buf = "";
  p.stdout.on("data", (d) => {
    buf += d.toString();
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); } catch { continue; }
      if (msg.id !== undefined && pending.has(msg.id)) {
        pending.get(msg.id)(msg);
        pending.delete(msg.id);
      }
    }
  });
  let nextId = 1;
  const send = (method, params) =>
    new Promise((resolve) => {
      const id = nextId++;
      pending.set(id, resolve);
      p.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    });
  const notify = (method, params) =>
    p.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
  return { proc: p, send, notify };
}

async function handshake(s) {
  await s.send("initialize", {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "lost-update-repro", version: "1.0.0" },
  });
  s.notify("notifications/initialized", {});
}

const createEntity = (s, name) =>
  s.send("tools/call", {
    name: "create_entities",
    arguments: { entities: [{ name, entityType: "note", observations: [`obs-${name}`] }] },
  });

function countEntities(memFile) {
  if (!existsSync(memFile)) return 0;
  return readFileSync(memFile, "utf-8")
    .split("\n")
    .filter((l) => l.trim())
    .filter((l) => JSON.parse(l).type === "entity").length;
}

const N = Number(process.argv[3] ?? 20);
const mode = process.argv[2] ?? "single";
const memFile = join(tmpdir(), `bench-mem-${mode}-${process.pid}-${Date.now()}.json`);
writeFileSync(memFile, "");

let responses = [];
if (mode === "serial") {
  // Control: N calls to one process, strictly one at a time.
  const s = startServer(memFile);
  await handshake(s);
  for (let i = 0; i < N; i++) responses.push(await createEntity(s, `entity-${i}`));
  s.proc.kill();
} else if (mode === "single") {
  // Scenario A: N in-flight calls to ONE process (one agent calling tools in parallel).
  const s = startServer(memFile);
  await handshake(s);
  responses = await Promise.all(Array.from({ length: N }, (_, i) => createEntity(s, `entity-${i}`)));
  s.proc.kill();
} else {
  // Scenario B: two processes sharing one MEMORY_FILE_PATH (two worktrees / two clients).
  const a = startServer(memFile);
  const b = startServer(memFile);
  await Promise.all([handshake(a), handshake(b)]);
  responses = await Promise.all(
    Array.from({ length: N }, (_, i) =>
      createEntity(i % 2 === 0 ? a : b, `entity-${i}`)
    )
  );
  a.proc.kill();
  b.proc.kill();
}

await new Promise((r) => setTimeout(r, 200));
const got = countEntities(memFile);
const errors = responses.filter((r) => r.error || r.result?.isError).length;
const ok = responses.filter((r) => !r.error && !r.result?.isError).length;
console.log(`${mode.padEnd(8)} sent=${N} ok_responses=${ok} error_responses=${errors} kept=${got} lost=${N - got}`);
rmSync(memFile, { force: true });
