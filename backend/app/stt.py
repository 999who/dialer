"""Speech-to-text: per-channel VAD segmentation + NVIDIA Parakeet.

The client sends interleaved stereo PCM16 @16 kHz: L = operator mic, R = client
(WASAPI loopback). Each channel gets its own segmenter, so speaker roles come
from the channel and no diarization model is needed.

Parakeet TDT is an offline (non-streaming) model, so we transcribe whole
utterances as soon as VAD sees end-of-speech. With ~450 ms of trailing silence
plus ~50-150 ms GPU inference the transcript is ready ~0.6 s after the speaker
stops, which leaves room for RAG + Gemini inside the 1.5-2.5 s target.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import numpy as np

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


class _SileroVAD:
    frame = 512  # silero v5 needs exactly 512 samples @16 kHz

    def __init__(self, model) -> None:
        import torch

        self._torch = torch
        self.model = model
        self.model.reset_states()

    def is_speech(self, frame: np.ndarray) -> bool:
        with self._torch.no_grad():
            p = self.model(self._torch.from_numpy(frame), SR).item()
        return p > 0.5


def make_vad(engine: str):
    if engine == "silero":
        try:
            from silero_vad import load_silero_vad

            return _SileroVAD(load_silero_vad(onnx=False))
        except Exception as e:  # pragma: no cover - depends on install
            log.warning("silero-vad unavailable (%s), falling back to energy VAD", e)
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


class ParakeetSTT(STTEngine):
    """Batches utterances from all concurrent calls onto one GPU model."""

    def __init__(self, model_name: str, device: str, window_ms: int, max_batch: int):
        self.model_name = model_name
        self.device = device
        self.window = window_ms / 1000
        self.max_batch = max_batch
        self.model = None
        self._q: asyncio.Queue[tuple[np.ndarray, asyncio.Future]] = asyncio.Queue()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.model = await loop.run_in_executor(None, self._load)
        asyncio.create_task(self._worker())

    def _load(self):
        import nemo.collections.asr as nemo_asr  # heavy import, keep lazy

        log.info("loading %s on %s", self.model_name, self.device)
        m = nemo_asr.models.ASRModel.from_pretrained(self.model_name)
        m = m.to(self.device).eval()
        # warm-up so the first real call doesn't pay CUDA init
        m.transcribe([np.zeros(SR, dtype=np.float32)], batch_size=1, verbose=False)
        return m

    async def transcribe(self, audio: np.ndarray) -> str:
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
        import torch

        with torch.inference_mode():
            hyps = self.model.transcribe(audios, batch_size=len(audios), verbose=False)
        if isinstance(hyps, tuple):  # older NeMo returns (best, all)
            hyps = hyps[0]
        return [(h.text if hasattr(h, "text") else str(h)).strip() for h in hyps]


def make_stt(settings) -> STTEngine:
    if settings.stt_engine == "parakeet":
        return ParakeetSTT(settings.parakeet_model, settings.stt_device,
                           settings.stt_batch_window_ms, settings.stt_max_batch)
    return NullSTT()


def split_stereo_pcm16(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Interleaved s16le stereo -> (left, right) float32 in [-1, 1]."""
    pcm = np.frombuffer(data[: len(data) - len(data) % 4], dtype="<i2").reshape(-1, 2)
    f = pcm.astype(np.float32) / 32768.0
    return np.ascontiguousarray(f[:, 0]), np.ascontiguousarray(f[:, 1])
