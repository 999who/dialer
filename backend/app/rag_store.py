"""Embeddings (multilingual-e5-small) + vector search in Supabase Postgres (pgvector).

We talk to Postgres directly with asyncpg instead of the Supabase REST API:
a pooled connection + HNSW index gives ~5-30 ms per query, while PostgREST adds
an HTTP round-trip on top.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

log = logging.getLogger("rag")


class Embedder:
    """e5 models expect 'query: ' / 'passage: ' prefixes; vectors are L2-normalised.

    Uses sentence-transformers (torch) when installed; otherwise the ONNX export of the same
    model on onnxruntime, so the all-in-one exe doesn't need torch.
    """

    dim = 384

    def __init__(self, model_name: str, device: str | None = None):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            self.model = None
            self._onnx = _OnnxE5(model_name)
        else:
            self.model = SentenceTransformer(model_name, device=device)
        self._encode(["query: rozgrzewka"])  # warm-up

    def _encode(self, texts: list[str]) -> np.ndarray:
        if self.model is None:
            return self._onnx.encode(texts)
        return self.model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)

    @lru_cache(maxsize=2048)
    def _query_cached(self, text: str) -> tuple[float, ...]:
        return tuple(self._encode([f"query: {text}"])[0].tolist())

    async def embed_query(self, text: str) -> np.ndarray:
        loop = asyncio.get_running_loop()
        vec = await loop.run_in_executor(None, self._query_cached, text.strip().lower())
        return np.asarray(vec, dtype=np.float32)

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return self._encode([f"passage: {t}" for t in texts])


# same weights, published with onnx/model.onnx (used if the main repo lacks an ONNX export)
ONNX_MIRRORS = {"intfloat/multilingual-e5-small": ["Xenova/multilingual-e5-small"]}


class _OnnxE5:
    """multilingual-e5 from its ONNX export: tokenizer.json + onnx/model.onnx, mean pooling."""

    def __init__(self, model_name: str):
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        repos = [model_name] + [r for r in ONNX_MIRRORS.get(model_name, []) if r != model_name]
        for i, repo in enumerate(repos):
            try:
                tok = hf_hub_download(repo, "tokenizer.json")
                onnx = hf_hub_download(repo, "onnx/model.onnx")
                break
            except Exception as e:
                if i == len(repos) - 1:
                    raise
                log.warning("no ONNX export in %s (%s), trying %s", repo, e, repos[i + 1])
        self.tok = Tokenizer.from_file(tok)
        self.tok.enable_truncation(512)
        self.tok.enable_padding()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        self.session = ort.InferenceSession(onnx, sess_options=opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def encode(self, texts: list[str]) -> np.ndarray:
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run(None, feed)[0]  # (batch, tokens, 384)
        m = mask[..., None].astype(np.float32)
        vec = (hidden * m).sum(1) / np.maximum(m.sum(1), 1e-9)
        return (vec / np.linalg.norm(vec, axis=1, keepdims=True)).astype(np.float32)


@dataclass
class KBMatch:
    id: int
    kind: str
    topic: str
    title: str
    question: str
    answer: str
    hint: str
    content: str
    verified: bool
    source_ref: str
    similarity: float

    def to_prompt(self, n: int) -> str:
        lines = [f"[{n}] typ={self.kind} temat={self.topic or '-'} verified={str(self.verified).lower()} "
                 f"podobieństwo={self.similarity:.2f}"]
        if self.title:
            lines.append(f"Tytuł: {self.title}")
        if self.question:
            lines.append(f"Pytanie: {self.question}")
        if self.answer:
            lines.append(f"Odpowiedź: {self.answer}")
        if self.hint:
            lines.append(f"hint: {self.hint}")
        if self.content and not self.answer:
            lines.append(f"Treść: {self.content}")
        return "\n".join(lines)


class KnowledgeBase:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def start(self) -> None:
        if not self.dsn:
            log.warning("DATABASE_URL empty: RAG disabled, prompts get empty context")
            return
        import asyncpg
        from pgvector.asyncpg import register_vector

        async def init(conn):
            await register_vector(conn)
            await conn.execute("set hnsw.ef_search = 40")

        # statement_cache_size=0 keeps us compatible with Supabase's transaction pooler (6543)
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=8, init=init,
                                              statement_cache_size=0)

    async def search(self, vec: np.ndarray, k: int, min_sim: float) -> list[KBMatch]:
        if not self.pool:
            return []
        rows = await self.pool.fetch("select * from match_kb($1, $2, $3)", vec, k, min_sim)
        return [KBMatch(**{**dict(r), "similarity": float(r["similarity"])}) for r in rows]

    # ---- call logging (best effort, never blocks hints) ----
    async def log_call_start(self, call_id: str, agent_id: str, phone: str) -> None:
        await self._exec("insert into calls(id, agent_id, phone_masked) values($1,$2,$3) on conflict do nothing",
                         call_id, agent_id, phone)

    async def log_call_end(self, call_id: str, summary: str) -> None:
        await self._exec("update calls set ended_at = now(), summary = $2 where id = $1", call_id, summary)

    async def log_utterance(self, call_id: str, speaker: str, text: str, t_start: float, t_end: float) -> None:
        await self._exec("insert into call_utterances(call_id, speaker, text, t_start, t_end) values($1,$2,$3,$4,$5)",
                         call_id, speaker, text, t_start, t_end)

    async def log_hint(self, hint_id: str, call_id: str, category: str, hint: str,
                       kb_ids: list[int], latency_ms: int) -> None:
        await self._exec("insert into call_hints(id, call_id, category, hint, kb_item_ids, latency_ms) "
                         "values($1,$2,$3,$4,$5,$6)", hint_id, call_id, category, hint, kb_ids, latency_ms)

    async def log_feedback(self, hint_id: str, useful: bool | None, copied: bool | None) -> None:
        await self._exec("update call_hints set useful = coalesce($2, useful), copied = coalesce($3, copied) "
                         "where id = $1", hint_id, useful, copied)

    async def _exec(self, sql: str, *args) -> None:
        if not self.pool:
            return
        try:
            await self.pool.execute(sql, *args)
        except Exception:
            log.exception("db log failed")
