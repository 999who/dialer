"""What Zadarma itself is doing with sound, from the Windows audio session API.

Every app that plays or records sound owns an audio session on each device it uses.
Zadarma's sessions tell us, without any Zadarma API:
- mic_active: Zadarma has a microphone stream running (a softphone opens it for a call);
- out_active / out_peak: Zadarma is playing sound, and how loud (its own peak meter,
  other apps such as YouTube or Teams are not included).
The call detector uses these instead of the whole system output, so system sounds
can neither start a call nor be sent as the client's voice while Zadarma is quiet.

Windows only (pycaw/comtypes); elsewhere `available` stays False and the client
falls back to plain voice detection.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("zadarma")

PROCESS_MATCH = "zadarma"
SESSION_ACTIVE = 1          # AudioSessionStateActive
E_RENDER, E_CAPTURE = 0, 1  # EDataFlow
DEVICE_STATE_ACTIVE = 1


@dataclass
class ZadarmaAudio:
    available: bool = False   # session API works on this PC
    found: bool = False       # Zadarma has at least one audio session (it ran a stream at least once)
    mic_active: bool = False
    out_active: bool = False
    out_peak: float = 0.0     # 0..1, Zadarma's own output only
    out_found: bool = False   # Zadarma has a playback session we can meter
    # names of the devices Zadarma actually uses for the call (often the headset, set as the
    # Windows "communications" device, while the default output is the speakers)
    out_device: str = ""
    mic_device: str = ""


def _is_zadarma(pid: int, cache: dict[int, bool]) -> bool:
    """Zadarma itself or a helper process it started (an Electron/Chromium softphone plays
    sound from a child process that may carry a different name)."""
    hit = cache.get(pid)
    if hit is None:
        import psutil

        hit = False
        try:
            p = psutil.Process(pid)
            for _ in range(4):
                if p is None:
                    break
                if PROCESS_MATCH in p.name().lower():
                    hit = True
                    break
                p = p.parent()
        except Exception:
            pass
        cache[pid] = hit
    return hit


def _device_name(dev, cache: dict[str, str]) -> str:
    dev_id = dev.GetId()
    name = cache.get(dev_id)
    if name is None:
        try:
            from pycaw.utils import AudioUtilities

            name = AudioUtilities.CreateDevice(dev).FriendlyName or ""
        except Exception:
            name = ""
        cache[dev_id] = name
    return name


def read_zadarma_audio(_procs: dict[int, bool] = {}, _devs: dict[str, str] = {}) -> ZadarmaAudio:  # noqa: B006
    """One snapshot of Zadarma's sessions on every active playback and recording device."""
    import comtypes
    from pycaw.api.audiopolicy import IAudioSessionControl2, IAudioSessionManager2
    from pycaw.api.endpointvolume import IAudioMeterInformation
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
    from pycaw.constants import CLSID_MMDeviceEnumerator

    st = ZadarmaAudio(available=True)
    best_out = -1.0
    enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
    for flow in (E_RENDER, E_CAPTURE):
        devices = enum.EnumAudioEndpoints(flow, DEVICE_STATE_ACTIVE)
        for i in range(devices.GetCount()):
            try:
                dev = devices.Item(i)
                mgr = dev.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None) \
                    .QueryInterface(IAudioSessionManager2)
                sessions = mgr.GetSessionEnumerator()
                for j in range(sessions.GetCount()):
                    ctl = sessions.GetSession(j)
                    if ctl is None:
                        continue
                    pid = ctl.QueryInterface(IAudioSessionControl2).GetProcessId()
                    if not pid:
                        continue  # system sounds session
                    if not _is_zadarma(pid, _procs):
                        continue
                    st.found = True
                    active = ctl.GetState() == SESSION_ACTIVE
                    if flow == E_CAPTURE:
                        st.mic_active |= active
                        if active and not st.mic_device:
                            st.mic_device = _device_name(dev, _devs)
                    else:
                        st.out_found = True
                        st.out_active |= active
                        if active:
                            peak = float(ctl.QueryInterface(IAudioMeterInformation).GetPeakValue())
                            st.out_peak = max(st.out_peak, peak)
                            if peak > best_out:  # loudest device = where the call is heard
                                best_out = peak
                                st.out_device = _device_name(dev, _devs)
            except Exception as e:  # a device unplugged mid-scan etc.
                log.debug("session scan failed on a device: %s", e)
    return st


class ZadarmaAudioWatcher(threading.Thread):
    """Polls Zadarma's sessions every `interval` s and calls callback(ZadarmaAudio) from this thread.

    `gate_open()` is safe to call from the audio thread: True while Zadarma played sound
    in the last `hold_s` seconds, and always when the session API is unavailable or Zadarma
    has no session yet (so a misdetected Zadarma can never silence the client's voice).
    """

    def __init__(self, callback, interval: float = 0.1, hold_s: float = 0.6):
        super().__init__(name="zadarma-audio", daemon=True)
        self.callback, self.interval, self.hold_s = callback, interval, hold_s
        self.state = ZadarmaAudio()
        self._last_sound = 0.0
        self._stop = threading.Event()

    def gate_open(self) -> bool:
        st = self.state
        if not st.available or not st.out_found:
            return True  # can't meter Zadarma's playback: never mute the client
        return time.monotonic() - self._last_sound <= self.hold_s

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception as e:
            log.warning("audio session API unavailable (%s): call detection falls back to voice", e)
            return
        failures = 0
        while not self._stop.is_set():
            try:
                st = read_zadarma_audio()
                failures = 0
            except Exception as e:
                failures += 1
                if failures == 1:
                    log.warning("reading Zadarma audio sessions failed: %s", e)
                st = ZadarmaAudio(available=failures < 20)  # give up after ~2 s of errors
            if st.out_peak > 0.0005:
                self._last_sound = time.monotonic()
            self.state = st
            self.callback(st)
            self._stop.wait(self.interval)
