"""Sign-in to the EMANAGER CRM with the operator's own account (email + password).

Calls and clients brought in are attributed to whoever is signed in, and the client card
reads the CRM with that operator's permissions. The CRM address and the publishable key are
asked for here too while config.toml doesn't have them yet.

Looks like the mockup's 'Okno logowania': 400 wide, no system frame (the strip at the top drags
the window), logo tile, Login / Hasło, 'Zapamiętaj mnie', one accent button. Enter signs in,
closing the window skips the sign-in.
"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from PyQt6.QtCore import QPoint, QSize, Qt, QTimer
from PyQt6.QtGui import QAction, QIcon, QPixmap, QTransform
from PyQt6.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QVBoxLayout, QWidget)

from . import theme as T
from .config import CRM_KEY, CRM_URL
from .crm import CrmError, Supabase

W = 400


def _check_png() -> str:
    """Style sheets take images only from files: the tick of the checkbox, drawn once."""
    path = Path(tempfile.gettempdir()) / "emanager-dialer-check.png"
    if not path.exists():
        T.pixmap("check", T.ACCENT_INK, 12, dpr=1.0).save(str(path), "PNG")
    return path.as_posix()


class LoginDialog(QDialog):
    def __init__(self, cfg, email: str = "", reason: str = "", parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.sb: Supabase | None = None
        self.setWindowTitle("EMANAGER Dialer · logowanie")
        self.setWindowIcon(T.app_icon())
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._drag: QPoint | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)  # height follows the content
        frame = QFrame()
        frame.setObjectName("login")
        frame.setFixedWidth(W)
        outer.addWidget(frame)
        self.setStyleSheet(
            f"QFrame#login{{background:{T.BG};border:1px solid {T.BORDER};border-radius:16px;}}"
            f"QLabel{{background:transparent;color:{T.TEXT};}}"
            f"QLineEdit{{background:{T.SURFACE};color:{T.TEXT_STRONG};border:1px solid #2A2E35;border-radius:10px;"
            f"padding:0 12px;min-height:42px;max-height:42px;font-size:14px;"
            f"selection-background-color:{T.ACCENT};selection-color:{T.ACCENT_INK};}}"
            f"QLineEdit:focus{{border-color:{T.ACCENT};}}"
            f"QLineEdit[error=\"true\"]{{border-color:{T.ERROR};}}"
            f"QLineEdit:disabled{{color:{T.MUTED};}}"
            f"QCheckBox{{color:{T.TEXT_SOFT};font-size:13px;spacing:9px;background:transparent;}}"
            f"QCheckBox::indicator{{width:16px;height:16px;border-radius:4px;border:1.5px solid #3A3F47;"
            f"background:{T.SURFACE};}}"
            f"QCheckBox::indicator:checked{{background:{T.ACCENT};border-color:{T.ACCENT};"
            f"image:url({_check_png()});}}"
            f"QPushButton#submit{{background:{T.ACCENT};color:{T.ACCENT_INK};border:0;border-radius:10px;"
            f"min-height:44px;font-size:14px;font-weight:700;}}"
            f"QPushButton#submit:hover{{background:#45E0C1;}}"
            f"QPushButton#submit:disabled{{background:#1F2A28;color:{T.ACCENT};}}"
            f"QPushButton#bar{{background:transparent;border:0;border-radius:7px;}}"
            f"QPushButton#bar:hover{{background:rgba(255,255,255,0.06);}}"
            f"QPushButton#link{{background:transparent;border:0;color:{T.MUTED};font-size:12px;"
            f"text-align:left;padding:0;}}"
            f"QPushButton#link:hover{{color:{T.TEXT_SOFT};}}")

        root = QVBoxLayout(frame)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # title strip: drags the window; minimise and close (= skip the sign-in)
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 6, 6, 0)
        bar.setSpacing(2)
        bar.addStretch(1)
        for name, tip, slot in (("minus", "Zminimalizuj", self.showMinimized),
                                ("close", "Zamknij (bez logowania)", self.reject)):
            b = QPushButton()
            b.setObjectName("bar")
            b.setFixedSize(32, 28)
            b.setIcon(T.icon(name, T.FAINT, 13))
            b.setIconSize(QSize(13, 13))
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(slot)
            bar.addWidget(b)
        root.addLayout(bar)

        body = QVBoxLayout()
        body.setContentsMargins(32, 4, 32, 32)
        body.setSpacing(24)
        root.addLayout(body)

        brand = QVBoxLayout()
        brand.setSpacing(14)
        tile = QLabel()
        tile.setFixedSize(64, 64)
        tile.setPixmap(T.pixmap("logo_full", T.BRAND, 38))
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tile.setStyleSheet(f"background:{T.SURFACE};border:1px solid #23262B;border-radius:18px;")
        brand.addWidget(tile, 0, Qt.AlignmentFlag.AlignHCenter)
        names = QVBoxLayout()
        names.setSpacing(4)
        title = QLabel("EMANAGER Dialer")
        title.setFont(T.sans(22, 800, -2))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        names.addWidget(title)
        sub = QLabel("Zaloguj się, aby otrzymywać podpowiedzi w trakcie rozmów")
        sub.setFont(T.sans(13))
        sub.setStyleSheet(f"color:{T.MUTED};")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        names.addWidget(sub)
        brand.addLayout(names)
        body.addLayout(brand)

        form = QVBoxLayout()
        form.setSpacing(14)
        self.alert = QFrame()
        self.alert.setObjectName("alert")
        self.alert.setStyleSheet(f"QFrame#alert{{background:rgba(255,107,107,0.10);border:1px solid {T.ERROR_BORDER};"
                                 f"border-radius:10px;}}")
        al = QHBoxLayout(self.alert)
        al.setContentsMargins(12, 10, 12, 10)
        al.setSpacing(9)
        warn = QLabel()
        warn.setPixmap(T.pixmap("alert", T.ERROR, 15))
        warn.setFixedSize(15, 15)
        warn.setStyleSheet("border:0;background:transparent;")
        al.addWidget(warn, 0, Qt.AlignmentFlag.AlignTop)
        self.alert_text = QLabel()
        self.alert_text.setWordWrap(True)
        self.alert_text.setFont(T.sans(13))
        self.alert_text.setStyleSheet("color:#FFB3B3;border:0;background:transparent;")
        self.alert_text.setAccessibleName("alert")
        al.addWidget(self.alert_text, 1)
        form.addWidget(self.alert)

        self.email = QLineEdit(email)
        self.email.setPlaceholderText("imie@emanager.pro")
        form.addLayout(self._field("Login", self.email))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.eye = QAction(T.icon("eye", T.MUTED, 16), "Pokaż hasło", self.password)
        self.eye.triggered.connect(self._toggle_password)
        self.password.addAction(self.eye, QLineEdit.ActionPosition.TrailingPosition)
        self.password.textEdited.connect(lambda _: self._mark_error(False))
        form.addLayout(self._field("Hasło", self.password))

        # connection to the CRM: needed once per PC, afterwards out of the way
        self.url = QLineEdit(cfg.crm_url)
        self.url.setPlaceholderText("https://….supabase.co")
        self.key = QLineEdit(cfg.crm_key)
        self.key.setPlaceholderText("sb_publishable_… lub klucz anon")
        self.sip = QLineEdit(cfg.crm_sip)
        self.sip.setPlaceholderText("np. 100; puste = dowolny")
        self.conn = QWidget()
        cl = QVBoxLayout(self.conn)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(14)
        for lbl, w in (("Adres CRM", self.url), ("Klucz publiczny", self.key), ("Numer wewn. Zadarma", self.sip)):
            cl.addLayout(self._field(lbl, w))
        form.addWidget(self.conn)

        self.remember = QCheckBox("Zapamiętaj mnie na tym komputerze")
        self.remember.setChecked(True)
        form.addWidget(self.remember)
        self.ok = QPushButton("Zaloguj się")
        self.ok.setObjectName("submit")
        self.ok.setDefault(True)
        self.ok.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ok.setIconSize(QSize(16, 16))
        self.ok.clicked.connect(self._login)
        form.addSpacing(4)
        form.addWidget(self.ok)
        self.more = QPushButton("Połączenie z CRM…")
        self.more.setObjectName("link")
        self.more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more.setToolTip("Adres CRM, klucz publiczny i numer wewnętrzny Zadarma")
        self.more.clicked.connect(lambda: self._show_conn(True))
        form.addWidget(self.more)
        body.addLayout(form)

        self._show_conn(not (cfg.crm_url and cfg.crm_key))
        self._show_alert(reason)

        self._result: object = None
        self._poll = QTimer(self, interval=150, timeout=self._poll_result)
        self._angle = 0
        self._spin = QTimer(self, interval=60, timeout=self._spin_tick)
        (self.password if email else self.email).setFocus()

    # ------------------------------------------------------------ pieces
    @staticmethod
    def _field(label: str, edit: QLineEdit) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(6)
        lb = QLabel(label)
        lb.setFont(T.sans(12, 600))
        lb.setStyleSheet(f"color:{T.MUTED2};")
        lb.setBuddy(edit)
        col.addWidget(lb)
        edit.setFont(T.sans(14))
        col.addWidget(edit)
        return col

    def _show_conn(self, on: bool) -> None:
        self.conn.setVisible(on)
        self.more.setVisible(not on)
        QTimer.singleShot(0, self.adjustSize)

    def _show_alert(self, msg: str) -> None:
        self.alert_text.setText(msg)
        self.alert.setVisible(bool(msg))
        QTimer.singleShot(0, self.adjustSize)

    def _mark_error(self, on: bool) -> None:
        self.password.setProperty("error", "true" if on else "false")
        self.password.style().unpolish(self.password)
        self.password.style().polish(self.password)

    def _toggle_password(self) -> None:
        show = self.password.echoMode() == QLineEdit.EchoMode.Password
        self.password.setEchoMode(QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password)
        self.eye.setIcon(T.icon("eye_off" if show else "eye", T.MUTED, 16))
        self.eye.setText("Ukryj hasło" if show else "Pokaż hasło")

    def _busy(self, on: bool) -> None:
        for w in (self.email, self.password, self.url, self.key, self.sip, self.remember):
            w.setEnabled(not on)
        self.ok.setEnabled(not on)
        self.ok.setText("Logowanie…" if on else "Zaloguj się")
        if on:
            self._spin.start()
            self._spin_tick()
        else:
            self._spin.stop()
            self.ok.setIcon(QIcon())

    def _spin_tick(self) -> None:
        self._angle = (self._angle + 30) % 360
        pm: QPixmap = T.pixmap("spinner", T.ACCENT, 16).transformed(
            QTransform().rotate(self._angle), Qt.TransformationMode.SmoothTransformation)
        ic = QIcon()
        ic.addPixmap(pm, QIcon.Mode.Disabled)  # the button is disabled while signing in: no grey-out
        self.ok.setIcon(ic)

    # ------------------------------------------------------------ dragging the frameless window
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and e.position().y() < 40:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    # ------------------------------------------------------------ sign-in
    def connection(self) -> dict:
        # empty = the CRM built into the app
        return {"crm_url": self.url.text().strip().rstrip("/") or CRM_URL, "crm_key": self.key.text().strip() or CRM_KEY,
                "crm_sip": self.sip.text().strip()}

    def _login(self) -> None:
        conn = self.connection()
        email, password = self.email.text().strip(), self.password.text()
        if not (email and password):
            return self._show_alert("Wpisz login i hasło.")
        self._show_alert("")
        self._mark_error(False)
        try:
            sb = Supabase(conn["crm_url"], conn["crm_key"])
        except CrmError as e:  # a secret key pasted by mistake
            self._show_conn(True)
            return self._show_alert(str(e))
        self._busy(True)
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
        self._busy(False)
        if isinstance(self._result, Supabase):
            self.sb = self._result
            self.accept()
            return
        e = self._result
        if getattr(e, "status", 0) in (400, 401):
            self._show_alert("Nieprawidłowy login lub hasło. Spróbuj ponownie.")
            self._mark_error(True)
            self.password.clear()
            self.password.setFocus()
        else:
            self._show_alert(str(e))

    def done(self, r: int) -> None:
        self._poll.stop()
        self._spin.stop()
        super().done(r)
