"""'Połączenie z serwerem': server address + token, checked against the backend before saving.

Shown on first run (no config.toml yet), when the server rejects the token, and from the
settings menu, so nobody has to edit config.toml by hand.
"""
from __future__ import annotations

import json

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtWebSockets import QWebSocket
from PyQt6.QtWidgets import (QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout)

from . import theme as T
from .config import normalize_server_url

CHECK_TIMEOUT_MS = 6000


class ConnectDialog(QDialog):
    def __init__(self, url: str, token: str, agent_id: str, reason: str = "", parent=None):
        super().__init__(parent)
        self.agent_id = agent_id
        self.setWindowTitle("EMANAGER Dialer · połączenie z serwerem")
        self.setWindowIcon(T.icon("logo_full", T.BRAND, 64))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(460)
        self.setStyleSheet(
            f"QDialog{{background:{T.SURFACE};}} QLabel{{color:{T.TEXT};}}"
            f"QLineEdit{{background:{T.BG};color:{T.TEXT};border:1px solid {T.BORDER};border-radius:8px;"
            f"padding:7px 9px;selection-background-color:{T.ACCENT};}}"
            f"QPushButton{{background:{T.BUTTON_BG};color:{T.TEXT};border:1px solid {T.BORDER};"
            f"border-radius:8px;padding:7px 14px;}}"
            f"QPushButton#primary{{background:{T.TEXT_STRONG};color:{T.BG};border:0;font-weight:600;}}"
            f"QPushButton:disabled{{color:{T.FAINT};}}")

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        title = QLabel("Połączenie z serwerem podpowiedzi")
        title.setFont(T.sans(15, 700))
        root.addWidget(title)
        if reason:
            r = QLabel(reason)
            r.setWordWrap(True)
            r.setStyleSheet(f"color:{T.ERROR};")
            root.addWidget(r)

        form = QFormLayout()
        form.setSpacing(8)
        self.url_edit = QLineEdit(url)
        self.url_edit.setPlaceholderText("localhost lub 192.168.1.50")
        self.token_edit = QLineEdit(token)
        self.token_edit.setPlaceholderText("AUTH_TOKEN z pliku backend\\.env")
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Adres serwera", self.url_edit)
        form.addRow("Token", self.token_edit)
        root.addLayout(form)
        hint = QLabel("Ten sam komputer co serwer: localhost. Inny komputer: adres IP serwera w sieci.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{T.MUTED};")
        root.addWidget(hint)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.save_anyway = QPushButton("Zapisz bez sprawdzania")
        self.save_anyway.clicked.connect(self.accept)
        self.check_btn = QPushButton("Sprawdź i zapisz")
        self.check_btn.setObjectName("primary")
        self.check_btn.setDefault(True)
        self.check_btn.clicked.connect(self._check)
        buttons.addWidget(self.save_anyway)
        buttons.addWidget(self.check_btn)
        root.addLayout(buttons)

        self._ws: QWebSocket | None = None
        self._timeout = QTimer(self, singleShot=True, interval=CHECK_TIMEOUT_MS, timeout=self._on_timeout)

    # ---------------------------------------------------------------- result
    @property
    def url(self) -> str:
        return normalize_server_url(self.url_edit.text())

    @property
    def token(self) -> str:
        return self.token_edit.text().strip()

    # ---------------------------------------------------------------- check
    def _check(self) -> None:
        self._close_ws()
        self.url_edit.setText(self.url)
        self._set_status(f"Łączenie z {self.url}…", T.MUTED2)
        self.check_btn.setEnabled(False)
        ws = self._ws = QWebSocket()
        ws.connected.connect(lambda: ws.sendTextMessage(json.dumps(
            {"type": "hello", "token": self.token, "agent_id": self.agent_id,
             "sample_rate": 16000, "channels": 2, "format": "s16le"})))
        ws.textMessageReceived.connect(self._on_text)
        ws.errorOccurred.connect(lambda _e: self._fail(f"Brak połączenia: {ws.errorString()}. "
                                                       "Sprawdź, czy serwer jest uruchomiony i adres jest poprawny."))
        self._timeout.start()
        ws.open(QUrl(self.url))

    def _on_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if msg.get("type") == "ready":
            self._done()
            self._set_status("Połączono.", T.ACCENT)
            QTimer.singleShot(500, self.accept)
        elif msg.get("type") == "error" and msg.get("code") == "auth":
            self._fail("Serwer odrzucił token. Wpisz wartość AUTH_TOKEN z pliku backend\\.env.")

    def _on_timeout(self) -> None:
        self._fail("Serwer nie odpowiada. Sprawdź adres i czy serwer działa.")

    def _fail(self, text: str) -> None:
        if not self.check_btn.isEnabled():
            self._done()
            self._set_status(text, T.ERROR)

    def _done(self) -> None:
        self._timeout.stop()
        self.check_btn.setEnabled(True)
        self._close_ws()

    def _close_ws(self) -> None:
        if self._ws is not None:
            ws, self._ws = self._ws, None
            ws.errorOccurred.disconnect()
            ws.textMessageReceived.disconnect()
            ws.abort()
            ws.deleteLater()

    def _set_status(self, text: str, color: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{color};")

    def done(self, r: int) -> None:
        self._timeout.stop()
        self._close_ws()
        super().done(r)
