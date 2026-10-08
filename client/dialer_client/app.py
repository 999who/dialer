"""Wires audio capture, call detection, backend link and the overlay together."""
from __future__ import annotations

import logging
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QDir, QLockFile, QObject, QPoint, QProcess, QSettings, QStandardPaths, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QDesktopServices
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox, QSystemTrayIcon

from . import theme as T
from .config import CONFIG_PATH, Config, load_config, needs_setup, save_values
from .crm import Crm, CrmError, Supabase
from .login_dialog import LoginDialog
from .settings_dialog import SettingsDialog
from .detect import CallDetector, ZadarmaWatcher
from .local_server import LocalServer
from .model_download import DownloadProgress
from .net import BackendLink
from .overlay import CORNERS, Overlay
from .zadarma_audio import ZadarmaAudioWatcher
from . import updater

log = logging.getLogger("app")


class Bridge(QObject):
    """Thread -> Qt main thread."""
    frame = pyqtSignal(bytes, float, float)
    zadarma = pyqtSignal(object)
    zadarma_audio = pyqtSignal(object)
    update = pyqtSignal(str, object)  # (event, payload) from the updater thread
    crm = pyqtSignal(str, object)     # (event, payload) from CRM worker threads


class DialerApp(QObject):
    def __init__(self, cfg: Config, local: LocalServer, audio_factory=None):
        super().__init__()
        self.cfg = cfg
        self.local = local
        self.qs = QSettings("EMANAGER", "Dialer")
        corner = self.qs.value("corner", cfg.corner)
        self.overlay = Overlay(corner, cfg.hint_seconds, cfg.summary_seconds, cfg.opacity, cfg.show_in_taskbar)
        self.link = BackendLink(local.url, local.token, cfg.agent_id, cfg.offline_buffer_s)
        self.detector = CallDetector(end_silence_s=cfg.call_end_silence_s, no_line_s=cfg.no_line_warning_s)
        self.bridge = Bridge()
        self.preroll: deque[bytes] = deque(maxlen=30)  # 3 s, so the first words aren't lost
        self.transcript: list[tuple[float, str, str]] = []
        self.zadarma_ok: bool | None = None
        self.zadarma_audio = None  # latest ZadarmaAudio snapshot
        self.manual_call = False
        self._manual_zadarma_idle = False

        self.bridge.frame.connect(self._on_frame)
        self.bridge.zadarma.connect(self._on_zadarma)
        self.bridge.zadarma_audio.connect(self._on_zadarma_audio)
        self.bridge.update.connect(self._on_update_event)
        self.bridge.crm.connect(self._on_crm_event)
        self.crm: Crm | None = None        # set once the operator is signed in
        self.operator = ""                 # their name, for the menu and the log
        self.crm_remember = True           # keep the CRM session across restarts ("Zapamiętaj mnie")
        self.card = None                   # ClientCard of the current call
        self._call_seq = 0                 # a lookup for an earlier call must not land on this one
        self._release = None
        self._updating = False
        self.link.message.connect(self._on_message)
        self.link.online_changed.connect(self._on_online)
        self.link.retry_in.connect(self._on_retry)
        ov = self.overlay
        ov.hint_copied.connect(lambda hid: self.link.send({"type": "feedback", "hint_id": hid, "copied": True}))
        ov.hint_feedback.connect(lambda hid, u: self.link.send({"type": "feedback", "hint_id": hid, "useful": u}))
        ov.pause_toggled.connect(self.link.set_paused)
        ov.stop_requested.connect(self.end_call_by_hand)
        ov.error_action.connect(self._on_error_action)
        ov.settings_requested.connect(self._settings_menu)
        ov.save_crm.connect(self._save_crm)
        ov.open_transcript.connect(self._open_transcript)

        self.clock = QTimer(self, interval=1000, timeout=self._on_clock)
        self.clock.start()

        self.audio = (audio_factory or self._make_audio)()
        self.watcher = ZadarmaWatcher(self.bridge.zadarma.emit)
        self.zwatch = ZadarmaAudioWatcher(self.bridge.zadarma_audio.emit)
        if cfg.call_detect == "zadarma":
            self.audio.line_gate = self.zwatch.gate_open
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
        # connect as soon as the built-in backend has loaded its models
        self.overlay.show_error("loading")
        self._download = None
        if self.cfg.local_stt == "parakeet" and not self.cfg.parakeet_model_path:
            try:
                self._download = DownloadProgress()
            except Exception as e:  # progress is only a nicety
                log.info("model download progress unavailable: %s", e)
        self._local_timer = QTimer(self, interval=500, timeout=self._check_local)
        self._local_timer.start()
        if updater.can_self_update():
            QTimer.singleShot(15000, lambda: self.check_updates(manual=False))
            self._update_timer = QTimer(self, interval=6 * 3600 * 1000, timeout=lambda: self.check_updates(False))
            self._update_timer.start()
        QTimer.singleShot(0, self._crm_startup)
        self.watcher.start()
        if self.cfg.call_detect == "zadarma":
            self.zwatch.start()
        try:
            self.audio.start()
            self.overlay.clear_error("no_audio")
        except Exception as e:
            log.exception("audio start failed")
            self.overlay.show_error("no_audio", str(e)[:40])

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(T.app_icon(), self)
        self.tray.setToolTip("EMANAGER Dialer")
        self.tray.activated.connect(lambda *_: (self.overlay.showNormal(), self.overlay.reposition()))
        menu = QMenu()
        menu.addAction("Pokaż", lambda: (self.overlay.showNormal(), self.overlay.reposition()))
        menu.addAction("Ustawienia (Gemini, baza wiedzy)…", lambda: self.open_connection_settings())
        menu.addAction("Konto CRM…", self._account_action)
        menu.addAction("Sprawdź aktualizacje", lambda: self.check_updates(manual=True))
        menu.addAction("Zamknij", QApplication.quit)
        self.tray.setContextMenu(menu)
        self.tray.show()

    # ---------------------------------------------------------------- audio / call
    def _on_frame(self, frame: bytes, mic: float, line: float) -> None:
        self.overlay.set_levels(mic, line)
        if self.manual_call and self.cfg.call_detect == "zadarma" and self.zadarma_audio is not None:
            if not self.zadarma_audio.mic_active:
                self._manual_zadarma_idle = True
            elif self._manual_zadarma_idle:
                # a real Zadarma call began during a call started by hand: that one ends here,
                # and the Zadarma call starts fresh and will also end on its own
                log.info("Zadarma call during a manual call: switching to it")
                self.detector.force(False)
                self._call_ended()
        z = self.zadarma_audio if (self.cfg.call_detect == "zadarma" and not self.manual_call) else None
        ev = self.detector.update(mic, line, zadarma=z)
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
        log.info("call started, operator: %s", self.operator or "(not signed in)")
        self.transcript.clear()
        self.overlay.clear_screen()  # nothing from the previous call stays on screen
        self.overlay.set_paused(False)  # pause is for one call only
        self.link.call_start("")
        self._call_seq += 1
        self.card = None
        self._lookup_number(self._call_seq)
        for f in self.preroll:
            self.link.send_audio(f)
        self.preroll.clear()
        self.overlay.set_call(True, 0)

    def _call_ended(self) -> None:
        log.info("call ended")
        self.manual_call = False
        self.link.call_end()
        self.overlay.set_paused(False)
        self.overlay.set_call(False)
        self.overlay.clear_transcript()  # the summary card keeps it ("Otwórz transkrypcję")
        self.overlay.clear_error("no_line")

    def end_call_by_hand(self) -> None:
        """Stop button / menu: ends the call now and clears the screen."""
        if self.detector.in_call:
            self.detector.force(False)
            self._call_ended()
        else:
            self.overlay.clear_screen()

    def _on_clock(self) -> None:
        if self.detector.in_call:
            self.overlay.set_call(True, time.monotonic() - self.detector.started_at)

    def _on_zadarma_audio(self, st) -> None:
        prev = self.zadarma_audio
        self.zadarma_audio = st
        if prev is None or (prev.available, prev.mic_active, prev.out_active, prev.out_found) != (
                st.available, st.mic_active, st.out_active, st.out_found):
            log.info("zadarma audio: available=%s mic=%s (%s) out=%s (%s) metered=%s", st.available,
                     st.mic_active, st.mic_device or "-", st.out_active, st.out_device or "-", st.out_found)
        self._follow_zadarma_devices(st)

    def _auto_device(self, key: str) -> bool:
        """True unless the operator picked this device by hand (menu or config.toml)."""
        return not (self.qs.value(key, "") or getattr(self.cfg, key))

    def _follow_zadarma_devices(self, st) -> None:
        """Capture exactly where Zadarma plays and records the call.

        Windows often has the headset as the "communications" device and the speakers as the
        default output; Zadarma uses the former, so the default loopback would hear nothing.
        """
        if not st.mic_active or not hasattr(self.audio, "restart"):
            return  # decide during a call only, when Zadarma's streams point at the real devices
        change = {}
        cur_line = getattr(getattr(self.audio, "line_device", None), "name", "") or ""
        cur_mic = getattr(getattr(self.audio, "mic_device", None), "name", "") or ""
        if st.out_device and self._auto_device("line_device") and st.out_device.lower() not in cur_line.lower():
            change["line_name"] = st.out_device
        if st.mic_device and self._auto_device("mic_device") and st.mic_device.lower() not in cur_mic.lower():
            change["mic_name"] = st.mic_device
        if not change or change == getattr(self, "_last_follow", None):
            return
        self._last_follow = change  # don't retry the same switch every 100 ms if the device can't open
        log.info("following Zadarma's devices: %s", change)
        try:
            self.audio.restart(**change)
            self.overlay.clear_error("no_audio")
        except Exception as e:
            log.exception("switching to Zadarma's device failed")
            self.overlay.show_error("no_audio", str(e)[:40])

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
            for kind in ("server", "loading", "local_failed"):
                self.overlay.clear_error(kind)

    def _check_local(self) -> None:
        if self.local.state == "loading":
            st = self._download.status() if self._download else None
            if st:
                self.overlay.set_error_progress("loading", st[0], st[1])
            else:
                self.overlay.set_error_progress("loading", None)
            return
        self._local_timer.stop()
        if self.local.state == "ready":
            self.link.connect_now()
        else:
            self.overlay.clear_error("loading")
            self.overlay.show_error("local_failed", self.local.error[:40])

    def _on_retry(self, seconds: int) -> None:
        if self.local.state == "error":
            self.overlay.clear_error("loading")
            self.overlay.show_error("local_failed", self.local.error[:40])
        elif self.local.state == "loading":
            self.overlay.show_error("loading")
        else:  # ready but the connection dropped: reconnects on its own
            self.overlay.show_error("server", f"ponowna próba za {seconds} s")

    def open_connection_settings(self, reason: str = "") -> bool:
        before = (self.cfg.gemini_api_key, self.cfg.gemini_model, self.cfg.database_url)
        if not ask_settings(self.cfg, reason, on_check_updates=lambda: self.check_updates(manual=True)):
            return False
        if (self.cfg.gemini_api_key, self.cfg.gemini_model, self.cfg.database_url) != before:
            relaunch()  # the built-in backend reads these at startup
        return True

    def _on_message(self, m: dict) -> None:
        t = m.get("type")
        if t == "ready":
            if m.get("llm_error"):
                self.overlay.show_error("no_llm", str(m["llm_error"])[:40])
            else:
                self.overlay.clear_error("no_llm")
            if m.get("rag_error"):
                self.overlay.show_error("no_rag", str(m["rag_error"])[:40])
            else:
                self.overlay.clear_error("no_rag")
        elif t == "transcript":
            self.transcript.append((m.get("t", 0), m["speaker"], m["text"]))
            if self.detector.in_call:  # last words of an ended call go to its summary only
                self.overlay.add_transcript(m.get("t", 0), m["speaker"], m["text"])
        elif t == "hint":
            self.overlay.show_hint(m)
        elif t == "latency":
            self.overlay.set_latency(int(m["ms"]))
        elif t == "caller":
            self._lookup_name(str(m.get("text", "")), self._call_seq)
        elif t == "call_summary":
            m["transcript"] = list(self.transcript)
            self.overlay.show_summary(m)
        elif t == "error":
            log.error("backend: %s", m)

    # ---------------------------------------------------------------- actions
    def _on_error_action(self, kind: str) -> None:
        if kind == "server":
            self.link.connect_now()
        elif kind in ("no_llm", "no_rag"):
            self.open_connection_settings()
        elif kind == "update":
            self.install_update()
        elif kind in ("loading", "local_failed"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(_log_dir() / "dialer.log")))
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
            auto = QAction("Automatycznie (to samo co Zadarma)", sub, checkable=True)
            auto.setChecked(self._auto_device(key))
            auto.triggered.connect(lambda _=False, k=key: self._set_auto_device(k))
            group.addAction(auto)
            sub.addAction(auto)
            sub.addSeparator()
            for d in devs:
                a = QAction(d.name.replace(" [Loopback]", ""), sub, checkable=True)
                a.setChecked(bool(cur and cur.index == d.index) and not self._auto_device(key))
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
            menu.addAction("Ustawienia (Gemini, baza wiedzy)…", lambda: self.open_connection_settings())
            menu.addAction(f"Konto CRM: {self.operator} (wyloguj)" if self.crm else "Zaloguj do CRM…",
                           self._account_action)
            menu.addAction(f"Sprawdź aktualizacje (wersja {updater.version_label()})",
                           lambda: self.check_updates(manual=True))
            menu.addAction("Pokaż dziennik (log)", lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(_log_dir() / "dialer.log"))))
            menu.addSeparator()
            if self.detector.in_call:
                menu.addAction("Zakończ rozmowę", self.end_call_by_hand)
            else:
                menu.addAction("Rozpocznij rozmowę ręcznie", self._manual_start)
                menu.addAction("Wyczyść ekran", self.overlay.clear_screen)
            menu.addSeparator()
            menu.addAction("Zamknij EMANAGER Dialer", QApplication.quit)
        menu.exec(pos)

    def _manual_start(self) -> None:
        self.manual_call = True
        self._manual_zadarma_idle = False  # switch to Zadarma only on a call that starts after this
        self.detector.force(True)
        self._call_started()

    # ---------------------------------------------------------------- CRM: operator and client card
    def _crm_worker(self, name: str, fn) -> None:
        """Runs fn() in a thread; its (event, payload) result comes back on the Qt thread."""
        def work():
            try:
                self.bridge.crm.emit(*fn())
            except Exception as e:
                log.warning("crm %s failed: %s", name, e)
                self.bridge.crm.emit("failed", (name, e))

        threading.Thread(target=work, name=f"crm-{name}", daemon=True).start()

    def _crm_startup(self) -> None:
        token = self.qs.value("crm/refresh_token", "")
        if not (self.cfg.crm_url and self.cfg.crm_key and token):
            QTimer.singleShot(300, self.crm_login)
            return
        sb = Supabase(self.cfg.crm_url, self.cfg.crm_key)

        def resume():
            sb.refresh(token)
            return "signed_in", sb

        self._crm_worker("resume", resume)

    def crm_login(self, reason: str = "") -> None:
        dlg = LoginDialog(self.cfg, self.qs.value("crm/email", ""), reason)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.sb is None:
            return
        conn = dlg.connection()
        if conn != {k: getattr(self.cfg, k) for k in conn}:
            for k, v in conn.items():
                setattr(self.cfg, k, v)
            try:
                save_values(conn)
            except OSError as e:
                log.error("cannot save %s: %s", CONFIG_PATH, e)
        self.crm_remember = dlg.remember.isChecked()
        if not self.crm_remember:
            self.qs.remove("crm/refresh_token")
        self._on_crm_event("signed_in", dlg.sb)

    def _account_action(self) -> None:
        if not self.crm:
            self.crm_login()
            return
        sb = self.crm.sb
        threading.Thread(target=sb.sign_out, daemon=True).start()
        self.qs.remove("crm/refresh_token")
        log.info("operator %s signed out", self.operator)
        self.crm, self.operator = None, ""
        self.link.agent_id = self.cfg.agent_id
        self.crm_login()

    def _lookup_number(self, seq: int) -> None:
        crm = self.crm
        if not crm:
            return

        def work():
            # Zadarma's webhook reaches the CRM about a second after the call starts
            call = None
            for _ in range(8):
                try:
                    call = crm.current_call(self.cfg.crm_sip)
                except CrmError as e:  # a network hiccup: try again within the same window
                    log.info("current call lookup: %s", e)
                if call or not crm.has_current_call_fn or seq != self._call_seq:
                    break
                time.sleep(1.0)
            if not call:
                return "no_number", seq
            log.info("caller: %s call on extension %s", call.get("direction"), call.get("internal"))
            return "card", (seq, crm.by_phone(call.get("phone", "")))

        self._crm_worker("number", work)

    def _lookup_name(self, heard: str, seq: int) -> None:
        if not self.crm or not heard or (self.card is not None and self.card.kind != "unknown"):
            return  # the number already found them; a name only fills the gap
        crm = self.crm
        log.info("caller introduced themselves: %r", heard)

        def work():
            card = crm.by_name(heard)
            return ("card", (seq, card)) if card.found else ("no_name", seq)

        self._crm_worker("name", work)

    def _on_crm_event(self, event: str, payload) -> None:
        if event == "signed_in":
            sb: Supabase = payload
            sb.on_session = lambda s: self.bridge.crm.emit("token", s.refresh_token)
            if self.crm_remember:  # otherwise the session lives only until the app closes
                self.qs.setValue("crm/refresh_token", sb.session.refresh_token)
            self.qs.setValue("crm/email", sb.session.email)
            self.crm = Crm(sb, [n for n in self.cfg.own_numbers.split(",") if n.strip()])
            self.operator = sb.session.email
            self.link.agent_id = sb.session.email  # the backend logs calls under this operator

            def warm():
                profile = self.crm.load_profile()
                self.crm.refresh_cache(force=True)
                return "profile", profile

            self._crm_worker("profile", warm)
        elif event == "token":
            if self.crm and self.crm_remember:
                self.qs.setValue("crm/refresh_token", payload)
        elif event == "profile":
            self.operator = payload.get("full_name") or self.operator
            log.info("signed in to the CRM as %s", self.operator)
            self._notify(f"Zalogowano do CRM: {self.operator}")
        elif event == "card":
            seq, card = payload
            if seq != self._call_seq or not self.detector.in_call:
                return
            self.card = card
            self.overlay.show_client(card.to_dict())
            self.link.send({"type": "client_context", "text": card.to_prompt()})
            log.info("client card: %s (%s, by %s)", card.title or card.person or "-", card.kind, card.via)
        elif event == "no_number":
            if payload == self._call_seq and self.detector.in_call and self.crm and self.crm.has_current_call_fn:
                log.info("no Zadarma event for this call in the CRM")
        elif event == "failed":
            name, e = payload
            if name == "resume":
                if isinstance(e, CrmError) and e.status in (400, 401, 403):  # the saved session is gone
                    self.qs.remove("crm/refresh_token")
                    QTimer.singleShot(0, lambda: self.crm_login("Sesja CRM wygasła. Zaloguj się ponownie."))
                else:  # no network yet: keep the session and retry in a minute
                    QTimer.singleShot(60000, self._crm_startup)
            elif isinstance(e, CrmError) and e.status == 401 and self.crm:
                self.crm = None
                self._notify("Sesja CRM wygasła. Zaloguj się ponownie (logo → Konto CRM).")

    # ---------------------------------------------------------------- updates
    def check_updates(self, manual: bool = True) -> None:
        if not updater.can_self_update():
            if manual:
                self._notify("Aktualizacje działają w wersji .exe pobranej z GitHub.")
            return

        def work():
            try:
                self.bridge.update.emit("checked", (updater.fetch_release(), manual))
            except Exception as e:
                log.warning("update check failed: %s", e)
                self.bridge.update.emit("check_failed", (str(e), manual))

        threading.Thread(target=work, name="update-check", daemon=True).start()

    def install_update(self) -> None:
        if self._updating or self._release is None:
            return
        if self.detector.in_call:
            self.overlay.show_error("update", "po zakończeniu rozmowy")
            return
        self._updating = True
        rel = self._release
        self.overlay.show_error("update", "pobieranie 0%")

        def work():
            try:
                new = updater.download(rel, lambda f: self.bridge.update.emit("progress", f))
                self.bridge.update.emit("downloaded", new)
            except Exception as e:
                log.exception("update download failed")
                self.bridge.update.emit("failed", str(e))

        threading.Thread(target=work, name="update-download", daemon=True).start()

    def _on_update_event(self, event: str, payload) -> None:
        if event == "checked":
            rel, manual = payload
            log.info("latest release %s, this build %s", rel.commit[:7], updater.BUILD[:7])
            if rel.newer:
                self._release = rel
                self.overlay.show_error("update", rel.published[:10])
            elif manual:
                self._notify(f"Masz najnowszą wersję ({updater.version_label()}).")
        elif event == "check_failed":
            if payload[1]:
                self._notify(f"Nie udało się sprawdzić aktualizacji: {payload[0][:80]}")
        elif event == "progress":
            self.overlay.show_error("update", f"pobieranie {int(payload * 100)}%")
        elif event == "downloaded":
            try:
                updater.install(payload)
            except Exception as e:
                log.exception("update install failed")
                self._updating = False
                self.overlay.show_error("update", "błąd instalacji")
                self._notify(f"Nie udało się zainstalować aktualizacji: {e}")
                return
            log.info("update installed, restarting")
            relaunch()
        elif event == "failed":
            self._updating = False
            self.overlay.show_error("update", "błąd pobierania")
            self._notify(f"Nie udało się pobrać aktualizacji: {str(payload)[:80]}")

    def _notify(self, text: str) -> None:
        self.tray.showMessage("EMANAGER Dialer", text, QSystemTrayIcon.MessageIcon.Information, 5000)

    def _set_auto_device(self, key: str) -> None:
        self.qs.setValue(key, "")
        self._last_follow = None
        if self.zadarma_audio is not None:
            self._follow_zadarma_devices(self.zadarma_audio)

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


_lock: QLockFile | None = None


def ask_settings(cfg: Config, reason: str = "", on_check_updates=None) -> bool:
    """Settings window; on OK updates cfg and config.toml."""
    dlg = SettingsDialog(cfg, reason, on_check_updates=on_check_updates)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return False
    vals = dlg.values()
    for k, v in vals.items():
        setattr(cfg, k, v)
    try:
        save_values(vals)
    except OSError as e:
        log.error("cannot save %s: %s", CONFIG_PATH, e)
        QMessageBox.warning(None, "EMANAGER Dialer", f"Nie udało się zapisać {CONFIG_PATH}:\n{e}")
    return True


def relaunch() -> None:
    args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
    if _lock is not None:
        _lock.unlock()
    QProcess.startDetached(sys.executable, args)
    QApplication.quit()


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
    app.setWindowIcon(T.app_icon())

    global _lock
    lock = _lock = QLockFile(QDir.temp().filePath("emanager_dialer.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(3000):  # 3 s: a relaunch may still be shutting down the old instance
        QMessageBox.information(None, "EMANAGER Dialer",
                                "EMANAGER Dialer już działa. Jego ikona jest w zasobniku systemowym obok zegara.")
        return 0

    updater.cleanup()
    log.info("version %s", updater.version_label())
    cfg = load_config()
    if needs_setup(cfg):
        ask_settings(cfg)
    local = LocalServer(cfg)
    local.start()
    app.aboutToQuit.connect(local.stop)
    dialer = DialerApp(cfg, local)
    dialer.start()
    return app.exec()
