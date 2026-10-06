"""Speech-to-text: per-channel VAD segmentation + NVIDIA Parakeet on CPU.

The client sends interleaved stereo PCM16 @16 kHz: L = operator mic, R = client
(WASAPI loopback). Each channel gets its own segmenter, so speaker roles come
from the channel and no diarization model is needed.

No GPU and no torch: same approach as 999who/recorder_fork (feat/parakeet-engine).
- Parakeet TDT 0.6B v3 (multilingual, incl. Polish) runs through onnx-asr on
  onnxruntime, CPU, int8. Cost grows with clip length, so short utterances are cheap.
- Silero VAD runs from its ONNX file (backend/models/silero_vad.onnx) on onnxruntime.

Parakeet TDT is an offline (non-streaming) model, so we transcribe whole
utterances as soon as VAD sees end-of-speech (~450 ms of trailing silence).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .langfilter import is_foreign_language

log = logging.getLogger("stt")

SR = 16000


@dataclass
class Segment:
    speaker: str            # "operator" | "client"
    audio: np.ndarray       # float32 mono 16 kHz
    t_start: float          # seconds since call start
    t_end: float
    ended_at: float = field(default_factory=time.monotonic)  # wall clock for latency


# --------------------------------------------------------------------------- VAD
class _EnergyVAD:
    """Dependency-free fallback: adaptive energy threshold on 32 ms frames."""

    frame = 512

    def __init__(self) -> None:
        self.noise = 1e-4

    def is_speech(self, frame: np.ndarray) -> bool:
        e = float(np.sqrt(np.mean(frame * frame)) + 1e-9)
        speech = e > max(self.noise * 3.0, 0.006)
        if not speech:
            self.noise = 0.95 * self.noise + 0.05 * e
        return speech


MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
_silero_session = None
_silero_lock = threading.Lock()


def _silero():
    """One shared onnxruntime session; every detector keeps its own recurrent state."""
    global _silero_session
    with _silero_lock:
        if _silero_session is None:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            _silero_session = ort.InferenceSession(str(MODELS_DIR / "silero_vad.onnx"), sess_options=opts,
                                                   providers=["CPUExecutionProvider"])
        return _silero_session


class _SileroVAD:
    """Silero VAD v5 via onnxruntime: 512-sample windows @16 kHz with 64 samples of context."""

    frame = 512
    _ctx = 64

    def __init__(self, threshold: float = 0.5) -> None:
        self.session = _silero()
        self.threshold = threshold
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self._ctx), dtype=np.float32)
        self._sr = np.array(SR, dtype=np.int64)

    def prob(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._context, frame.reshape(1, -1).astype(np.float32)], axis=1)
        out, self._state = self.session.run(["output", "stateN"], {"input": x, "state": self._state, "sr": self._sr})
        self._context = x[:, -self._ctx:]
        return float(out[0][0])

    def is_speech(self, frame: np.ndarray) -> bool:
        return self.prob(frame) >= self.threshold


def make_vad(engine: str):
    if engine == "silero":
        try:
            return _SileroVAD()
        except Exception as e:  # pragma: no cover - depends on install
            log.warning("silero VAD (onnx) unavailable (%s), falling back to energy VAD", e)
    return _EnergyVAD()


class ChannelSegmenter:
    """Cuts one audio channel into utterances."""

    def __init__(self, speaker: str, vad, min_silence_ms: int, max_utt_s: float, min_utt_ms: int):
        self.speaker = speaker
        self.vad = vad
        self.frame = vad.frame
        self.min_silence_frames = max(1, int(min_silence_ms / 1000 * SR / self.frame))
        self.max_frames = int(max_utt_s * SR / self.frame)
        self.min_frames = max(1, int(min_utt_ms / 1000 * SR / self.frame))
        self.pad_frames = 3  # ~100 ms pre-roll so first phoneme isn't clipped
        self._buf = np.zeros(0, dtype=np.float32)
        self._pre: list[np.ndarray] = []
        self._cur: list[np.ndarray] = []
        self._silence = 0
        self._samples_seen = 0
        self._utt_start = 0
        self.level = 0.0  # last RMS, for meters/diagnostics

    def feed(self, audio: np.ndarray) -> list[Segment]:
        out: list[Segment] = []
        self._buf = np.concatenate([self._buf, audio])
        while len(self._buf) >= self.frame:
            fr, self._buf = self._buf[: self.frame], self._buf[self.frame :]
            self.level = float(np.sqrt(np.mean(fr * fr)))
            speech = self.vad.is_speech(fr)
            pos = self._samples_seen
            self._samples_seen += self.frame
            if self._cur:
                self._cur.append(fr)
                self._silence = 0 if speech else self._silence + 1
                if self._silence >= self.min_silence_frames or len(self._cur) >= self.max_frames:
                    seg = self._close()
                    if seg:
                        out.append(seg)
            elif speech:
                self._utt_start = pos - len(self._pre) * self.frame
                self._cur = self._pre + [fr]
                self._pre = []
                self._silence = 0
            else:
                self._pre = (self._pre + [fr])[-self.pad_frames :]
        return out

    def flush(self) -> list[Segment]:
        seg = self._close() if self._cur else None
        return [seg] if seg else []

    def _close(self) -> Segment | None:
        frames = self._cur[: len(self._cur) - max(0, self._silence - 2)]  # trim most trailing silence
        self._cur, self._silence = [], 0
        if len(frames) < self.min_frames:
            return None
        audio = np.concatenate(frames)
        t0 = self._utt_start / SR
        return Segment(self.speaker, audio, t0, t0 + len(audio) / SR)


# --------------------------------------------------------------------------- STT engines
class STTEngine:
    async def transcribe(self, audio: np.ndarray) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    async def start(self) -> None:
        pass


class NullSTT(STTEngine):
    """Text-only mode: audio is ignored, the client injects text (for tests/demo)."""

    async def transcribe(self, audio: np.ndarray) -> str:
        return ""


PARAKEET_MODEL = "nemo-parakeet-tdt-0.6b-v3"
MIN_AUDIO_S = 0.4      # shorter clips are noise
MAX_CHUNK_S = 20.0     # safe length for one model call


def highpass(audio: np.ndarray, cutoff_hz: float = 80.0) -> np.ndarray:
    """Removes desk rumble / 50 Hz hum without touching the speech band."""
    if len(audio) < 16:
        return audio.astype(np.float32)
    from scipy.signal import butter, sosfilt

    sos = butter(2, cutoff_hz, btype="highpass", fs=SR, output="sos")
    return np.nan_to_num(sosfilt(sos, audio)).astype(np.float32)


def normalize(audio: np.ndarray, target_peak: float = 0.92) -> np.ndarray:
    """Peak-normalise quiet speech (up to x8), never amplify loud audio."""
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if 0.0001 < peak < 0.75:
        return (audio * min(target_peak / peak, 8.0)).astype(np.float32)
    if peak >= 1.0:
        return (audio / (peak + 1e-6) * target_peak).astype(np.float32)
    return audio.astype(np.float32)


def prepare_audio(audio: np.ndarray) -> np.ndarray:
    return normalize(highpass(audio))


class ParakeetSTT(STTEngine):
    """Parakeet TDT 0.6B v3 through onnx-asr: onnxruntime, CPU only, int8.

    Utterances from all concurrent calls go through one model, in small batches.
    """

    def __init__(self, threads: int, model_path: str, window_ms: int, max_batch: int, polish_only: bool = True):
        self.threads = max(1, int(threads))
        self.model_path = (model_path or "").strip()
        self.window = window_ms / 1000
        self.max_batch = max_batch
        self.polish_only = polish_only
        self.model = None
        self._q: asyncio.Queue[tuple[np.ndarray, asyncio.Future]] = asyncio.Queue()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.model = await loop.run_in_executor(None, self._load)
        asyncio.create_task(self._worker())

    def _load(self):
        import onnx_asr
        import onnxruntime as ort

        if self.model_path and not Path(self.model_path).is_dir():
            raise RuntimeError(f"PARAKEET_MODEL_PATH not found: {self.model_path}")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.threads
        opts.inter_op_num_threads = 1
        log.info("loading %s (int8, CPU, %d threads)%s", PARAKEET_MODEL, self.threads,
                 f" from {self.model_path}" if self.model_path else ", downloading from Hugging Face on first run")
        m = onnx_asr.load_model(PARAKEET_MODEL, self.model_path or None, quantization="int8",
                                sess_options=opts, providers=["CPUExecutionProvider"])
        m.recognize(np.zeros(SR, dtype=np.float32), sample_rate=SR)  # warm-up
        return m

    async def transcribe(self, audio: np.ndarray) -> str:
        if len(audio) < int(MIN_AUDIO_S * SR):
            return ""
        fut = asyncio.get_running_loop().create_future()
        await self._q.put((audio, fut))
        return await fut

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            batch = [await self._q.get()]
            deadline = loop.time() + self.window
            while len(batch) < self.max_batch:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._q.get(), timeout))
                except asyncio.TimeoutError:
                    break
            audios = [a for a, _ in batch]
            try:
                texts = await loop.run_in_executor(None, self._run, audios)
                for (_, fut), text in zip(batch, texts):
                    if not fut.done():
                        fut.set_result(text)
            except Exception as e:  # keep the worker alive
                log.exception("parakeet batch failed")
                for _, fut in batch:
                    if not fut.done():
                        fut.set_exception(e)

    def _run(self, audios: list[np.ndarray]) -> list[str]:
        # utterances are capped by VAD (vad_max_utterance_s), but guard the model limit anyway
        clips = [prepare_audio(a[: int(MAX_CHUNK_S * SR)]) for a in audios]
        texts = self.model.recognize(clips, sample_rate=SR)
        return [self._postprocess(t) for t in texts]

    def _postprocess(self, text: str) -> str:
        text = " ".join((text or "").split())
        # v3 is multilingual and can't be forced to Polish; on quiet/unclear clips it sometimes
        # returns short English phrases ("Yeah.", "Okay.") - drop those
        if self.polish_only and is_foreign_language(text):
            return ""
        return text


def make_stt(settings) -> STTEngine:
    if settings.stt_engine == "parakeet":
        return ParakeetSTT(settings.parakeet_threads, settings.parakeet_model_path,
                           settings.stt_batch_window_ms, settings.stt_max_batch, settings.stt_polish_only)
    return NullSTT()


def split_stereo_pcm16(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Interleaved s16le stereo -> (left, right) float32 in [-1, 1]."""
    pcm = np.frombuffer(data[: len(data) - len(data) % 4], dtype="<i2").reshape(-1, 2)
    f = pcm.astype(np.float32) / 32768.0
    return np.ascontiguousarray(f[:, 0]), np.ascontiguousarray(f[:, 1])
