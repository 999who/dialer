"""Call detection from Zadarma's own audio streams (or, as a fallback, from voice).

Zadarma Softphone has no local API. On Windows the audio session API shows Zadarma's
own streams (see zadarma_audio.py), so:
- a call starts when Zadarma has its microphone stream open AND its own output carried voice
  for >= start_voice_s within a 3 s window; a ringtone (no microphone) and system sounds
  (YouTube, Teams, notifications: not Zadarma's) can't start a call;
- a call ends when Zadarma closes its streams for stream_off_s, or after end_silence_s of silence.
Without the session API (not Windows, pycaw missing) the line channel's voice is used instead.
'Nie słychać linii': during a call the operator speaks but the line stays silent for no_line_s.
(For exact call boundaries hook up Zadarma PBX webhooks NOTIFY_START/NOTIFY_END on the backend later.)
"""
from __future__ import annotations

import sys
import threading
import time
from collections import deque

VOICE_RMS = 0.012
ZADARMA_VOICE_PEAK = 0.02  # session peak meter is a peak, not RMS: speech sits well above this


class CallDetector:
    def __init__(self, start_voice_s: float = 0.6, end_silence_s: float = 15.0, no_line_s: float = 12.0,
                 stream_off_s: float = 2.0):
        self.start_voice_s, self.end_silence_s, self.no_line_s = start_voice_s, end_silence_s, no_line_s
        self.stream_off_s = stream_off_s
        self._stream_on = 0.0
        self.in_call = False
        self.started_at = 0.0
        self._line_hist: deque[bool] = deque(maxlen=30)  # 3 s of 100 ms ticks
        self._last_voice = 0.0
        self._last_line = 0.0
        self._mic_since_line = 0.0

    def update(self, mic_rms: float, line_rms: float, now: float | None = None, zadarma=None) -> str | None:
        """Feed one 100 ms tick. Returns 'start', 'end' or None. Check .line_silent_s for the warning.

        `zadarma`: a ZadarmaAudio snapshot when the session API works; then only Zadarma's
        streams and Zadarma's own output level count, not the whole system output.
        """
        now = time.monotonic() if now is None else now
        mic_v, line_v = mic_rms > VOICE_RMS, line_rms > VOICE_RMS
        by_zadarma = zadarma is not None and zadarma.available
        can_start = True
        if by_zadarma:
            if getattr(zadarma, "out_found", True):  # Zadarma's own playback meter
                line_v = zadarma.out_peak > ZADARMA_VOICE_PEAK
            # else: its playback isn't visible (e.g. played by a helper process): keep the loopback
            # level, the microphone check below still keeps YouTube & co. from starting a call
            if zadarma.mic_active or zadarma.out_active:
                self._stream_on = now
            # a ringtone plays without the microphone; a conversation needs it
            can_start = zadarma.mic_active
        self._line_hist.append(line_v and can_start)
        if line_v:
            self._last_line = now
            self._mic_since_line = 0.0
        elif mic_v:
            self._mic_since_line += 0.1
        if mic_v or line_v:
            self._last_voice = now
        if not self.in_call and sum(self._line_hist) * 0.1 >= self.start_voice_s:
            self.in_call, self.started_at = True, now
            self._last_voice = self._last_line = now
            return "start"
        if self.in_call and by_zadarma and now - self._stream_on >= self.stream_off_s:
            self.in_call = False  # Zadarma closed its audio: the call is over
            self._line_hist.clear()
            return "end"
        if self.in_call and now - self._last_voice >= self.end_silence_s:
            self.in_call = False
            self._line_hist.clear()
            return "end"
        return None

    def force(self, in_call: bool, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.in_call = in_call
        if in_call:
            self.started_at = self._last_voice = self._last_line = self._stream_on = now
        self._line_hist.clear()

    def line_silent_s(self, now: float | None = None) -> float:
        """Seconds the line has been silent while the operator kept talking (0 if not applicable)."""
        now = time.monotonic() if now is None else now
        if not self.in_call or self._mic_since_line < 2.0:
            return 0.0
        return now - self._last_line


def zadarma_running() -> bool | None:
    """True/False on Windows, None where we can't tell."""
    if sys.platform != "win32":
        return None
    try:
        import psutil
    except ImportError:
        return None
    for p in psutil.process_iter(["name"]):
        if "zadarma" in (p.info.get("name") or "").lower():
            return True
    return False


class ZadarmaWatcher(threading.Thread):
    def __init__(self, callback, interval: float = 5.0):
        super().__init__(name="zadarma-watch", daemon=True)
        self.callback, self.interval = callback, interval
        self.kick = threading.Event()

    def run(self) -> None:
        while True:
            self.callback(zadarma_running())
            self.kick.wait(self.interval)
            self.kick.clear()
