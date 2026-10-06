"""Call detection from audio activity + Zadarma process check.

Zadarma Softphone has no local API, so a call is inferred from the line channel:
- call starts when the client channel carries voice for >= start_voice_s within a 3 s window;
- call ends after end_silence_s of silence on both channels.
'Nie słychać linii': during a call the operator speaks but the line stays silent for no_line_s.
(For exact call boundaries hook up Zadarma PBX webhooks NOTIFY_START/NOTIFY_END on the backend later.)
"""
from __future__ import annotations

import sys
import threading
import time
from collections import deque

VOICE_RMS = 0.012


class CallDetector:
    def __init__(self, start_voice_s: float = 0.6, end_silence_s: float = 15.0, no_line_s: float = 12.0):
        self.start_voice_s, self.end_silence_s, self.no_line_s = start_voice_s, end_silence_s, no_line_s
        self.in_call = False
        self.started_at = 0.0
        self._line_hist: deque[bool] = deque(maxlen=30)  # 3 s of 100 ms ticks
        self._last_voice = 0.0
        self._last_line = 0.0
        self._mic_since_line = 0.0

    def update(self, mic_rms: float, line_rms: float, now: float | None = None) -> str | None:
        """Feed one 100 ms tick. Returns 'start', 'end' or None. Check .line_silent_s for the warning."""
        now = time.monotonic() if now is None else now
        mic_v, line_v = mic_rms > VOICE_RMS, line_rms > VOICE_RMS
        self._line_hist.append(line_v)
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
        if self.in_call and now - self._last_voice >= self.end_silence_s:
            self.in_call = False
            self._line_hist.clear()
            return "end"
        return None

    def force(self, in_call: bool, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.in_call = in_call
        if in_call:
            self.started_at = self._last_voice = self._last_line = now
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
