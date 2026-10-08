"""Per-call RAG controller.

utterance (Parakeet) -> should we react? -> embed client phrase -> pgvector top-k
-> master prompt -> Gemini JSON -> enrich for the overlay -> push to client.

Only one Gemini request per call is in flight; utterances that arrive meanwhile
are coalesced and the newest state is evaluated right after (no queue build-up,
no stale hints).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .llm import SILENT
from .rag_store import KBMatch

log = logging.getLogger("session")

ROLE_LABEL = {"operator": "Konsultant", "client": "Klient"}
FILLERS = {"tak", "nie", "aha", "mhm", "okej", "ok", "dobrze", "no", "yhm", "jasne", "rozumiem", "dziękuję",
           "halo", "dzień", "dobry", "słucham"}
QUESTION_WORDS = ("ile", "jak", "czy", "kiedy", "gdzie", "dlaczego", "co ", "jaki", "jaka", "jakie", "kto")


@dataclass
class Utterance:
    speaker: str
    text: str
    t_start: float
    t_end: float
    ended_at: float = field(default_factory=time.monotonic)


def worth_reacting(u: Utterance) -> bool:
    """Cheap gate before spending an embedding + LLM call."""
    words = re.findall(r"\w+", u.text.lower())
    if not words or all(w in FILLERS for w in words):
        return False
    if u.speaker == "client":
        return len(words) >= 3 or "?" in u.text or u.text.lower().startswith(QUESTION_WORDS)
    # operator lines are checked for standard violations (warning) and for
    # "operator already answered correctly" -> silence
    return len(words) >= 4


class CallSession:
    def __init__(self, *, settings, llm, kb, embedder, send: Callable[[dict], Awaitable[None]],
                 agent_id: str = ""):
        self.s = settings
        self.llm = llm
        self.kb = kb
        self.embedder = embedder
        self.send = send
        self.agent_id = agent_id
        self.call_id: str | None = None
        self.started_at = time.monotonic()
        self.history: deque[Utterance] = deque(maxlen=200)
        self.last_hint = ""
        self.client_context = ""   # the CRM client block, sent by the app once it knows the caller
        self.caller = ""           # who the caller said they are, as Gemini heard it
        self.paused = False
        self.stats = {"hints": 0, "used": 0, "objections": 0}
        self._busy = False
        self._pending: Utterance | None = None
        self._hint_ids: set[str] = set()
        self._used_ids: set[str] = set()

    # ------------------------------------------------------------ lifecycle
    async def start_call(self, phone: str = "") -> None:
        self.call_id = str(uuid.uuid4())
        self.started_at = time.monotonic()
        self.history.clear()
        self.last_hint = ""
        self.client_context = ""
        self.caller = ""
        self.stats = {"hints": 0, "used": 0, "objections": 0}
        self._hint_ids.clear()
        self._used_ids.clear()
        await self.kb.log_call_start(self.call_id, self.agent_id, phone)
        await self.send({"type": "call_started", "call_id": self.call_id})

    async def end_call(self) -> None:
        if not self.call_id:
            return
        duration = time.monotonic() - self.started_at
        transcript = self._format_history(len(self.history))
        summary = await self.llm.summarize(transcript) if self.llm else ""
        await self.kb.log_call_end(self.call_id, summary)
        await self.send({"type": "call_summary", "call_id": self.call_id, "duration_s": round(duration),
                         "summary": summary, **self.stats})
        self.call_id = None

    # ------------------------------------------------------------ input
    async def on_utterance(self, u: Utterance) -> None:
        if not u.text:
            return
        if not self.call_id:
            await self.start_call()
        self.history.append(u)
        await self.send({"type": "transcript", "speaker": u.speaker, "text": u.text,
                         "t": round(u.t_start, 2)})
        asyncio.create_task(self.kb.log_utterance(self.call_id, u.speaker, u.text, u.t_start, u.t_end))
        if self.paused or not worth_reacting(u):
            return
        if self._busy:
            self._pending = u  # coalesce: newest wins
            return
        self._busy = True  # set before the task starts, or parallel utterances all pass the check
        asyncio.create_task(self._run(u))

    async def on_feedback(self, hint_id: str, useful: bool | None = None, copied: bool | None = None) -> None:
        if hint_id not in self._hint_ids:
            return
        if (useful or copied) and hint_id not in self._used_ids:
            self._used_ids.add(hint_id)
            self.stats["used"] += 1
        await self.kb.log_feedback(hint_id, useful, copied)

    # ------------------------------------------------------------ core
    async def _run(self, u: Utterance) -> None:
        try:
            while u is not None:
                try:
                    await self._evaluate(u)
                except Exception as e:
                    log.exception("hint evaluation failed")
                    # tell the operator instead of silently showing nothing
                    reason = f"{getattr(e, 'code', type(e).__name__)} {getattr(e, 'message', None) or e}"
                    await self.send({"type": "error", "code": "llm", "message": reason[:300]})
                u, self._pending = self._pending, None
        finally:
            self._busy = False

    def _last_client(self) -> Utterance | None:
        return next((h for h in reversed(self.history) if h.speaker == "client"), None)

    def _rag_query(self) -> str:
        """Last client phrase(s): that's what operator needs an answer for."""
        clients = [h for h in list(self.history)[-6:] if h.speaker == "client"]
        if not clients:
            return ""
        q = clients[-1].text
        if len(q.split()) < 5 and len(clients) > 1:  # short follow-up, add previous phrase
            q = f"{clients[-2].text} {q}"
        return q

    def _format_history(self, n: int) -> str:
        return "\n".join(f"[{ROLE_LABEL[h.speaker]}]: {h.text}" for h in list(self.history)[-n:])

    async def _evaluate(self, u: Utterance) -> None:
        t0 = time.monotonic()
        call_id = self.call_id
        matches: list[KBMatch] = []
        query = self._rag_query()
        if query and self.embedder:
            vec = await self.embedder.embed_query(query)
            matches = await self.kb.search(vec, self.s.rag_top_k, self.s.rag_min_similarity)
        t_rag = time.monotonic()

        rag_context = "\n\n".join(m.to_prompt(i + 1) for i, m in enumerate(matches)) \
            or "(brak dopasowań w bazie wiedzy)"
        result = await self.llm.decide(rag_context, self.last_hint or "(brak)",
                                       self._format_history(self.s.history_turns),
                                       self.client_context) if self.llm else dict(SILENT)
        t_llm = time.monotonic()
        latency_ms = int((t_llm - u.ended_at) * 1000)
        log.info("utt=%r rag=%dms llm=%dms total=%dms -> %s", u.text[:60], (t_rag - t0) * 1000,
                 (t_llm - t_rag) * 1000, latency_ms, result)

        if call_id != self.call_id:  # call ended / restarted meanwhile
            return
        await self.send({"type": "latency", "ms": latency_ms})
        caller = result.get("caller", "")
        if caller and caller != self.caller:
            self.caller = caller  # the app looks this name up in the CRM when the number didn't match
            await self.send({"type": "caller", "text": caller})
        if not result["show"] or result["hint"] == self.last_hint:
            return

        hint_id = str(uuid.uuid4())
        self._hint_ids.add(hint_id)
        self.last_hint = result["hint"]
        self.stats["hints"] += 1
        if result["category"] == "objection":
            self.stats["objections"] += 1

        top = matches[0] if matches else None
        last_client = self._last_client()
        await self.send({
            "type": "hint",
            "id": hint_id,
            "category": result["category"],
            "topic": top.topic if top else "",
            "hint": result["hint"],
            "quote": last_client.text if (last_client and result["category"] in ("objection", "info")) else "",
            "match": round(top.similarity, 2) if top else None,
            "variants": self._variants(result["hint"], matches),
            "sources": [{"title": m.title, "ref": m.source_ref} for m in matches[:2] if m.title],
            "latency_ms": latency_ms,
        })
        asyncio.create_task(self.kb.log_hint(hint_id, call_id, result["category"], result["hint"],
                                             [m.id for m in matches], latency_ms))

    @staticmethod
    def _variants(main: str, matches: list[KBMatch]) -> list[str]:
        """'Inny wariant': the model's hint first, then ready hints of equally good KB matches."""
        out = [main]
        if matches:
            best = matches[0].similarity
            for m in matches:
                if m.hint and m.verified and best - m.similarity <= 0.04 and m.hint not in out:
                    out.append(m.hint)
        return out[:3]
