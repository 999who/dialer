"""Load the knowledge base into Supabase (pgvector).

    python -m tools.ingest_kb                      # kb/*.jsonl + kb/regulations/*.md
    python -m tools.ingest_kb --dry-run            # parse + embed, print, don't write
    python -m tools.ingest_kb --deactivate-missing # hide rows no longer present in files

JSONL row: {"kind","topic","title","question","answer","hint","verified","source_ref"}.
Re-running is idempotent (content_hash).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

FIELDS = ("kind", "topic", "title", "question", "answer", "hint", "content", "verified", "source_ref")


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip() and not line.lstrip().startswith("//"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"{path}:{n}: {e}")
    return rows


def chunk_markdown(path: Path, max_chars: int = 900) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    meta = {}
    m = re.match(r"^---\n(.*?)\n---\n", text, flags=re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        text = text[m.end():]
    rows, title, buf = [], path.stem, []

    def flush():
        body = "\n".join(buf).strip()
        buf.clear()
        while body:
            piece, body = body[:max_chars], body[max_chars:]
            if body:  # cut on a sentence/paragraph boundary
                cut = max(piece.rfind("\n"), piece.rfind(". "))
                if cut > max_chars // 2:
                    body, piece = piece[cut + 1:] + body, piece[:cut + 1]
            rows.append({"kind": "regulation", "topic": meta.get("topic", ""), "title": title,
                         "content": piece.strip(), "verified": meta.get("verified", "true") != "false",
                         "source_ref": meta.get("source_ref", path.name)})

    doc = path.stem
    for line in text.splitlines():
        h = re.match(r"^(#{1,4})\s+(.*)", line)
        if h:
            flush()
            if len(h.group(1)) == 1:
                doc = title = h.group(2).strip()
            else:
                head = h.group(2).strip()
                m = re.match(r"^(§\s*[\d.]+)", head)
                # chip label like "Regulamin rabatów § 4.2"
                title = f"{doc} {m.group(1)}" if m else f"{doc}: {head}"
        else:
            buf.append(line)
    flush()
    return [r for r in rows if r["content"]]


def passage_text(r: dict) -> str:
    # The query is what the client says, so embed the client-side wording first.
    main = r.get("question") or r.get("content") or r.get("answer")
    return f"{r.get('title', '')}. {main}".strip(". ")


def normalise(r: dict) -> dict:
    out = {k: r.get(k, "") for k in FIELDS}
    out["verified"] = bool(r.get("verified", True))
    if out["kind"] not in ("regulation", "qa", "objection", "script"):
        raise SystemExit(f"bad kind {out['kind']!r} in {r}")
    out["content_hash"] = hashlib.sha256(json.dumps(out, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb-dir", default=str(ROOT / "kb"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--deactivate-missing", action="store_true")
    a = ap.parse_args()

    kb = Path(a.kb_dir)
    rows = [r for p in sorted(kb.glob("*.jsonl")) for r in load_jsonl(p)]
    rows += [r for p in sorted((kb / "regulations").glob("*.md")) if p.name != "README.md"
             for r in chunk_markdown(p)]
    rows = [normalise(r) for r in rows]
    print(f"{len(rows)} items")

    from app.rag_store import Embedder

    s = get_settings()
    vecs = Embedder(s.embed_model).embed_passages([passage_text(r) for r in rows])
    if a.dry_run:
        for r in rows[:5]:
            print(json.dumps(r, ensure_ascii=False)[:200])
        return

    import asyncpg
    from pgvector.asyncpg import register_vector

    conn = await asyncpg.connect(s.database_url, statement_cache_size=0)
    await register_vector(conn)
    cols = list(FIELDS) + ["content_hash", "embedding"]
    sql = (f"insert into kb_items({', '.join(cols)}) values({', '.join(f'${i + 1}' for i in range(len(cols)))}) "
           "on conflict (content_hash) do update set active = true, updated_at = now()")
    async with conn.transaction():
        await conn.executemany(sql, [[r[c] for c in cols[:-1]] + [v] for r, v in zip(rows, vecs)])
        if a.deactivate_missing:
            n = await conn.execute("update kb_items set active = false where not (content_hash = any($1))",
                                   [r["content_hash"] for r in rows])
            print("deactivated:", n)
    await conn.close()
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
