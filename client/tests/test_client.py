import sys
import time
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


def test_save_values_keeps_comments_and_other_keys(tmp_path):
    from dialer_client.config import load_config, needs_setup, save_values

    p = tmp_path / "config.toml"
    assert needs_setup(load_config(p))
    p.write_text('# comment\nserver_url = "ws://YOUR-SERVER:8000/ws"   # old\nmic_gain = 1.5\n', encoding="utf-8")
    assert needs_setup(load_config(p))
    save_values({"gemini_api_key": 'a"b', "gemini_model": "gemini-3.5-flash-lite"}, p)
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# comment\n") and "mic_gain = 1.5" in text
    cfg = load_config(p)
    assert cfg.gemini_api_key == 'a"b' and cfg.gemini_model == "gemini-3.5-flash-lite" and cfg.mic_gain == 1.5
    assert not needs_setup(cfg)


def test_broken_config_falls_back_to_defaults(tmp_path):
    from dialer_client.config import load_config

    p = tmp_path / "config.toml"
    p.write_text('gemini_model = "x\n', encoding="utf-8")
    assert load_config(p).gemini_model == "gemini-3.8-flash"


def _z(mic=False, out=False, peak=0.0):
    from dialer_client.zadarma_audio import ZadarmaAudio

    return ZadarmaAudio(available=True, found=True, mic_active=mic, out_active=out, out_peak=peak, out_found=True)


def test_system_sound_does_not_start_a_call_when_zadarma_is_idle():
    d = CallDetector()
    for i in range(100):  # YouTube on the loopback, Zadarma has no streams
        assert d.update(0.0, 0.2, i * 0.1, zadarma=_z()) is None
    for i in range(100, 200):  # ringtone: Zadarma plays, microphone closed
        assert d.update(0.0, 0.2, i * 0.1, zadarma=_z(out=True, peak=0.5)) is None
    assert not d.in_call


def test_zadarma_streams_start_and_end_the_call():
    d = CallDetector()
    now, events = 0.0, []
    for _ in range(10):  # answered: mic open, client says "Halo"
        now += 0.1
        events.append(d.update(0.0, 0.2, now, zadarma=_z(mic=True, out=True, peak=0.3)))
    assert events.count("start") == 1 and d.in_call
    for _ in range(50):  # 5 s pause in the conversation, streams still open
        now += 0.1
        assert d.update(0.0, 0.0, now, zadarma=_z(mic=True, out=True)) is None
    events = []
    for _ in range(30):  # hang up: Zadarma closes its streams
        now += 0.1
        events.append(d.update(0.0, 0.0, now, zadarma=_z()))
    assert events.count("end") == 1 and not d.in_call


def test_line_gate_mutes_other_apps_only_while_zadarma_is_silent():
    from dialer_client.zadarma_audio import ZadarmaAudio, ZadarmaAudioWatcher

    w = ZadarmaAudioWatcher(lambda st: None)
    assert w.gate_open()  # nothing known yet
    w.state = ZadarmaAudio(available=True, found=False)
    assert w.gate_open()  # Zadarma not seen: never mute
    w.state = ZadarmaAudio(available=True, found=True, mic_active=True)
    assert w.gate_open()  # only Zadarma's microphone is visible, its playback isn't: never mute
    w.state = _z(mic=True, out=True)
    assert not w.gate_open()  # Zadarma silent -> loopback is someone else's sound
    w._last_sound = time.monotonic()
    assert w.gate_open()


def test_read_zadarma_audio_on_windows():
    """Exercises the real COM calls; CI runs this on windows-latest."""
    import pytest

    if sys.platform != "win32":
        pytest.skip("Windows audio session API")
    import comtypes

    from dialer_client.zadarma_audio import read_zadarma_audio

    comtypes.CoInitialize()
    st = read_zadarma_audio()
    assert st.available and not st.mic_active  # no Zadarma on the CI runner


def test_gemini_key_is_cleaned(tmp_path):
    from dialer_client.config import load_config, save_values

    p = tmp_path / "config.toml"
    save_values({"gemini_api_key": ' "AIzaXYZ" '}, p)
    assert load_config(p).gemini_api_key == "AIzaXYZ"


def test_follows_zadarma_devices_during_a_call():
    from types import SimpleNamespace

    from dialer_client.app import DialerApp

    calls = []
    audio = SimpleNamespace(line_device=SimpleNamespace(name="Głośniki (Realtek) [Loopback]"),
                            mic_device=SimpleNamespace(name="Mikrofon (Realtek)"),
                            restart=lambda **kw: calls.append(kw))
    fake = SimpleNamespace(audio=audio, cfg=SimpleNamespace(line_device="", mic_device=""),
                           qs=SimpleNamespace(value=lambda k, d="": ""), overlay=SimpleNamespace(
                               clear_error=lambda k: None, show_error=lambda *a: None))
    fake._auto_device = lambda key: DialerApp._auto_device(fake, key)
    st = _z(mic=True, out=True, peak=0.3)
    st.out_device, st.mic_device = "Słuchawki (Jabra Evolve2)", "Mikrofon (Jabra Evolve2)"
    DialerApp._follow_zadarma_devices(fake, _z())  # no call yet: nothing changes
    DialerApp._follow_zadarma_devices(fake, st)
    DialerApp._follow_zadarma_devices(fake, st)  # same switch is not retried
    assert calls == [{"line_name": "Słuchawki (Jabra Evolve2)", "mic_name": "Mikrofon (Jabra Evolve2)"}]


def test_call_starts_by_loopback_when_zadarma_playback_is_not_metered():
    from dialer_client.zadarma_audio import ZadarmaAudio

    d = CallDetector()
    z = ZadarmaAudio(available=True, found=True, mic_active=True)  # only the microphone session is visible
    events = [d.update(0.0, 0.05, i * 0.1, zadarma=z) for i in range(10)]
    assert "start" in events


def test_update_swaps_the_running_exe_and_cleans_up(tmp_path, monkeypatch):
    from dialer_client import updater

    exe = tmp_path / "EmanagerDialer.exe"
    exe.write_bytes(b"old")
    new = tmp_path / "EmanagerDialer.download"
    new.write_bytes(b"new")
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    updater.install(new)
    assert exe.read_bytes() == b"new" and (tmp_path / "EmanagerDialer.old").read_bytes() == b"old"
    updater.cleanup()
    assert not (tmp_path / "EmanagerDialer.old").exists()


def test_release_is_newer_only_for_a_different_build(monkeypatch):
    from dialer_client import updater

    rel = updater.Release("abc", "u", 1, "")
    monkeypatch.setattr(updater, "BUILD", "")
    assert not rel.newer  # running from source: never offer
    monkeypatch.setattr(updater, "BUILD", "abc")
    assert not rel.newer
    monkeypatch.setattr(updater, "BUILD", "def")
    assert rel.newer


def test_model_download_progress_counts_cache_bytes(tmp_path):
    from dialer_client.model_download import DownloadProgress

    dl = DownloadProgress(tmp_path, fetch_total=False)
    dl.total = 1000
    (tmp_path / "blobs").mkdir()
    part = tmp_path / "blobs" / "abc.incomplete"
    part.write_bytes(b"x" * 300)
    frac, label = dl.status()
    assert frac == 0.3 and label.endswith("MB")
    part.write_bytes(b"x" * 1000)  # finished
    assert dl.status() is None


def test_model_already_downloaded_shows_no_progress(tmp_path):
    from dialer_client.model_download import DownloadProgress

    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / "encoder-model.int8.onnx").write_bytes(b"x" * 500)
    dl = DownloadProgress(tmp_path, fetch_total=False)
    dl.total = 10_000
    assert dl.status() is None  # nothing new arrives: only loading from disk


def test_stop_and_pause_buttons_and_clearing_between_calls():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    from dialer_client.overlay import Overlay

    app = QApplication.instance() or QApplication([])  # noqa: F841
    ov = Overlay()
    stops, pauses = [], []
    ov.stop_requested.connect(lambda: stops.append(1))
    ov.pause_toggled.connect(pauses.append)
    ov.set_call(True, 5)
    assert not ov.bar.stop_btn.isHidden()
    ov.bar.pause_btn.click()
    assert pauses == [True] and ov.bar.status_lbl.text() == "wstrzymano" and ov.bar.meters.isHidden()
    ov.set_paused(False)
    assert pauses == [True, False] and ov.bar.status_lbl.isHidden()
    ov.bar.stop_btn.click()
    assert stops == [1]
    ov.add_transcript(1, "client", "Dzień dobry")
    ov.show_summary({"duration_s": 10, "summary": "x"})
    ov.clear_screen()
    assert not ov.panel.transcript.rows and not ov.cards
    ov.set_call(False)
    assert ov.bar.stop_btn.isHidden()


def test_local_server_reports_the_real_startup_error_not_systemexit(monkeypatch):
    import logging

    from dialer_client import local_server
    from dialer_client.config import Config

    srv = local_server.LocalServer(Config(gemini_api_key="", local_stt="none"))

    class FakeServer:
        def __init__(self, cfg):
            self.should_exit = False

        async def startup(self, sockets=None):
            pass

        def run(self):  # what uvicorn does when the app's startup raises
            try:
                raise ConnectionRefusedError("[Errno 111] Connect call failed ('db.example', 5432)")
            except ConnectionRefusedError:
                logging.getLogger("uvicorn.error").exception("Application startup failed. Exiting.")
            raise SystemExit(3)

    import uvicorn
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.syspath_prepend(str(local_server.BACKEND_DIR))
    srv._run()
    assert srv.state == "error" and srv.error.startswith("ConnectionRefusedError")
