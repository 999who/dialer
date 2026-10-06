import asyncio
import sys
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.langfilter import is_foreign_language  # noqa: E402
from app.stt import SR, ChannelSegmenter, ParakeetSTT, _SileroVAD, make_vad, prepare_audio  # noqa: E402


def test_silero_onnx_loads_and_rejects_silence():
    vad = make_vad("silero")
    assert isinstance(vad, _SileroVAD)
    probs = [vad.prob(np.zeros(512, dtype=np.float32)) for _ in range(20)]
    assert max(probs) < 0.2


def test_silero_detectors_keep_separate_state():
    a, b = _SileroVAD(), _SileroVAD()
    noise = (np.random.default_rng(0).standard_normal(512) * 0.3).astype(np.float32)
    a.prob(noise)
    assert not np.array_equal(a._state, b._state)


def test_segmenter_with_silero_ignores_silence():
    seg = ChannelSegmenter("client", _SileroVAD(), 450, 12, 300)
    assert seg.feed(np.zeros(SR * 3, dtype=np.float32)) == []


def test_prepare_audio_highpass_and_normalize():
    t = np.arange(SR) / SR
    hum = 0.15 * np.sin(2 * np.pi * 30 * t)          # below 80 Hz cutoff
    voice = 0.15 * np.sin(2 * np.pi * 400 * t)
    out = prepare_audio((hum + voice).astype(np.float32))
    assert out.dtype == np.float32
    assert 0.8 < np.max(np.abs(out)) <= 0.93       # quiet input brought up to ~0.92 peak
    spec = np.abs(np.fft.rfft(out))
    assert spec[30] < spec[400] * 0.3                # hum attenuated relative to voice


def test_language_filter():
    assert is_foreign_language("Yeah. Okay.")
    assert is_foreign_language("Well I got you.")
    assert not is_foreign_language("Ile to kosztuje?")
    assert not is_foreign_language("Okej, to dobrze.")
    assert not is_foreign_language("CRM")


def test_parakeet_cpu_engine_batches_and_filters(monkeypatch):
    calls = {}

    class FakeModel:
        def recognize(self, clips, sample_rate):
            if isinstance(clips, np.ndarray):
                return ""
            calls.setdefault("batches", []).append(len(clips))
            return ["Szczerze mówiąc,  to drogo.", "Yeah."][: len(clips)]

    def load_model(name, path, **kw):
        calls.update(name=name, path=path, **kw)
        return FakeModel()

    monkeypatch.setitem(sys.modules, "onnx_asr", types.SimpleNamespace(load_model=load_model))

    async def go():
        stt = ParakeetSTT(threads=2, model_path="", window_ms=30, max_batch=8)
        await stt.start()
        speech = np.full(SR, 0.1, dtype=np.float32)
        a, b, short = await asyncio.gather(stt.transcribe(speech), stt.transcribe(speech),
                                           stt.transcribe(np.zeros(1000, dtype=np.float32)))
        return a, b, short

    a, b, short = asyncio.run(go())
    assert calls["name"] == "nemo-parakeet-tdt-0.6b-v3" and calls["quantization"] == "int8"
    assert calls["providers"] == ["CPUExecutionProvider"] and calls["sess_options"].intra_op_num_threads == 2
    assert calls["batches"] == [2]          # both utterances in one model call
    assert a == "Szczerze mówiąc, to drogo." and b == "" and short == ""
