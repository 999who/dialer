"""Wires audio capture, call detection, backend link and the overlay together."""
from __future__ import annotations

import logging
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QDir, QLockFile, QObject, QPoint, QSettings, QStandardPaths, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QDesktopServices
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox, QSystemTrayIcon

from . import theme as T
from .config import CONFIG_PATH, Config, load_config, needs_setup, save_values
from .connect_dialog import ConnectDialog
from .detect import CallDetector, ZadarmaWatcher
from .net import BackendLink
from .overlay import CORNERS, Overlay

log = logging.getLogger("app")


class Bridge(QObject):
    """Thread -> Qt main thread."""
    frame = pyqtSignal(bytes, float, float)
    zadarma = pyqtSignal(object)


class DialerApp(QObject):
    def __init__(self, cfg: Config, audio_factory=None):
        super().__init__()
        self.cfg = cfg
        self.qs = QSettings("EMANAGER", "Dialer")
        corner = self.qs.value("corner", cfg.corner)
        self.overlay = Overlay(corner, cfg.hint_seconds, cfg.summary_seconds, cfg.opacity, cfg.show_in_taskbar)
        self.link = BackendLink(cfg.server_url, cfg.token, cfg.agent_id, cfg.offline_buffer_s)
        self.detector = CallDetector(end_silence_s=cfg.call_end_silence_s, no_line_s=cfg.no_line_warning_s)
        self.bridge = Bridge()
        self.preroll: deque[bytes] = deque(maxlen=30)  # 3 s, so the first words aren't lost
        self.transcript: list[tuple[float, str, str]] = []
        self.zadarma_ok: bool | None = None
        self.manual_call = False

        self.bridge.frame.connect(self._on_frame)
        self.bridge.zadarma.connect(self._on_zadarma)
        self.link.message.connect(self._on_message)
        self.link.online_changed.connect(self._on_online)
        self.link.retry_in.connect(lambda s: self.overlay.show_error("server", f"ponowna próba za {s} s"))
        self.link.auth_failed.connect(self._on_auth_failed)
        ov = self.overlay
        ov.hint_copied.connect(lambda hid: self.link.send({"type": "feedback", "hint_id": hid, "copied": True}))
        ov.hint_feedback.connect(lambda hid, u: self.link.send({"type": "feedback", "hint_id": hid, "useful": u}))
        ov.pause_toggled.connect(self.link.set_paused)
        ov.error_action.connect(self._on_error_action)
        ov.settings_requested.connect(self._settings_menu)
        ov.save_crm.connect(self._save_crm)
        ov.open_transcript.connect(self._open_transcript)

        self.clock = QTimer(self, interval=1000, timeout=self._on_clock)
        self.clock.start()

        self.audio = (audio_factory or self._make_audio)()
        self.watcher = ZadarmaWatcher(self.bridge.zadarma.emit)
        self._setup_tray()

    # ---------------------------------------------------------------- startup
    def _make_audio(self):
        from .audio import AudioCapture

        return AudioCapture(lambda f, m, l: self.bridge.frame.emit(f, m, l),
                            self.qs.value("mic_device", self.cfg.mic_device),
                            self.qs.value("line_device", self.cfg.line_device),
                            self.cfg.mic_gain, self.cfg.line_gain)

    def start(self) -> None:
        self.overlay.show()
        self.link.connect_now()
        self.watcher.start()
        try:
            self.audio.start()
            self.overlay.clear_error("no_audio")
        except Exception as e:
            log.exception("audio start failed")
            self.overlay.show_error("no_audio", str(e)[:40])

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(T.icon("logo_full", T.BRAND, 32), self)
        self.tray.setToolTip("EMANAGER Dialer")
        self.tray.activated.connect(lambda *_: (self.overlay.showNormal(), self.overlay.reposition()))
        menu = QMenu()
        menu.addAction("Pokaż", lambda: (self.overlay.showNormal(), self.overlay.reposition()))
        menu.addAction("Połączenie z serwerem…", lambda: self.open_connection_settings())
        menu.addAction("Zamknij", QApplication.quit)
        self.tray.setContextMenu(menu)
        self.tray.show()

    # ---------------------------------------------------------------- audio / call
    def _on_frame(self, frame: bytes, mic: float, line: float) -> None:
        self.overlay.set_levels(mic, line)
        ev = self.detector.update(mic, line)
        if ev == "start" and self.cfg.require_zadarma and self.zadarma_ok is False:
            self.detector.force(False)  # sound without Zadarma (YouTube, Teams…) is not a call
            ev = None
        if ev == "start":
            self._call_started()
        self.link.send_audio(frame)
        if not self.link.in_call:
            self.preroll.append(frame)
        if ev == "end":
            self._call_ended()
        silent = self.detector.line_silent_s()
        if silent >= self.cfg.no_line_warning_s:
            self.overlay.show_error("no_line", f"{int(silent)} s ciszy")
        elif "no_line" in self.overlay.errors and silent == 0:
            self.overlay.clear_error("no_line")

    def _call_started(self) -> None:
        log.info("call started")
        self.transcript.clear()
        self.overlay.clear_transcript()
        self.link.call_start("")
        for f in self.preroll:
            self.link.send_audio(f)
        self.preroll.clear()
        self.overlay.set_call(True, 0)

    def _call_ended(self) -> None:
        log.info("call ended")
        self.manual_call = False
        self.link.call_end()
        self.overlay.set_call(False)
        self.overlay.clear_error("no_line")

    def _on_clock(self) -> None:
        if self.detector.in_call:
            self.overlay.set_call(True, time.monotonic() - self.detector.started_at)

    def _on_zadarma(self, running) -> None:
        self.zadarma_ok = running
        if running is False and self.cfg.require_zadarma:
            self.overlay.show_error("no_zadarma")
        else:
            self.overlay.clear_error("no_zadarma")

    # ---------------------------------------------------------------- backend
    def _on_online(self, online: bool) -> None:
        self.overlay.set_online(online, self.detector.in_call)
        if online:
            self.overlay.clear_error("server")

    def _on_auth_failed(self) -> None:
        self.overlay.clear_error("server")
        self.overlay.show_error("auth")

    def open_connection_settings(self, reason: str = "") -> bool:
        dlg = ConnectDialog(self.cfg.server_url, self.cfg.token, self.cfg.agent_id, reason)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        self.cfg.server_url, self.cfg.token = dlg.url, dlg.token
        try:
            save_values({"server_url": dlg.url, "token": dlg.token})
        except OSError as e:
            log.error("cannot save %s: %s", CONFIG_PATH, e)
            QMessageBox.warning(None, "EMANAGER Dialer", f"Nie udało się zapisać {CONFIG_PATH}:\n{e}")
        self.overlay.clear_error("auth")
        self.link.set_target(dlg.url, dlg.token)
        return True

    def _on_message(self, m: dict) -> None:
        t = m.get("type")
        if t == "transcript":
            self.transcript.append((m.get("t", 0), m["speaker"], m["text"]))
            self.overlay.add_transcript(m.get("t", 0), m["speaker"], m["text"])
        elif t == "hint":
            self.overlay.show_hint(m)
        elif t == "latency":
            self.overlay.set_latency(int(m["ms"]))
        elif t == "call_summary":
            m["transcript"] = list(self.transcript)
            self.overlay.show_summary(m)
        elif t == "error":
            log.error("backend: %s", m)

    # ---------------------------------------------------------------- actions
    def _on_error_action(self, kind: str) -> None:
        if kind == "server":
            self.link.connect_now()
        elif kind == "auth":
            self.open_connection_settings()
        elif kind == "no_zadarma":
            self.watcher.kick.set()
        elif kind in ("no_line", "no_audio"):
            self._settings_menu(self.overlay.bar.mapToGlobal(QPoint(0, 0)), devices_only=True)

    def _settings_menu(self, pos: QPoint, devices_only: bool = False) -> None:
        menu = QMenu()
        menu.setStyleSheet(f"QMenu{{background:{T.SURFACE};color:{T.TEXT};border:1px solid {T.BORDER};padding:6px;}}"
                           f"QMenu::item{{padding:6px 18px;border-radius:6px;}}"
                           f"QMenu::item:selected{{background:{T.SURFACE2};}}"
                           f"QMenu::separator{{height:1px;background:{T.BORDER};margin:4px 8px;}}")
        try:
            from .audio import AudioCapture
            mics, loops = AudioCapture.list_devices()
        except Exception:
            mics, loops = [], []
        for title, devs, key in (("Mikrofon konsultanta", mics, "mic_device"),
                                 ("Dźwięk rozmówcy (wyjście Zadarma)", loops, "line_device")):
            sub = menu.addMenu(title)
            cur = getattr(self.audio, f"{key.split('_')[0]}_device", None)
            group = QActionGroup(sub)
            for d in devs:
                a = QAction(d.name.replace(" [Loopback]", ""), sub, checkable=True)
                a.setChecked(bool(cur and cur.index == d.index))
                a.triggered.connect(lambda _=False, k=key, n=d.name: self._set_device(k, n))
                group.addAction(a)
                sub.addAction(a)
            if not devs:
                sub.addAction("(brak urządzeń)").setEnabled(False)
        if not devices_only:
            sub = menu.addMenu("Położenie panelu")
            group = QActionGroup(sub)
            for key, label in CORNERS.items():
                a = QAction(label, sub, checkable=True)
                a.setChecked(self.overlay.corner == key)
                a.triggered.connect(lambda _=False, k=key: self._set_corner(k))
                group.addAction(a)
                sub.addAction(a)
            menu.addAction("Połączenie z serwerem…", lambda: self.open_connection_settings())
            menu.addAction("Pokaż dziennik (log)", lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(_log_dir() / "dialer.log"))))
            menu.addSeparator()
            if self.detector.in_call:
                menu.addAction("Zakończ rozmowę", lambda: (self.detector.force(False), self._call_ended()))
            else:
                menu.addAction("Rozpocznij rozmowę ręcznie", self._manual_start)
            menu.addSeparator()
            menu.addAction("Zamknij EMANAGER Dialer", QApplication.quit)
        menu.exec(pos)

    def _manual_start(self) -> None:
        self.manual_call = True
        self.detector.force(True)
        self._call_started()

    def _set_device(self, key: str, name: str) -> None:
        self.qs.setValue(key, name)
        try:
            self.audio.restart(**{"mic_name" if key == "mic_device" else "line_name": name})
            self.overlay.clear_error("no_audio")
            self.overlay.clear_error("no_line")
        except Exception as e:
            self.overlay.show_error("no_audio", str(e)[:40])

    def _set_corner(self, corner: str) -> None:
        self.qs.setValue("corner", corner)
        self.overlay.set_corner(corner)

    def _save_crm(self, data: dict) -> None:
        # No CRM API yet: put a ready-to-paste note on the clipboard.
        lines = [f"Rozmowa {T.fmt_seconds(data.get('duration_s', 0))}", data.get("summary", "")]
        QApplication.clipboard().setText("\n".join(l for l in lines if l))
        self.tray.showMessage("EMANAGER Dialer", "Podsumowanie skopiowane — wklej je w CRM.",
                              QSystemTrayIcon.MessageIcon.Information, 3000)

    def _open_transcript(self, data: dict) -> None:
        rows = data.get("transcript") or self.transcript
        text = "\n".join(f"{T.fmt_seconds(t)}  {'KONSULTANT' if s == 'operator' else 'KLIENT'}: {x}"
                         for t, s, x in rows)
        if data.get("summary"):
            text = f"PODSUMOWANIE: {data['summary']}\n\n{text}"
        path = Path(tempfile.gettempdir()) / f"emanager_rozmowa_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        path.write_text(text, encoding="utf-8")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def _log_dir() -> Path:
    d = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
             or tempfile.gettempdir())
    d.mkdir(parents=True, exist_ok=True)
    return d


def _setup_logging() -> Path:
    path = _log_dir() / "dialer.log"
    handlers: list[logging.Handler] = [logging.FileHandler(path, mode="w", encoding="utf-8")]
    if sys.stderr is not None:  # EmanagerDialer.exe has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    return path


def _set_app_id() -> None:
    """Own taskbar identity on Windows (icon and grouping), instead of python.exe's."""
    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("EMANAGER.Dialer")
        except Exception:
            pass


def main() -> int:
    _set_app_id()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("EMANAGER Dialer")
    app.setOrganizationName("EMANAGER")
    log_path = _setup_logging()
    log.info("log file: %s, config: %s", log_path, CONFIG_PATH)
    T.load_fonts()
    app.setFont(T.sans(13))
    app.setWindowIcon(T.icon("logo_full", T.BRAND, 256))

    lock = QLockFile(QDir.temp().filePath("emanager_dialer.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, "EMANAGER Dialer",
                                "EMANAGER Dialer już działa. Jego ikona jest w zasobniku systemowym obok zegara.")
        return 0

    cfg = load_config()
    dialer = DialerApp(cfg)
    if needs_setup(cfg):
        dialer.open_connection_settings()
    dialer.start()
    return app.exec()
