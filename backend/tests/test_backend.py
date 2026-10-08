import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.llm import MasterPrompt, parse_hint  # noqa: E402
from app.rag_store import KBMatch  # noqa: E402
from app.session import CallSession, Utterance, worth_reacting  # noqa: E402
from app.stt import ChannelSegmenter, _EnergyVAD, split_stereo_pcm16  # noqa: E402

PROMPT = ROOT.parent / "prompts" / "master_prompt_pl.md"


def test_master_prompt_split_and_render():
    mp = MasterPrompt.load(PROMPT)
    assert "# ROLA I ZADANIE" in mp.static and "{rag_context}" not in mp.static
    assert '"show": true | false' in mp.static          # JSON braces survive (no str.format)
    system, user = mp.render("CTX", "LAST", "[Klient]: Ile?")
    assert system == mp.static
    assert "CTX" in user and "LAST" in user and "[Klient]: Ile?" in user and "{" not in user


def test_master_prompt_without_rule_falls_back_to_single_prompt():
    mp = MasterPrompt("Rola\n{rag_context}\n{transcript_history}")
    system, user = mp.render("A", "B", "C")
    assert system == "Rola\nA\nC" and user


@pytest.mark.parametrize("raw,expected", [
    ('{"show": true, "category": "info", "hint": "CRM od 1500 zł"}', True),
    ('```json\n{"show": true, "category": "objection", "hint": "x"}\n```', True),
    ('{"show": false, "category": "", "hint": ""}', False),
    ('{"show": true, "category": "bogus", "hint": "x"}', False),
    ('{"show": true, "category": "info", "hint": ""}', False),
    ('not json', False),
])
def test_parse_hint(raw, expected):
    assert parse_hint(raw)["show"] is expected


def test_split_stereo():
    pcm = np.array([[1000, -2000], [3000, -4000]], dtype="<i2").tobytes()
    left, right = split_stereo_pcm16(pcm)
    assert np.allclose(left * 32768, [1000, 3000]) and np.allclose(right * 32768, [-2000, -4000])


def test_segmenter_cuts_utterance_on_silence():
    seg = ChannelSegmenter("client", _EnergyVAD(), min_silence_ms=400, max_utt_s=10, min_utt_ms=200)
    sr = 16000
    silence = np.zeros(sr // 2, dtype=np.float32)
    t = np.arange(sr) / sr
    tone = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    out = []
    for chunk in (silence, tone, silence, silence):
        for i in range(0, len(chunk), 1600):
            out += seg.feed(chunk[i:i + 1600])
    assert len(out) == 1
    s = out[0]
    assert s.speaker == "client" and 0.85 < len(s.audio) / sr < 1.4 and 0.35 < s.t_start < 0.55


def test_worth_reacting():
    assert not worth_reacting(Utterance("client", "Mhm, tak.", 0, 0))
    assert worth_reacting(Utterance("client", "Ile to kosztuje?", 0, 0))
    assert worth_reacting(Utterance("client", "Ile?", 0, 0))
    assert not worth_reacting(Utterance("operator", "Dobrze, rozumiem.", 0, 0))
    assert worth_reacting(Utterance("operator", "Gwarantuję minimum 50 klientów miesięcznie.", 0, 0))


class FakeKB:
    def __init__(self, matches):
        self.matches = matches
        self.pool = None
        self.logged = []

    async def search(self, vec, k, min_sim):
        return self.matches

    async def log_call_start(self, *a): pass
    async def log_call_end(self, *a): pass
    async def log_utterance(self, *a): pass
    async def log_hint(self, *a): self.logged.append(a)
    async def log_feedback(self, *a): pass


class FakeEmbedder:
    async def embed_query(self, text):
        return np.zeros(384, dtype=np.float32)


class FakeLLM:
    def __init__(self, result, delay=0.0):
        self.result, self.delay, self.calls = result, delay, []

    async def decide(self, rag_context, last_hint, history):
        self.calls.append((rag_context, last_hint, history))
        await asyncio.sleep(self.delay)
        return dict(self.result)

    async def summarize(self, transcript):
        return "Klient umówił konsultację."


class S:
    rag_top_k, rag_min_similarity, history_turns = 4, 0.8, 8


def match(**kw):
    base = dict(id=1, kind="objection", topic="cena", title="Obiekcja: za drogo", question="", answer="a",
                hint="Zacznij od jednego produktu.", content="", verified=True, source_ref="", similarity=0.92)
    return KBMatch(**{**base, **kw})


def run(coro):
    return asyncio.run(coro)


def test_session_emits_enriched_hint_and_summary():
    async def go():
        sent = []
        llm = FakeLLM({"show": True, "category": "objection", "hint": "Zacznij od jednego produktu; zero prowizji."})
        kb = FakeKB([match(), match(id=2, hint="Lepsze warunki od 6 miesięcy.", similarity=0.90)])

        async def send(m): sent.append(m)
        s = CallSession(settings=S, llm=llm, kb=kb, embedder=FakeEmbedder(), send=send)
        await s.start_call("+48")
        await s.on_utterance(Utterance("client", "Szczerze mówiąc, to dla nas trochę za drogo…", 1, 3))
        await asyncio.sleep(0.05)
        hint = next(m for m in sent if m["type"] == "hint")
        assert hint["category"] == "objection" and hint["topic"] == "cena" and hint["match"] == 0.92
        assert hint["quote"].startswith("Szczerze")
        assert hint["variants"][0] == hint["hint"] and len(hint["variants"]) == 3
        assert "[Klient]: Szczerze" in llm.calls[0][2] and "verified=true" in llm.calls[0][0]
        await s.on_feedback(hint["id"], useful=True)
        await s.end_call()
        summ = next(m for m in sent if m["type"] == "call_summary")
        assert summ["hints"] == 1 and summ["used"] == 1 and summ["objections"] == 1
        assert summ["summary"]
    run(go())


def test_session_coalesces_and_dedupes():
    async def go():
        sent = []
        llm = FakeLLM({"show": True, "category": "info", "hint": "Ta sama podpowiedź."}, delay=0.05)

        async def send(m): sent.append(m)
        s = CallSession(settings=S, llm=llm, kb=FakeKB([]), embedder=FakeEmbedder(), send=send)
        for i in range(4):
            await s.on_utterance(Utterance("client", f"Pytanie numer {i} o cenę?", i, i))
        await asyncio.sleep(0.3)
        assert len(llm.calls) == 2                      # first + newest coalesced, middle ones skipped
        assert "numer 3" in llm.calls[1][2]
        assert llm.calls[1][1] == "Ta sama podpowiedź."  # last_hint passed to the prompt
        assert sum(m["type"] == "hint" for m in sent) == 1  # identical hint not repeated
    run(go())


def test_websocket_text_mode(monkeypatch):
    monkeypatch.setenv("STT_ENGINE", "none")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("AUTH_TOKEN", "t")
    from app import config, main
    config.get_settings.cache_clear()
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        main.state["llm"] = FakeLLM({"show": True, "category": "info", "hint": "Start 1–2 tygodnie po umowie."})
        with c.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "hello", "token": "bad"}))
            assert ws.receive_json()["code"] == "auth"
        with c.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "hello", "token": "t", "agent_id": "a1"}))
            assert ws.receive_json()["type"] == "ready"
            ws.send_text(json.dumps({"type": "call_start"}))
            assert ws.receive_json()["type"] == "call_started"
            ws.send_text(json.dumps({"type": "text", "speaker": "client", "text": "Kiedy możecie zacząć?"}))
            msgs = [ws.receive_json() for _ in range(3)]
            assert [m["type"] for m in msgs] == ["transcript", "latency", "hint"]
            ws.send_text(json.dumps({"type": "call_end"}))
            assert ws.receive_json()["type"] == "call_summary"
    config.get_settings.cache_clear()


def test_new_prompt_without_rag_slot_gets_no_literal_placeholders_and_kb_matches():
    from app.llm import MasterPrompt

    p = MasterPrompt.load(ROOT.parent / "prompts" / "master_prompt_pl.md")
    static, dyn = p.render("[1] Sklep od 5000 zł", "(brak)", "[Klient]: Ile kosztuje sklep?")
    assert "# ROLA" in static and "{caller_name}" not in dyn and "brak danych" in dyn
    assert "[Klient]: Ile kosztuje sklep?" in dyn
    if "{rag_context}" not in p.dynamic:
        assert "Sklep od 5000 zł" in dyn
    _, dyn = p.render("(brak dopasowań w bazie wiedzy)", "(brak)", "x")
    assert "FRAGMENTY BAZY WIEDZY" not in dyn


def test_unreachable_database_does_not_stop_startup(monkeypatch):
    monkeypatch.setenv("STT_ENGINE", "none")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/db")  # nothing listens on port 1
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("AUTH_TOKEN", "t")
    from app import config, main
    config.get_settings.cache_clear()
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": "hello", "token": "t", "agent_id": "a1"}))
            ready = ws.receive_json()
            assert ready["type"] == "ready" and ready["rag_error"]
    config.get_settings.cache_clear()
