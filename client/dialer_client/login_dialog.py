"""Sign-in to the EMANAGER CRM with the operator's own account (email + password).

Calls and clients brought in are attributed to whoever is signed in, and the client card
reads the CRM with that operator's permissions. The CRM address and the publishable key are
asked for here too while config.toml doesn't have them yet.
"""
from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from . import theme as T
from .crm import CrmError, Supabase


class LoginDialog(QDialog):
    def __init__(self, cfg, email: str = "", reason: str = "", parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.sb: Supabase | None = None
        self.setWindowTitle("EMANAGER Dialer · konto CRM")
        self.setWindowIcon(T.icon("logo_full", T.BRAND, 64))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(440)
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
        title = QLabel("Zaloguj się do CRM")
        title.setFont(T.sans(15, 700))
        root.addWidget(title)
        info = QLabel(reason or "Tym samym kontem co w CRM. Rozmowy i pozyskani klienci będą przypisani do "
                      "Ciebie, a podczas rozmowy zobaczysz kartę klienta.")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{T.ERROR if reason else T.MUTED};")
        root.addWidget(info)

        form = QFormLayout()
        form.setSpacing(8)
        self.email = QLineEdit(email)
        self.email.setPlaceholderText("imie@emanager.pro")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("E-mail", self.email)
        form.addRow("Hasło", self.password)
        self.url = QLineEdit(cfg.crm_url)
        self.url.setPlaceholderText("https://….supabase.co")
        self.key = QLineEdit(cfg.crm_key)
        self.key.setPlaceholderText("sb_publishable_… lub klucz anon")
        self.sip = QLineEdit(cfg.crm_sip)
        self.sip.setPlaceholderText("np. 100; puste = dowolny")
        # the connection rows are needed once per PC; afterwards they stay out of the way
        self._conn_rows = [("Adres CRM", self.url), ("Klucz publiczny", self.key), ("Numer wewn. Zadarma", self.sip)]
        for lbl, w in self._conn_rows:
            form.addRow(lbl, w)
        root.addLayout(form)
        self.more = QPushButton("Połączenie z CRM…")
        self.more.clicked.connect(lambda: self._show_conn(True))
        root.addWidget(self.more, alignment=Qt.AlignmentFlag.AlignLeft)
        self._form = form
        self._show_conn(not (cfg.crm_url and cfg.crm_key))

        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        skip = QPushButton("Pomiń")
        skip.setToolTip("Dialer działa bez karty klienta, a rozmowy nie są przypisane do nikogo.")
        skip.clicked.connect(self.reject)
        self.ok = QPushButton("Zaloguj")
        self.ok.setObjectName("primary")
        self.ok.setDefault(True)
        self.ok.clicked.connect(self._login)
        buttons.addWidget(skip)
        buttons.addWidget(self.ok)
        root.addLayout(buttons)

        self._result: object = None
        self._poll = QTimer(self, interval=150, timeout=self._poll_result)
        (self.password if email else self.email).setFocus()

    def _show_conn(self, on: bool) -> None:
        for _, w in self._conn_rows:
            self._form.setRowVisible(w, on)
        self.more.setVisible(not on)
        self.adjustSize()

    def connection(self) -> dict:
        return {"crm_url": self.url.text().strip().rstrip("/"), "crm_key": self.key.text().strip(),
                "crm_sip": self.sip.text().strip()}

    def _login(self) -> None:
        conn = self.connection()
        email, password = self.email.text().strip(), self.password.text()
        if not (conn["crm_url"] and conn["crm_key"]):
            self._show_conn(True)
            return self._set_status("Wpisz adres CRM i klucz publiczny (Supabase → Project Settings → API).", T.ERROR)
        if not (email and password):
            return self._set_status("Wpisz e-mail i hasło.", T.ERROR)
        self.ok.setEnabled(False)
        self._set_status("Logowanie…", T.MUTED2)
        sb = Supabase(conn["crm_url"], conn["crm_key"])
        self._result = None

        def work():
            try:
                sb.sign_in(email, password)
                self._result = sb
            except CrmError as e:
                self._result = e
            except Exception as e:  # malformed URL and the like
                self._result = CrmError(str(e))

        threading.Thread(target=work, daemon=True).start()
        self._poll.start()

    def _poll_result(self) -> None:
        if self._result is None:
            return
        self._poll.stop()
        self.ok.setEnabled(True)
        if isinstance(self._result, Supabase):
            self.sb = self._result
            self.accept()
            return
        e = self._result
        msg = "Nieprawidłowy e-mail lub hasło." if getattr(e, "status", 0) in (400, 401) else str(e)
        self._set_status(msg, T.ERROR)

    def _set_status(self, text: str, color: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{color};")

    def done(self, r: int) -> None:
        self._poll.stop()
        super().done(r)
