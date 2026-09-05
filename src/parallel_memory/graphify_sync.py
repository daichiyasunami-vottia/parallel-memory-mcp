"""Mirror the store into graphify memory docs, and read them back.

graphify already reads ``graphify-out/memory/*.md`` — one markdown file per
memory, YAML frontmatter, folded on read. That format is the interop surface:
export into it and ``graphify --update`` / ``graphify reflect`` pick the
observations up with no changes on their side.

The one thing this module does differently from graphify's own writer is the
FILENAME. graphify derives it from ``query_<second>_<question-slug-50>.md``,
so two writes that share a second and the first 50 characters of the question
resolve to one path and the later write silently replaces the earlier one.
Here the filename carries the writer and the observation's UUID, so distinct
observations cannot collide however similar their questions or timing.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

_ESCAPES = {
    "\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r",
    "\t": "\\t", "\0": "\\0", "\u2028": "\\L", "\u2029": "\\P",
}

# Mirrors graphify.reflect's frontmatter grammar, so a round-trip is exact.
_SCALAR_RE = re.compile(r'^([A-Za-z_][\w-]*):\s*"(.*)"\s*$')
_LIST_RE = re.compile(r"^([A-Za-z_][\w-]*):\s*\[(.*)\]\s*$")
_DQ_ITEM_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def yaml_str(s: str) -> str:
    """Escape a string for a YAML double-quoted scalar graphify can parse back."""
    out = []
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


def _slug(s: str, limit: int = 50) -> str:
    return re.sub(r"[^\w]", "_", s.lower())[:limit].strip("_")


def doc_filename(obs: dict[str, Any]) -> str:
    """Collision-free by construction: the UUID is part of the name."""
    stamp = str(obs["created_at"]).replace("-", "").replace(":", "")[:15]
    return f"query_{stamp}_{obs['writer']}_{obs['id'][:8]}_{_slug(obs['question'])}.md"


def render_doc(obs: dict[str, Any]) -> str:
    lines = [
        "---",
        'type: "query"',
        f'date: "{obs["created_at"]}"',
        f'question: "{yaml_str(obs["question"])}"',
        f'contributor: "{yaml_str(obs["writer"])}"',
        # graphify's parser ignores keys it does not know, so carrying the id
        # here keeps a re-import exact without changing what graphify reads.
        f'memory_id: "{yaml_str(obs["id"])}"',
    ]
    prov = (obs.get("meta") or {}).get("provenance") or {}
    if prov.get("head"):
        lines.append(f'head: "{yaml_str(prov["head"])}"')
        lines.append(f'branch: "{yaml_str(prov.get("branch", ""))}"')
    if obs.get("outcome"):
        lines.append(f'outcome: "{yaml_str(obs["outcome"])}"')
    if obs.get("correction"):
        lines.append(f'correction: "{yaml_str(obs["correction"])}"')
    if obs.get("source_nodes"):
        items = ", ".join(f'"{yaml_str(n)}"' for n in obs["source_nodes"][:10])
        lines.append(f"source_nodes: [{items}]")
    lines.append("---")

    body = ["", f"# Q: {obs['question']}", "", "## Answer", "", obs["answer"]]
    if obs.get("outcome") or obs.get("correction"):
        body += ["", "## Outcome", ""]
        if obs.get("outcome"):
            body.append(f"- Signal: {obs['outcome']}")
        if obs.get("correction"):
            body.append(f"- Correction: {obs['correction']}")
    if obs.get("source_nodes"):
        body += ["", "## Source Nodes", ""]
        body += [f"- {n}" for n in obs["source_nodes"]]
    return "\n".join(lines + body)


def export(observations: Iterable[dict[str, Any]], memory_dir: "str | Path") -> list[Path]:
    """Write one graphify memory doc per observation. Idempotent."""
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for obs in observations:
        path = memory_dir / doc_filename(obs)
        content = render_doc(obs)
        # Skip a byte-identical rewrite so graphify's mtime-based incremental
        # detection does not treat an unchanged export as churn.
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def parse_doc(text: str) -> dict[str, Any] | None:
    """Parse a graphify memory doc's frontmatter (same grammar graphify uses)."""
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fields: dict[str, Any] = {"source_nodes": []}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = _LIST_RE.match(line)
        if m and m.group(1) == "source_nodes":
            fields["source_nodes"] = [_unescape(i) for i in _DQ_ITEM_RE.findall(m.group(2))]
            continue
        m = _SCALAR_RE.match(line)
        if m and m.group(1) in ("type", "date", "question", "outcome",
                                "correction", "contributor", "memory_id"):
            fields[m.group(1)] = _unescape(m.group(2))
    return fields


def _unescape(s: str) -> str:
    simple = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t",
              "0": "\0", "L": "\u2028", "P": "\u2029"}
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in simple:
                out.append(simple[nxt]); i += 2; continue
            if nxt == "x" and i + 3 < len(s):
                try:
                    out.append(chr(int(s[i + 2:i + 4], 16))); i += 4; continue
                except ValueError:
                    pass
            if nxt == "u" and i + 5 < len(s):
                try:
                    out.append(chr(int(s[i + 2:i + 6], 16))); i += 6; continue
                except ValueError:
                    pass
        out.append(s[i]); i += 1
    return "".join(out)


def import_dir(memory_dir: "str | Path") -> list[dict[str, Any]]:
    """Read existing graphify memory docs so a populated repo can be absorbed."""
    memory_dir = Path(memory_dir)
    if not memory_dir.exists():
        return []
    docs = []
    for path in sorted(memory_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        parsed = parse_doc(text)
        if parsed is None:
            continue
        parsed["_path"] = path.name
        parsed["answer"] = _answer_section(text)
        docs.append(parsed)
    return docs


def _answer_section(text: str) -> str:
    m = re.search(r"^## Answer\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return m.group(1).strip() if m else ""
