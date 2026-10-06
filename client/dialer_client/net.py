"""WebSocket link to the backend with auto-reconnect and catch-up buffer.

While the server is unreachable, audio of the current call is buffered (up to
`buffer_s` seconds) and flushed after reconnect, so transcripts and hints
"catch up" with the conversation, as the error widget promises.
"""
from __future__ import annotations

import json
import logging
from collections import deque

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtWebSockets import QWebSocket

log = logging.getLogger("net")

BACKOFF = (1, 2, 4, 8, 8, 15)


class BackendLink(QObject):
    message = pyqtSignal(dict)
    online_changed = pyqtSignal(bool)
    retry_in = pyqtSignal(int)  # seconds until next reconnect attempt
    auth_failed = pyqtSignal()  # wrong token: retrying won't help until the settings change

    def __init__(self, url: str, token: str, agent_id: str, buffer_s: int = 60):
        super().__init__()
        self.url, self.token, self.agent_id = url, token, agent_id
        self.ws = QWebSocket()
        self.ws.connected.connect(self._on_connected)
        self.ws.disconnected.connect(self._on_disconnected)
        self.ws.textMessageReceived.connect(self._on_text)
        self.online = False
        self.ready = False
        self._attempt = 0
        self._auth_failed = False
        self._aborting = False
        self._countdown = 0
        self._timer = QTimer(self, interval=1000, timeout=self._tick)
        self._pending_text: list[str] = []
        self._audio: deque[bytes] = deque(maxlen=int(buffer_s * 10))  # 100 ms frames
        self.in_call = False
        self.paused = False
        self.phone = ""

    # ---------------------------------------------------------------- connection
    def set_target(self, url: str, token: str) -> None:
        self.url, self.token = url, token
        self._attempt = 0
        self.connect_now()

    def connect_now(self) -> None:
        self._auth_failed = False
        self._timer.stop()
        self._aborting = True  # abort() emits disconnected synchronously; don't schedule a retry for it
        try:
            self.ws.abort()
        finally:
            self._aborting = False
        log.info("connecting to %s", self.url)
        self.ws.open(QUrl(self.url))

    def _on_connected(self) -> None:
        self._timer.stop()
        self._attempt = 0
        self.ws.sendTextMessage(json.dumps({"type": "hello", "token": self.token, "agent_id": self.agent_id,
                                            "sample_rate": 16000, "channels": 2, "format": "s16le"}))

    def _on_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if msg.get("type") == "ready":
            self.ready = True
            self.online = True
            self.online_changed.emit(True)
            if self.paused:
                self.ws.sendTextMessage(json.dumps({"type": "pause", "paused": True}))
            if self.in_call:  # resume the call on a fresh server session, then catch up
                self.ws.sendTextMessage(json.dumps({"type": "call_start", "phone": self.phone}))
            while self._audio:
                self.ws.sendBinaryMessage(self._audio.popleft())
            for t in self._pending_text:
                self.ws.sendTextMessage(t)
            self._pending_text.clear()
        elif msg.get("type") == "error" and msg.get("code") == "auth":
            log.error("backend rejected token")
            self._auth_failed = True
        self.message.emit(msg)

    def _on_disconnected(self) -> None:
        was = self.online
        self.online = self.ready = False
        if was:
            self.online_changed.emit(False)
        if self._aborting:
            return
        if self._auth_failed:
            self.auth_failed.emit()
            return
        delay = BACKOFF[min(self._attempt, len(BACKOFF) - 1)]
        self._attempt += 1
        self._countdown = delay
        self.retry_in.emit(delay)
        self._timer.start()

    def _tick(self) -> None:
        self._countdown -= 1
        if self._countdown <= 0:
            self.connect_now()
        else:
            self.retry_in.emit(self._countdown)

    # ---------------------------------------------------------------- outgoing
    def send_audio(self, frame: bytes) -> None:
        if not self.in_call or self.paused:
            return  # nothing leaves the PC outside a call
        if self.ready:
            self.ws.sendBinaryMessage(frame)
        else:
            self._audio.append(frame)

    def send(self, msg: dict) -> None:
        raw = json.dumps(msg, ensure_ascii=False)
        if self.ready:
            self.ws.sendTextMessage(raw)
        elif msg.get("type") in ("feedback", "call_end"):
            self._pending_text.append(raw)

    def call_start(self, phone: str = "") -> None:
        self.in_call, self.phone = True, phone
        self._audio.clear()
        self.send({"type": "call_start", "phone": phone})

    def call_end(self) -> None:
        self.in_call = False
        self.send({"type": "call_end"})

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.send({"type": "pause", "paused": paused})
