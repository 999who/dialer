"""EMANAGER Dialer backend: WebSocket audio in, JSON hints out.

Protocol (one WebSocket per operator, path /ws):

client -> server
  text   {"type":"hello","token":"…","agent_id":"…","sample_rate":16000,"channels":2,"format":"s16le"}
  binary interleaved stereo PCM16 @16 kHz, L = operator mic, R = client (loopback), ~100 ms per frame
  text   {"type":"call_start","phone":"+48 512 *** 204"} | {"type":"call_end"}
  text   {"type":"pause","paused":true}
  text   {"type":"feedback","hint_id":"…","useful":true|false} | {"type":"feedback","hint_id":"…","copied":true}
  text   {"type":"text","speaker":"client|operator","text":"…"}   # test injection, bypasses STT

server -> client
  {"type":"ready","model":"…","stt":"parakeet|none","llm_error":"","rag_error":""}   # llm_error: no hints; rag_error: no knowledge base
  {"type":"call_started","call_id":"…"}
  {"type":"transcript","speaker":"client","text":"…","t":12.3}
  {"type":"hint","id":"…","category":"objection","topic":"cena","hint":"…","quote":"…","match":0.92,
   "variants":["…"],"sources":[{"title":"…","ref":"…"}],"latency_ms":1800}
  {"type":"latency","ms":1650}
  {"type":"call_summary","duration_s":468,"summary":"…","hints":5,"used":3,"objections":2}
  {"type":"error","code":"auth|bad_format","message":"…"}
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .config import get_settings
from .rag_store import Embedder, KnowledgeBase
from .session import CallSession, Utterance
from .stt import NullSTT, ChannelSegmenter, make_stt, make_vad, split_stereo_pcm16

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    loop = asyncio.get_running_loop()
    state["stt"] = make_stt(s)
    await state["stt"].start()
    # The knowledge base and call logging are optional: if the database or the e5 model is
    # unreachable, speech recognition and hints still start, and the client says what's missing.
    state["kb"], state["embedder"], state["rag_error"] = KnowledgeBase(s.database_url), None, ""
    try:
        await state["kb"].start()
        if s.database_url:
            state["embedder"] = await loop.run_in_executor(None, Embedder, s.embed_model)
    except Exception as e:
        log.exception("knowledge base unavailable, continuing without it")
        state["rag_error"] = f"{type(e).__name__}: {e}"[:300]
        if state["kb"].pool:
            await state["kb"].pool.close()
        state["kb"], state["embedder"] = KnowledgeBase(""), None
    state["llm"], state["llm_error"] = None, ""
    if s.gemini_api_key:
        from .llm import HintLLM
        llm = HintLLM(s)
        err = await llm.check()
        if err:
            # keep it: a network hiccup at startup shouldn't disable hints, but say it loudly
            log.error("GEMINI CHECK FAILED (model=%s): %s. Check GEMINI_API_KEY / GEMINI_MODEL in .env",
                      s.gemini_model, err)
            state["llm_error"] = err
        else:
            log.info("gemini ok: model=%s", s.gemini_model)
        state["llm"] = llm
    else:
        log.warning("GEMINI_API_KEY empty: hints disabled, transcripts only")
        state["llm_error"] = "GEMINI_API_KEY empty"
    log.info("ready: stt=%s model=%s llm=%s rag=%s", s.stt_engine, s.gemini_model,
             "ok" if not state["llm_error"] else "ERROR", bool(state["embedder"]))
    yield
    if state["kb"].pool:
        await state["kb"].pool.close()


app = FastAPI(title="EMANAGER Dialer backend", lifespan=lifespan)


@app.get("/health")
async def health():
    s = get_settings()
    return {"ok": True, "stt": s.stt_engine, "model": s.gemini_model, "rag": bool(state.get("embedder")),
            "llm": bool(state.get("llm")), "llm_error": state.get("llm_error", "")}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    s = get_settings()
    await ws.accept()
    send_lock = asyncio.Lock()

    async def send(msg: dict) -> None:
        async with send_lock:
            try:
                await ws.send_text(json.dumps(msg, ensure_ascii=False))
            except Exception:
                pass  # socket gone; receive loop will notice

    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
    except Exception:
        await ws.close(code=4400)
        return
    if hello.get("type") != "hello" or (s.auth_token and hello.get("token") != s.auth_token):
        await send({"type": "error", "code": "auth", "message": "bad token"})
        await ws.close(code=4401)
        return
    if hello.get("sample_rate", 16000) != 16000 or hello.get("channels", 2) != 2:
        await send({"type": "error", "code": "bad_format", "message": "expected 16 kHz stereo s16le"})
        await ws.close(code=4415)
        return

    session = CallSession(settings=s, llm=state["llm"], kb=state["kb"], embedder=state["embedder"],
                          send=send, agent_id=str(hello.get("agent_id", "")))
    seg_args = (s.vad_min_silence_ms, s.vad_max_utterance_s, s.vad_min_utterance_ms)
    segmenters = [ChannelSegmenter("operator", make_vad(s.vad_engine), *seg_args),
                  ChannelSegmenter("client", make_vad(s.vad_engine), *seg_args)]
    stt = state["stt"]
    use_audio = not isinstance(stt, NullSTT)

    async def transcribe(seg):
        try:
            text = await stt.transcribe(seg.audio)
        except Exception:
            log.exception("stt failed")
            return
        await session.on_utterance(Utterance(seg.speaker, text, seg.t_start, seg.t_end, seg.ended_at))

    await send({"type": "ready", "model": s.gemini_model, "stt": s.stt_engine,
                "llm_error": state.get("llm_error", ""), "rag_error": state.get("rag_error", "")})
    log.info("operator %s connected", hello.get("agent_id"))
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                if not use_audio or session.paused:
                    continue
                left, right = split_stereo_pcm16(msg["bytes"])
                for segm, ch in zip(segmenters, (left, right)):
                    for seg in segm.feed(ch):
                        asyncio.create_task(transcribe(seg))
                continue
            data = json.loads(msg.get("text") or "{}")
            kind = data.get("type")
            if kind == "call_start":
                await session.start_call(str(data.get("phone", ""))[:32])
            elif kind == "call_end":
                for segm in segmenters:
                    for seg in segm.flush():
                        await transcribe(seg)
                await session.end_call()
            elif kind == "pause":
                session.paused = bool(data.get("paused"))
            elif kind == "feedback":
                await session.on_feedback(str(data.get("hint_id")), data.get("useful"), data.get("copied"))
            elif kind == "text":
                speaker = data.get("speaker") if data.get("speaker") in ("operator", "client") else "client"
                t = time.monotonic() - session.started_at
                await session.on_utterance(Utterance(speaker, str(data.get("text", ""))[:1000], t, t))
    except WebSocketDisconnect:
        pass
    finally:
        log.info("operator %s disconnected", hello.get("agent_id"))
        if session.call_id:
            await session.kb.log_call_end(session.call_id, "")
