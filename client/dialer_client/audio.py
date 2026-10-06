"""Two-channel capture: L = operator microphone, R = client voice (WASAPI loopback).

Windows: PyAudioWPatch (PyAudio fork with WASAPI loopback). Both devices run at
their native rates/channels; each source is down-mixed to mono and resampled
to 16 kHz with soxr, then a 100 ms wall-clock-paced mixer interleaves them
into s16le stereo frames for the WebSocket.

Why wall-clock pacing: WASAPI loopback delivers *nothing* while the output is
silent, and two sound cards drift. Each tick takes exactly 1600 samples per
channel, zero-pads a starved source and drops backlog beyond 300 ms, so the
channels stay aligned and latency stays bounded.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("audio")

SR = 16000
TICK_S = 0.1
TICK_N = int(SR * TICK_S)
MAX_BACKLOG = TICK_N * 3


@dataclass
class Device:
    index: int
    name: str
    rate: int
    channels: int
    loopback: bool


class _Source:
    """One capture stream -> mono float32 @16 kHz ring buffer."""

    def __init__(self, in_rate: int, channels: int):
        import soxr

        self.channels = channels
        self.rs = soxr.ResampleStream(in_rate, SR, 1, dtype="float32", quality="HQ") if in_rate != SR else None
        self.buf = np.zeros(0, dtype=np.float32)
        self.lock = threading.Lock()
        self.last_data = 0.0

    def push_int16(self, raw: bytes) -> None:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if self.channels > 1:
            x = x.reshape(-1, self.channels).mean(axis=1)
        if self.rs is not None:
            x = self.rs.resample_chunk(x)
        with self.lock:
            self.buf = np.concatenate([self.buf, x])
            if len(self.buf) > MAX_BACKLOG + TICK_N:
                self.buf = self.buf[-(MAX_BACKLOG + TICK_N):]
        self.last_data = time.monotonic()

    def take(self, n: int) -> np.ndarray:
        with self.lock:
            out, self.buf = self.buf[:n], self.buf[n:]
        if len(out) < n:
            out = np.concatenate([out, np.zeros(n - len(out), dtype=np.float32)])
        return out


def interleave(left: np.ndarray, right: np.ndarray) -> bytes:
    st = np.empty((len(left), 2), dtype="<i2")
    st[:, 0] = np.clip(left * 32767, -32768, 32767)
    st[:, 1] = np.clip(right * 32767, -32768, 32767)
    return st.tobytes()


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


class AudioCapture:
    """Calls on_frame(bytes, mic_rms, line_rms) every 100 ms from a worker thread."""

    def __init__(self, on_frame, mic_name: str = "", line_name: str = "", mic_gain: float = 1.0,
                 line_gain: float = 1.0):
        self.on_frame = on_frame
        self.mic_name, self.line_name = mic_name, line_name
        self.mic_gain, self.line_gain = mic_gain, line_gain
        self._pa = None
        self._streams = []
        self._mic: _Source | None = None
        self._line: _Source | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.mic_device: Device | None = None
        self.line_device: Device | None = None
        # callable -> bool; False mutes the line channel (Zadarma is silent, so whatever the
        # loopback hears is some other app's sound, not the client)
        self.line_gate = None

    # ---------------------------------------------------------------- devices
    @staticmethod
    def list_devices() -> tuple[list[Device], list[Device]]:
        """(microphones, loopback outputs) on the WASAPI host API."""
        import pyaudiowpatch as pyaudio

        pa = pyaudio.PyAudio()
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
            mics, loops = [], []
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                if d["hostApi"] != wasapi or d["maxInputChannels"] < 1:
                    continue
                dev = Device(i, d["name"], int(d["defaultSampleRate"]), int(d["maxInputChannels"]),
                             bool(d.get("isLoopbackDevice")))
                (loops if dev.loopback else mics).append(dev)
            return mics, loops
        finally:
            pa.terminate()

    def _pick(self, pa, pyaudio):
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        mics, loops = self.list_devices()
        if not mics or not loops:
            raise RuntimeError("no WASAPI microphone or loopback device")

        def find(devs, name, default_name):
            for want in (name, default_name):
                if want:
                    for d in devs:
                        if want.lower() in d.name.lower():
                            return d
            return devs[0]

        default_in = pa.get_device_info_by_index(wasapi["defaultInputDevice"])["name"]
        default_out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
        # loopback devices are named "<output name> [Loopback]"
        return find(mics, self.mic_name, default_in), find(loops, self.line_name, default_out)

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        import pyaudiowpatch as pyaudio

        self._pa = pyaudio.PyAudio()
        mic, line = self._pick(self._pa, pyaudio)
        self.mic_device, self.line_device = mic, line
        log.info("mic: %s (%d Hz, %d ch) | line: %s (%d Hz, %d ch)", mic.name, mic.rate, mic.channels,
                 line.name, line.rate, line.channels)
        self._mic = _Source(mic.rate, mic.channels)
        self._line = _Source(line.rate, line.channels)
        for dev, src in ((mic, self._mic), (line, self._line)):
            def cb(data, frames, info, status, src=src):
                src.push_int16(data)
                return None, pyaudio.paContinue

            s = self._pa.open(format=pyaudio.paInt16, channels=dev.channels, rate=dev.rate, input=True,
                              input_device_index=dev.index, frames_per_buffer=int(dev.rate * 0.02),
                              stream_callback=cb)
            s.start_stream()
            self._streams.append(s)
        self._stop.clear()
        self._thread = threading.Thread(target=self._mix_loop, name="audio-mixer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        for s in self._streams:
            try:
                s.stop_stream()
                s.close()
            except Exception:
                pass
        self._streams.clear()
        if self._pa:
            self._pa.terminate()
            self._pa = None

    def restart(self, mic_name: str | None = None, line_name: str | None = None) -> None:
        if mic_name is not None:
            self.mic_name = mic_name
        if line_name is not None:
            self.line_name = line_name
        self.stop()
        self.start()

    def _mix_loop(self) -> None:
        nxt = time.monotonic()
        while not self._stop.is_set():
            nxt += TICK_S
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.monotonic()  # fell behind (sleep/hibernate): resync
            left = self._mic.take(TICK_N) * self.mic_gain
            right = self._line.take(TICK_N) * self.line_gain
            gate = self.line_gate
            if gate is not None and not gate():
                right = np.zeros_like(right)
            try:
                self.on_frame(interleave(left, right), rms(left), rms(right))
            except Exception:
                log.exception("on_frame failed")
