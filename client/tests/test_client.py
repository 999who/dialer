import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dialer_client.audio import TICK_N, _Source, interleave, rms  # noqa: E402
from dialer_client.detect import CallDetector  # noqa: E402


def test_source_resamples_48k_stereo_to_16k_mono():
    src = _Source(48000, 2)
    t = np.arange(48000) / 48000
    tone = (0.5 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2")
    stereo = np.stack([tone, tone], axis=1).tobytes()
    for i in range(0, len(stereo), 3840):  # 20 ms chunks like WASAPI
        src.push_int16(stereo[i:i + 3840])
    out = src.take(TICK_N)
    assert len(out) == TICK_N
    assert 0.3 < rms(out) < 0.4  # 0.5 amplitude sine -> rms 0.354


def test_source_pads_when_starved_and_caps_backlog():
    src = _Source(16000, 1)
    assert not src.take(TICK_N).any()  # silent loopback -> zeros, no blocking
    src.push_int16(np.ones(16000 * 2, dtype="<i2").tobytes())
    assert len(src.buf) <= TICK_N * 4


def test_interleave_layout():
    raw = interleave(np.array([0.5, -0.5], dtype=np.float32), np.array([0.25, 0.0], dtype=np.float32))
    pcm = np.frombuffer(raw, dtype="<i2").reshape(-1, 2)
    assert pcm[0, 0] == 16383 and pcm[0, 1] == 8191 and pcm[1, 0] == -16383


def test_detector_start_end_and_no_line():
    d = CallDetector(start_voice_s=0.6, end_silence_s=15, no_line_s=12)
    now = 0.0
    events = []
    for _ in range(10):  # 1 s of client voice
        now += 0.1
        events.append(d.update(0.0, 0.05, now))
    assert "start" in events and d.in_call
    for _ in range(130):  # operator talks, line silent 13 s
        now += 0.1
        d.update(0.05, 0.0, now)
    assert d.line_silent_s(now) >= 12
    events = []
    for _ in range(160):  # total silence
        now += 0.1
        events.append(d.update(0.0, 0.0, now))
    assert events.count("end") == 1 and not d.in_call


def test_detector_ignores_operator_only_audio():
    d = CallDetector()
    for i in range(100):
        assert d.update(0.05, 0.0, i * 0.1) is None
    assert not d.in_call


def test_normalize_server_url_accepts_what_people_type():
    from dialer_client.config import normalize_server_url as n

    assert n(' http://0.0.0.0:8000') == "ws://localhost:8000/ws"
    assert n("localhost") == "ws://localhost:8000/ws"
    assert n("192.168.1.50:9000/") == "ws://192.168.1.50:9000/ws"
    assert n("ws://localhost:8000/ws") == "ws://localhost:8000/ws"
    assert n("https://dialer.example.com") == "wss://dialer.example.com/ws"
    assert n("") == "ws://localhost:8000/ws"


def test_save_values_keeps_comments_and_other_keys(tmp_path):
    from dialer_client.config import load_config, needs_setup, save_values

    p = tmp_path / "config.toml"
    assert needs_setup(load_config(p), p)
    p.write_text('# comment\nserver_url = "ws://YOUR-SERVER:8000/ws"   # note\nmic_gain = 1.5\n', encoding="utf-8")
    assert needs_setup(load_config(p), p)
    save_values({"server_url": "ws://localhost:8000/ws", "token": 'a"b'}, p)
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# comment\n") and "mic_gain = 1.5" in text
    cfg = load_config(p)
    assert cfg.server_url == "ws://localhost:8000/ws" and cfg.token == 'a"b' and cfg.mic_gain == 1.5
    assert not needs_setup(cfg, p)


def test_broken_config_falls_back_to_defaults(tmp_path):
    from dialer_client.config import load_config

    p = tmp_path / "config.toml"
    p.write_text('server_url = "ws://x\n', encoding="utf-8")
    assert load_config(p).server_url == "ws://localhost:8000/ws"
