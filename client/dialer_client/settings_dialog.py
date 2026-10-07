"""Settings window: Gemini API key and model, and the optional knowledge-base database.

Shown on first run (no key yet) and from the menus, so nobody has to edit config.toml by
hand. The key is checked with one small Gemini request before saving.
"""
from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QVBoxLayout)

from . import theme as T

CHECK_TIMEOUT_MS = 20000
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]


def check_gemini(key: str, model: str) -> str:
    """'' if the key and model answer, otherwise a short reason. Blocking: call from a thread."""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=15000))
        client.models.generate_content(model=model, contents="Odpowiedz jednym słowem: OK",
                                       config=types.GenerateContentConfig(max_output_tokens=256))
        return ""
    except Exception as e:
        msg = getattr(e, "message", None) or str(e)
        return f"{getattr(e, 'code', type(e).__name__)} {msg}"[:200]


class SettingsDialog(QDialog):
    def __init__(self, cfg, reason: str = "", parent=None, on_check_updates=None):
        super().__init__(parent)
        self.setWindowTitle("EMANAGER Dialer · ustawienia")
        self.setWindowIcon(T.icon("logo_full", T.BRAND, 64))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(500)
        self.setStyleSheet(
            f"QDialog{{background:{T.SURFACE};}} QLabel{{color:{T.TEXT};}}"
            f"QLineEdit, QComboBox{{background:{T.BG};color:{T.TEXT};border:1px solid {T.BORDER};border-radius:8px;"
            f"padding:7px 9px;selection-background-color:{T.ACCENT};}}"
            f"QComboBox QAbstractItemView{{background:{T.BG};color:{T.TEXT};}}"
            f"QPushButton{{background:{T.BUTTON_BG};color:{T.TEXT};border:1px solid {T.BORDER};"
            f"border-radius:8px;padding:7px 14px;}}"
            f"QPushButton#primary{{background:{T.TEXT_STRONG};color:{T.BG};border:0;font-weight:600;}}"
            f"QPushButton:disabled{{color:{T.FAINT};}}")

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)
        title = QLabel("Podpowiedzi Gemini")
        title.setFont(T.sans(15, 700))
        root.addWidget(title)
        if reason:
            r = QLabel(reason)
            r.setWordWrap(True)
            r.setStyleSheet(f"color:{T.ERROR};")
            root.addWidget(r)

        form = QFormLayout()
        form.setSpacing(8)
        self.key_edit = QLineEdit(cfg.gemini_api_key)
        self.key_edit.setPlaceholderText("AIza…")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.model_box = QComboBox()
        self.model_box.setEditable(True)
        self.model_box.addItems(GEMINI_MODELS if cfg.gemini_model in GEMINI_MODELS
                                else [cfg.gemini_model] + GEMINI_MODELS)
        self.model_box.setCurrentText(cfg.gemini_model)
        form.addRow("Klucz Gemini API", self.key_edit)
        form.addRow("Model", self.model_box)
        root.addLayout(form)
        how = QLabel(f'Klucz: <a style="color:{T.ACCENT};" href="https://aistudio.google.com/apikey">'
                     "aistudio.google.com/apikey</a> → Create API key. Przy pierwszym uruchomieniu aplikacja "
                     "pobierze model rozpoznawania mowy (~670 MB).")
        how.setOpenExternalLinks(True)
        how.setWordWrap(True)
        how.setStyleSheet(f"color:{T.MUTED};")
        root.addWidget(how)

        db_title = QLabel("Baza wiedzy (Supabase, opcjonalnie)")
        db_title.setFont(T.sans(13, 700))
        root.addWidget(db_title)
        self.db_edit = QLineEdit(cfg.database_url)
        self.db_edit.setPlaceholderText("postgresql://postgres.…:hasło@…pooler.supabase.com:6543/postgres")
        self.db_edit.setEchoMode(QLineEdit.EchoMode.Password)
        root.addWidget(self.db_edit)
        db_how = QLabel("Supabase → Connect → Transaction pooler → URI (z hasłem). Puste = podpowiedzi bez bazy "
                        "wiedzy i bez zapisu rozmów.")
        db_how.setWordWrap(True)
        db_how.setStyleSheet(f"color:{T.MUTED};")
        root.addWidget(db_how)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        from . import updater

        ver = QHBoxLayout()
        ver_lbl = QLabel(f"Wersja: {updater.version_label()}")
        ver_lbl.setStyleSheet(f"color:{T.MUTED};")
        ver.addWidget(ver_lbl)
        ver.addStretch(1)
        if on_check_updates is not None:
            upd = QPushButton("Sprawdź aktualizacje")
            upd.clicked.connect(lambda: (self.reject(), on_check_updates()))
            ver.addWidget(upd)
        root.addLayout(ver)

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

        self._result: str | None = None
        self._poll = QTimer(self, interval=200, timeout=self._poll_result)
        self._timeout = QTimer(self, singleShot=True, interval=CHECK_TIMEOUT_MS, timeout=self._on_timeout)

    # ---------------------------------------------------------------- result
    @property
    def gemini_api_key(self) -> str:
        return self.key_edit.text().strip().strip("\"'")

    @property
    def gemini_model(self) -> str:
        return self.model_box.currentText().strip() or GEMINI_MODELS[0]

    @property
    def database_url(self) -> str:
        return self.db_edit.text().strip().strip("\"'")

    def values(self) -> dict:
        return {"gemini_api_key": self.gemini_api_key, "gemini_model": self.gemini_model,
                "database_url": self.database_url}

    # ---------------------------------------------------------------- check
    def _check(self) -> None:
        if not self.gemini_api_key:
            self._set_status("Wpisz klucz Gemini API.", T.ERROR)
            return
        self.check_btn.setEnabled(False)
        self._set_status("Sprawdzanie klucza Gemini…", T.MUTED2)
        self._result = None
        key, model = self.gemini_api_key, self.gemini_model

        def work():
            self._result = check_gemini(key, model)

        threading.Thread(target=work, daemon=True).start()
        self._poll.start()
        self._timeout.start()

    def _poll_result(self) -> None:
        if self._result is None:
            return
        self._stop()
        if self._result:
            self._set_status(f"Gemini odrzucił żądanie: {self._result}", T.ERROR)
        else:
            self._set_status("Klucz działa.", T.ACCENT)
            QTimer.singleShot(500, self.accept)

    def _on_timeout(self) -> None:
        self._stop()
        self._set_status("Gemini nie odpowiada. Sprawdź połączenie z internetem.", T.ERROR)

    def _stop(self) -> None:
        self._poll.stop()
        self._timeout.stop()
        self.check_btn.setEnabled(True)

    def _set_status(self, text: str, color: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{color};")

    def done(self, r: int) -> None:
        self._stop()
        super().done(r)
