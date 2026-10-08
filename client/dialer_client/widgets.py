"""Overlay building blocks, one class per mockup board."""
from __future__ import annotations

import time
from datetime import date, datetime
from html import escape

from PyQt6.QtCore import QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from . import theme as T


# ------------------------------------------------------------------ primitives
def text(s: str, font, color: str, wrap: bool = False) -> QLabel:
    lb = QLabel(s)
    lb.setFont(font)
    lb.setStyleSheet(f"color:{color};background:transparent;")
    lb.setWordWrap(wrap)
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    if wrap:
        lb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return lb


def button(label: str = "", *, icon: str | None = None, bg: str = "transparent", fg: str = T.TEXT,
           border: str | None = None, h: int = 32, w: int | None = None, radius: int = 9, weight: int = 600,
           icon_size: int = 13, tip: str = "") -> QPushButton:
    b = QPushButton(label)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # never steal focus from the softphone/CRM
    b.setFont(T.sans(12, weight))
    b.setFixedHeight(h)
    if w:
        b.setFixedWidth(w)
    if icon:
        b.setIcon(T.icon(icon, fg, icon_size))
        b.setIconSize(QSize(icon_size, icon_size))
    if tip:
        b.setToolTip(tip)
    bd = f"1px solid {border}" if border else "0"
    pad = "0" if w else "0 12px"
    hover = QColor(bg).lighter(118).name() if bg != "transparent" else "rgba(255,255,255,0.06)"
    b.setStyleSheet(f"QPushButton{{background:{bg};color:{fg};border:{bd};border-radius:{radius}px;padding:{pad};}}"
                    f"QPushButton:hover{{background:{hover};}}")
    return b


def set_button_fg(b: QPushButton, icon_name: str, fg: str, size: int) -> None:
    b.setIcon(T.icon(icon_name, fg, size))


class IconLabel(QLabel):
    def __init__(self, name: str, color: str, size: int):
        super().__init__()
        self.setPixmap(T.pixmap(name, color, size))
        self.setFixedSize(size, size)
        self.setStyleSheet("background:transparent;")


class Dot(QWidget):
    def __init__(self, color: str, size: int = 8, halo: bool = False):
        super().__init__()
        self.color, self.size_, self.halo = color, size, halo
        pad = 4 if halo else 0
        self.setFixedSize(size + 2 * pad, size + 2 * pad)

    def set_color(self, color: str, halo: bool | None = None) -> None:
        self.color = color
        if halo is not None:
            self.halo = halo
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        c = QColor(self.color)
        r = QRectF(self.rect())
        if self.halo:
            h = QColor(c)
            h.setAlphaF(0.18)
            p.setBrush(h)
            p.drawEllipse(r)
        p.setBrush(c)
        d = self.size_
        p.drawEllipse(QRectF((r.width() - d) / 2, (r.height() - d) / 2, d, d))


class Meter(QWidget):
    """3-bar level meter (mic = accent, line = client colour)."""

    def __init__(self, color: str, height: int = 14):
        super().__init__()
        self.color, self.h, self.level = color, height, 0.0
        self.setFixedSize(3 * 3 + 2 * 2 + 5, height)
        self._shape = (0.4, 0.85, 0.55)

    def set_level(self, rms: float) -> None:
        # rms 0..1 -> 0..1 with a speech-friendly curve
        lvl = min(1.0, (rms * 9) ** 0.6) if rms > 0.004 else 0.0
        if abs(lvl - self.level) > 0.03:
            self.level = lvl
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        for i, k in enumerate(self._shape):
            h = max(3.0, self.h * k * self.level) if self.level else 3.0
            p.setBrush(QColor(self.color if self.level else T.METER_OFF))
            p.drawRoundedRect(QRectF(i * 5, self.h - h, 3, h), 1.5, 1.5)


# ------------------------------------------------------------------ card base
class Card(QFrame):
    """Rounded surface with an optional bottom auto-hide progress bar (pauses on hover)."""

    expired = pyqtSignal()

    def __init__(self, *, bg: str = T.SURFACE, border: str = T.BORDER, radius: int = 16, width: int = T.WIDGET_W):
        super().__init__()
        self.bg, self.border, self.radius = bg, border, radius
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        if width:
            self.setFixedWidth(width)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 16, 16, 16)
        self.body.setSpacing(10)
        self._progress = None  # 0..1 remaining
        self._bar_color = T.ACCENT
        self._total = 0.0
        self._left = 0.0
        self._hover = False
        self._timer = QTimer(self, interval=50, timeout=self._tick)
        self._last = 0.0

    def start_autohide(self, seconds: float, color: str) -> None:
        self._total = self._left = seconds
        self._bar_color = color
        self._progress = 1.0
        self._last = time.monotonic()
        self._timer.start()

    def stop_autohide(self) -> None:
        self._timer.stop()
        self._progress = None
        self.update()

    def _tick(self) -> None:
        now = time.monotonic()
        if not self._hover:
            self._left -= now - self._last
        self._last = now
        self._progress = max(0.0, self._left / self._total)
        self.update()
        if self._left <= 0:
            self._timer.stop()
            self.expired.emit()

    def enterEvent(self, e):
        self._hover = True
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        super().leaveEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, self.radius, self.radius)
        p.fillPath(path, QColor(self.bg))
        if self._progress is not None:
            p.save()
            p.setClipPath(path)
            p.fillRect(QRectF(0, self.height() - 2, self.width() * self._progress, 2), QColor(self._bar_color))
            p.restore()
        p.setPen(QPen(QColor(self.border), 1))
        p.drawPath(path)


def header_row(label: str, color: str, right: str = "", icon: str | None = None) -> tuple[QHBoxLayout, QLabel, QLabel]:
    row = QHBoxLayout()
    row.setSpacing(8)
    if icon:
        row.addWidget(IconLabel(icon, color, 14))
    left = text(label, T.label_font(), color)
    row.addWidget(left)
    row.addStretch(1)
    r = text(right, T.sans(11), T.MUTED)
    row.addWidget(r)
    return row, left, r


# ------------------------------------------------------------------ hint
class HintCard(Card):
    copied = pyqtSignal(str)              # hint_id
    feedback = pyqtSignal(str, bool)      # hint_id, useful

    def __init__(self, data: dict, *, in_panel: bool = False):
        super().__init__(bg=T.SURFACE_IN_PANEL if in_panel else T.SURFACE,
                         border="#262A30" if in_panel else T.BORDER,
                         radius=14 if in_panel else 16, width=0 if in_panel else T.WIDGET_W)
        self.data = data
        self.hint_id = data.get("id", "")
        self.variants = data.get("variants") or [data.get("hint", "")]
        self.idx = 0
        cat_label, color = T.CATEGORY.get(data.get("category"), ("PODPOWIEDŹ", T.ACCENT))
        self.color = color
        topic = (data.get("topic") or "").upper()
        match = data.get("match")
        row, _, _ = header_row(f"{cat_label} · {topic}" if topic else cat_label, color,
                               f"dopasowanie {round(match * 100)}%" if match else "")
        self.body.addLayout(row)
        if data.get("quote"):
            q = data["quote"]
            q = q if len(q) <= 110 else q[:107].rstrip() + "…"
            self.body.addWidget(text(f"„{q}”", T.serif(14, italic=True), T.MUTED2, wrap=True))
        self.hint_lbl = text(self.variants[0], T.serif(18), T.TEXT_STRONG, wrap=True)
        self.hint_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.addWidget(self.hint_lbl)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.setContentsMargins(0, 4, 0, 0)
        self.copy_btn = button("Kopiuj", icon="copy", bg=T.ACCENT, fg=T.ACCENT_INK)
        self.copy_btn.clicked.connect(self._copy)
        actions.addWidget(self.copy_btn)
        self.var_btn = None
        if len(self.variants) > 1:
            self.var_btn = button("", bg="transparent", fg=T.TEXT, border=T.BORDER_SOFT, weight=500)
            self.var_btn.clicked.connect(self._next_variant)
            self._update_variant_btn()
            actions.addWidget(self.var_btn)
        actions.addStretch(1)
        self.up = button(icon="up", fg=T.MUTED, w=32, icon_size=15, tip="Przydatna podpowiedź")
        self.down = button(icon="down", fg=T.MUTED, w=32, icon_size=15, tip="Podpowiedź nie pasuje")
        self.up.clicked.connect(lambda: self._vote(True))
        self.down.clicked.connect(lambda: self._vote(False))
        actions.addWidget(self.up)
        actions.addWidget(self.down)
        self.body.addLayout(actions)

    @property
    def current_text(self) -> str:
        return self.variants[self.idx]

    def _update_variant_btn(self) -> None:
        # "Inny wariant 1/3" with the counter in muted colour
        self.var_btn.setText(f"Inny wariant  {self.idx + 1}/{len(self.variants)}")

    def _next_variant(self) -> None:
        self.idx = (self.idx + 1) % len(self.variants)
        self.hint_lbl.setText(self.current_text)
        self._update_variant_btn()

    def _copy(self) -> None:
        from PyQt6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.current_text)
        self.copy_btn.setText("Skopiowano")
        self.copied.emit(self.hint_id)

    def _vote(self, useful: bool) -> None:
        set_button_fg(self.up, "up", T.ACCENT if useful else T.MUTED, 15)
        set_button_fg(self.down, "down", T.ERROR if not useful else T.MUTED, 15)
        self.feedback.emit(self.hint_id, useful)


# ------------------------------------------------------------------ client card
# kind -> badge text, badge colour, tile colour
CLIENT_KIND = {"subscriber": ("ABONAMENT", T.ACCENT), "client": ("KLIENT", T.ACCENT),
               "lead": ("LEAD", T.OPERATOR), "contact": ("KONTAKT", T.MUTED2), "unknown": ("NOWY", T.MUTED2)}
TICKET_STATUS = {"nowe": ("nowe", T.ACCENT), "w_toku": ("w toku", T.OPERATOR),
                 "czeka_na_klienta": ("czeka na klienta", T.CLIENT), "do_opracowania": ("do opracowania", T.MUTED2)}
MONTHS = ("Styczeń", "Luty", "Marzec", "Kwiecień", "Maj", "Czerwiec", "Lipiec", "Sierpień", "Wrzesień",
          "Październik", "Listopad", "Grudzień")


def _rgba(color: str, alpha: float) -> str:
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


def _hours(h: float) -> str:
    return (f"{h:.1f}".rstrip("0").rstrip(".") + " h").replace(".", ",")


def _ago(iso: str) -> str:
    try:
        days = (date.today() - date.fromisoformat(iso[:10])).days
    except ValueError:
        return ""
    return "dziś" if days <= 0 else "wczoraj" if days == 1 else f"{days} dni temu"


WEEKDAYS = ("poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela")


def _when(iso: str) -> str:
    """'dziś, 13:00', 'wczoraj, 9:05', 'środa, 17:34' within a week, else '01.10.2026, 13:00' (local time)."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return _pl_date(iso)
    if len(iso) <= 10:  # a bare date, no time to show
        return _pl_date(iso)
    dt = dt.astimezone() if dt.tzinfo else dt
    days = (date.today() - dt.date()).days
    day = ("dziś" if days <= 0 else "wczoraj" if days == 1 else WEEKDAYS[dt.weekday()] if days < 7
           else dt.strftime("%d.%m.%Y"))
    return f"{day}, {dt.hour}:{dt.minute:02d}"


def _pl_date(iso: str) -> str:
    try:
        return date.fromisoformat(iso[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return iso


def mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone or "" if ch.isdigit())[-9:]
    return f"+48 {digits[:3]} *** {digits[6:]}" if len(digits) == 9 else ""


class HourCells(QWidget):
    """Retainer hours as a row of cells: 1 cell = 1 h of the package (1/20 above 20 h), lit = left."""

    def __init__(self, limit: float, left: float, color: str, over: bool):
        super().__init__()
        self.n = max(1, min(20, int(round(limit))))
        self.lit = left / (limit / self.n) if limit else 0
        self.color, self.over = color, over
        self.setFixedHeight(10)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        gap = 3
        w = (self.width() - gap * (self.n - 1)) / self.n
        for i in range(self.n):
            r = QRectF(i * (w + gap), 0, w, self.height())
            path = QPainterPath()
            path.addRoundedRect(r, 3, 3)
            p.setClipPath(path)
            p.fillRect(r, QColor(255, 107, 107, 71) if self.over else QColor("#252930"))
            fill = 0.0 if self.over else max(0.0, min(1.0, self.lit - i))
            if fill:
                p.fillRect(QRectF(r.x(), 0, r.width() * fill, r.height()), QColor(self.color))
        p.setClipping(False)


def pill(s: str, fg: str, bg: str, px: int = 10, spacing: float = 8) -> QLabel:
    lb = QLabel(s)
    lb.setFont(T.sans(px, 700, spacing))
    lb.setStyleSheet(f"color:{fg};background:{bg};border-radius:9px;padding:3px 8px;")
    lb.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return lb


class ClientCard(Card):
    """Who is calling, straight from the CRM (no AI). Stays next to the bar for the whole call.

    Geist only, no serif: it must not look like an AI hint. Sections show only when there is
    something to show: hours of the retainer, open tickets, the last call with what was agreed.
    """

    closed = pyqtSignal()

    def __init__(self, data: dict):
        super().__init__(bg="#0F1215")
        self.body.setSpacing(14)
        self.data = data
        kind = data.get("kind") or "unknown"
        if kind == "client" and data.get("subscriber"):
            kind = "subscriber"
        badge, color = CLIENT_KIND.get(kind, CLIENT_KIND["unknown"])
        known = kind != "unknown" or data.get("title") or data.get("person")

        # header: tile, name + contact, badge + source, close
        head = QHBoxLayout()
        head.setSpacing(12)
        name = data.get("title") or data.get("person") or mask_phone(data.get("phone", "")) or "Nieznany rozmówca"
        contact = data.get("person") if data.get("title") else ("" if known else "Numer spoza CRM")
        tile = QLabel(name[:1].upper() if known else "?")
        tile.setFixedSize(40, 40)
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tile.setFont(T.sans(16, 700))
        tile_bg = {"lead": "#1B2433", "contact": "#1F2227", "unknown": "#1F2227"}.get(kind, "#1F2A28")
        tile.setStyleSheet(f"color:{color if kind not in ('contact', 'unknown') else T.MUTED};"
                           f"background:{tile_bg};border-radius:12px;")
        head.addWidget(tile, 0, Qt.AlignmentFlag.AlignVCenter)
        who = QVBoxLayout()
        who.setSpacing(1)
        title = ElidedLabel(name, T.sans(18, 700, -2), T.TEXT_STRONG)
        title.setToolTip(name)
        who.addWidget(title)
        if contact:
            who.addWidget(ElidedLabel(contact, T.sans(13), T.MUTED2))
        head.addLayout(who, 1)
        meta = QVBoxLayout()
        meta.setSpacing(4)
        meta.addWidget(pill(badge, color, _rgba(color, 0.13) if kind != "unknown" else "#1F2227"),
                       0, Qt.AlignmentFlag.AlignRight)
        src = "po nazwie" if data.get("via") == "name" else "z kontaktów" if kind == "contact" else \
            "z CRM" if known else ""
        if src:
            meta.addWidget(text(src, T.sans(10), T.FAINT), 0, Qt.AlignmentFlag.AlignRight)
        head.addLayout(meta)
        close = button(icon="close", fg=T.FAINT, w=28, h=28, radius=8, icon_size=13, tip="Zwiń kartę klienta")
        close.clicked.connect(self.closed.emit)
        head.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self.body.addLayout(head)

        if not known:
            self.body.addWidget(text("Tego numeru nie ma w bazie. Karta uzupełni się, gdy rozmówca poda nazwę "
                                     "firmy.", T.sans(14), T.TEXT_SOFT, wrap=True))
            return

        hours = data.get("hours") or {}
        if hours.get("limit"):
            self.body.addWidget(self._hours_box(hours))
        elif kind == "lead":
            since = f" · lead od {_pl_date(data['lead_since'])[:5]}" if data.get("lead_since") else ""
            self.body.addWidget(self._plain_box(f"Bez abonamentu{since}"))
        elif kind == "subscriber":
            self.body.addWidget(self._plain_box("Klient abonamentowy · bez limitu godzin"))

        if kind in ("subscriber", "client", "lead"):
            items = data.get("ticket_items") or []
            sec = self._section("ZGŁOSZENIA", str(len(items)))
            for t in items[:3]:
                row = QHBoxLayout()
                row.setSpacing(10)
                num = text(f"#{t['number']}", T.sans(13), T.FAINT)
                num.setMinimumWidth(26)
                row.addWidget(num)
                row.addWidget(ElidedLabel(t["title"], T.sans(13), T.TEXT), 1)
                label, c = TICKET_STATUS.get(t["status"], (t["status"].replace("_", " "), T.MUTED))
                row.addWidget(StatusDot(label, c))
                sec.addLayout(row)
            if not items:
                sec.addWidget(text("Brak otwartych zgłoszeń", T.sans(13), T.FAINT))
            self.body.addLayout(sec)

        deal = (data.get("deals") or [""])[0]
        if data.get("last_call_at") or data.get("callback") or (deal and kind != "lead"):
            self.body.addWidget(self._sep())
        if data.get("last_call_at"):
            when = _when(data["last_call_at"])
            ago = _ago(data["last_call_at"])
            sec = self._section("OSTATNIA ROZMOWA", "", "" if when.split(",")[0] in (ago, *WEEKDAYS) else ago)
            line = QLabel(f'<span style="color:{T.MUTED2}">{when}</span> · '
                          f'{escape(data.get("last_call_title") or "")}')
            line.setFont(T.sans(13))
            line.setStyleSheet(f"color:{T.TEXT};background:transparent;")
            line.setWordWrap(True)
            sec.addWidget(line)
            for item in data.get("promises") or []:
                sec.addWidget(Promise(item))
            self.body.addLayout(sec)
        if data.get("callback") or (deal and kind != "lead"):
            sec = self._section("DO ZROBIENIA")
            if data.get("callback"):
                sec.addWidget(text(data["callback"], T.sans(13), T.TEXT, wrap=True))
            if deal and kind != "lead":
                sec.addWidget(text(f"Szansa sprzedaży: {deal}", T.sans(13), T.TEXT_SOFT, wrap=True))
            self.body.addLayout(sec)

    @staticmethod
    def _sep() -> QFrame:
        f = QFrame()
        f.setFixedHeight(1)
        f.setStyleSheet("background:#1F2227;")
        return f

    @staticmethod
    def _section(label: str, count: str = "", right: str = "") -> QVBoxLayout:
        sec = QVBoxLayout()
        sec.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(text(label, T.label_font(), T.MUTED))
        if count:
            c = QLabel(count)
            c.setFont(T.sans(11, 700))
            c.setAlignment(Qt.AlignmentFlag.AlignCenter)
            c.setMinimumWidth(18)
            c.setFixedHeight(18)
            c.setStyleSheet(f"color:{T.TEXT_SOFT};background:#1F2329;border-radius:9px;padding:0 5px;")
            row.addWidget(c)
        row.addStretch(1)
        if right:
            row.addWidget(text(right, T.sans(11), T.FAINT))
        sec.addLayout(row)
        return sec

    @staticmethod
    def _plain_box(s: str) -> QWidget:
        box = Card(bg=T.SURFACE_IN_PANEL, border=T.SURFACE_IN_PANEL, radius=12, width=0)
        box.body.setContentsMargins(12, 12, 12, 12)
        box.body.addWidget(text(s, T.sans(13), T.MUTED2, wrap=True))
        return box

    @staticmethod
    def _hours_box(h: dict) -> QWidget:
        limit, left, used, pct = h["limit"], h.get("left", 0.0), h.get("used", 0.0), h.get("pct", 0)
        over = pct >= 100 and used > limit
        share = left / limit if limit else 0
        color = T.ERROR if over or share <= 0.10 else T.CLIENT if share <= 0.30 else T.ACCENT
        box = Card(bg=T.SURFACE_IN_PANEL, border=T.SURFACE_IN_PANEL, radius=12, width=0)
        box.body.setContentsMargins(12, 12, 12, 12)
        box.body.setSpacing(9)
        top = QHBoxLayout()
        top.setSpacing(8)
        big = text(("−" + _hours(used - limit)) if over else _hours(left), T.sans(26, 800, -3), color)
        top.addWidget(big, 0, Qt.AlignmentFlag.AlignBaseline)
        cap = f"ponad pakiet {_hours(limit)}" if over else f"zostało z {_hours(limit)}"
        top.addWidget(text(cap, T.sans(13), T.MUTED2), 0, Qt.AlignmentFlag.AlignBaseline)
        top.addStretch(1)
        top.addWidget(text(f"{pct}% wykorzystane", T.sans(12), T.MUTED), 0, Qt.AlignmentFlag.AlignBaseline)
        box.body.addLayout(top)
        cells = QHBoxLayout()
        cells.setSpacing(4)
        cells.addWidget(HourCells(limit, left, color, over), 1)
        if over:
            cells.addWidget(pill("+" + _hours(used - limit), "#FF8A8A", _rgba(T.ERROR, 0.16), 11, 0))
        box.body.addLayout(cells)
        today = date.today()
        foot = QHBoxLayout()
        foot.addWidget(text(MONTHS[today.month - 1], T.sans(11), T.FAINT))
        foot.addStretch(1)
        foot.addWidget(text(f"odnowienie 1.{today.month % 12 + 1:02d}", T.sans(11), T.FAINT))
        box.body.addLayout(foot)
        return box


class ElidedLabel(QLabel):
    """One line, cut with … to the available width."""

    def __init__(self, s: str, font, color: str):
        super().__init__()
        self.full = s
        self.setFont(font)
        self.setStyleSheet(f"color:{color};background:transparent;")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setText(s)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.setText(self.fontMetrics().elidedText(self.full, Qt.TextElideMode.ElideRight, self.width()))


class StatusDot(QWidget):
    def __init__(self, label: str, color: str):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)
        dot = QLabel()
        dot.setFixedSize(6, 6)
        dot.setStyleSheet(f"background:{color};border-radius:3px;")
        row.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(text(label, T.sans(11, 600), color))


class Promise(QWidget):
    """An agreed item from the last call, drawn as an empty checkbox."""

    def __init__(self, s: str):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(9)
        box = QLabel()
        box.setFixedSize(16, 16)
        box.setStyleSheet("border:1.5px solid #3A3F47;border-radius:5px;background:transparent;")
        row.addWidget(box, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(text(s, T.sans(13), T.TEXT, wrap=True), 1)


# ------------------------------------------------------------------ errors
INFO_KINDS = {"loading", "update"}  # not an error: shown in the accent colour

ERRORS = {
    "server": ("PODPOWIEDZI CHWILOWO NIEDOSTĘPNE",
               "Podpowiedzi są chwilowo niedostępne. Transkrypcja trwa dalej — gdy połączenie wróci, "
               "podpowiedzi nadrobią rozmowę.", "Połącz ponownie"),
    "no_llm": ("PODPOWIEDZI NIEDOSTĘPNE",
               "Gemini odrzuca zapytania. Transkrypcja działa, ale podpowiedzi nie będą się pojawiać. "
               "Sprawdź klucz Gemini API w ustawieniach.", "Ustawienia"),
    "update": ("DOSTĘPNA NOWA WERSJA",
               "Pobierz i zainstaluj nową wersję EMANAGER Dialer. Aplikacja uruchomi się ponownie "
               "(kilka sekund).", "Zaktualizuj"),
    "loading": ("PRZYGOTOWANIE ROZPOZNAWANIA MOWY",
                "Pierwsze uruchomienie pobiera model rozpoznawania mowy (~670 MB), to może potrwać kilka "
                "minut. Kolejne starty zajmują kilkanaście sekund.", "Pokaż dziennik"),
    "local_failed": ("NIE UDAŁO SIĘ URUCHOMIĆ PODPOWIEDZI",
                     "Rozpoznawanie mowy nie wystartowało. Przy pierwszym uruchomieniu potrzebny jest internet "
                     "do pobrania modelu. Szczegóły są w dzienniku.", "Pokaż dziennik"),
    "no_rag": ("BAZA WIEDZY NIEDOSTĘPNA",
               "Nie udało się połączyć z bazą wiedzy. Transkrypcja i podpowiedzi działają, ale bez bazy "
               "wiedzy i bez zapisu rozmów. Sprawdź adres bazy w ustawieniach.", "Ustawienia"),
    "no_line": ("NIE SŁYCHAĆ LINII",
                "Dźwięk rozmówcy nie dociera. Sprawdź, czy Zadarma wysyła dźwięk na wybrane urządzenie.",
                "Wybierz urządzenie"),
    "no_zadarma": ("NIE ZNALEZIONO ZADARMA",
                   "Uruchom Zadarma Softphone — EMANAGER Dialer połączy się z rozmową automatycznie.",
                   "Sprawdź ponownie"),
    "no_audio": ("BRAK URZĄDZENIA AUDIO",
                 "Nie udało się otworzyć mikrofonu lub wyjścia dźwięku. Wybierz urządzenie w ustawieniach.",
                 "Wybierz urządzenie"),
}


class ErrorCard(Card):
    action = pyqtSignal(str)     # error kind
    dismissed = pyqtSignal(str)

    def __init__(self, kind: str, note: str = ""):
        info = kind in INFO_KINDS
        super().__init__(border=T.BORDER if info else T.ERROR_BORDER)
        self.kind = kind
        title, body, act = ERRORS[kind]
        row, _, self.note = header_row(title, T.ACCENT if info else T.ERROR, note, icon="alert")
        self.body.addLayout(row)
        self.body.addWidget(text(body, T.serif(18), T.TEXT_STRONG, wrap=True))
        self.bar = ProgressLine()
        self.bar.hide()
        self.body.addWidget(self.bar)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.setContentsMargins(0, 4, 0, 0)
        b = button(act, icon="retry", bg=T.ACCENT if info else T.ERROR, fg=T.ACCENT_INK if info else T.ERROR_INK)
        b.clicked.connect(lambda: self.action.emit(self.kind))
        hide = button("Ukryj", fg=T.TEXT, border=T.BORDER_SOFT, weight=500)
        hide.clicked.connect(lambda: self.dismissed.emit(self.kind))
        actions.addWidget(b)
        actions.addWidget(hide)
        actions.addStretch(1)
        self.body.addLayout(actions)

    def set_note(self, note: str) -> None:
        self.note.setText(note)

    def set_progress(self, fraction: float | None) -> None:
        """A thin bar under the text (e.g. the model download); None hides it."""
        self.bar.setVisible(fraction is not None)
        if fraction is not None:
            self.bar.set_value(fraction)


class ProgressLine(QWidget):
    def __init__(self):
        super().__init__()
        self.value = 0.0
        self.setFixedHeight(6)

    def set_value(self, v: float) -> None:
        self.value = max(0.0, min(1.0, v))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        p.setBrush(QColor(T.BORDER_SOFT))
        p.drawRoundedRect(r, 3, 3)
        if self.value > 0:
            p.setBrush(QColor(T.ACCENT))
            p.drawRoundedRect(QRectF(r.x(), r.y(), max(6.0, r.width() * self.value), r.height()), 3, 3)


# ------------------------------------------------------------------ call summary
class SummaryCard(Card):
    save_crm = pyqtSignal()
    open_transcript = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, data: dict):
        super().__init__()
        self.body.setSpacing(12)
        self.data = data
        row, _, _ = header_row("ROZMOWA ZAKOŃCZONA", T.OPERATOR,
                               f"czas trwania {T.fmt_seconds(data.get('duration_s', 0))}", icon="check")
        self.body.addLayout(row)
        summary = data.get("summary") or "Brak podsumowania."
        self.body.addWidget(text(summary, T.serif(18), T.TEXT_STRONG, wrap=True))
        grid = QGridLayout()
        grid.setSpacing(8)
        for i, (key, lbl) in enumerate((("hints", "podpowiedzi"), ("used", "użyte"), ("objections", "obiekcje"))):
            tile = Card(bg=T.SURFACE2, border=T.SURFACE2, radius=10, width=0)
            tile.body.setContentsMargins(10, 9, 10, 9)
            tile.body.setSpacing(0)
            tile.body.addWidget(text(str(data.get(key, 0)), T.sans(18, 700), T.TEXT))
            tile.body.addWidget(text(lbl, T.sans(11), T.MUTED))
            grid.addWidget(tile, 0, i)
        self.body.addLayout(grid)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        crm = button("Zapisz w CRM", icon="download", bg=T.TEXT_STRONG, fg=T.BG)
        crm.clicked.connect(self.save_crm.emit)
        tr = button("Otwórz transkrypcję", fg=T.TEXT, border=T.BORDER_SOFT, weight=500)
        tr.clicked.connect(self.open_transcript.emit)
        close = button(icon="close", fg=T.MUTED, w=32, icon_size=14, tip="Zamknij")
        close.clicked.connect(self.closed.emit)
        actions.addWidget(crm)
        actions.addWidget(tr)
        actions.addStretch(1)
        actions.addWidget(close)
        self.body.addLayout(actions)


# ------------------------------------------------------------------ collapsed app bar
class StatusBar(Card):
    """'Aplikacja · zwinięta': 400 × 56. States: call / waiting / offline."""

    pause_toggled = pyqtSignal(bool)
    stop = pyqtSignal()
    expand = pyqtSignal()
    settings = pyqtSignal()

    def __init__(self, *, in_panel: bool = False):
        super().__init__(bg=T.BG if not in_panel else "#16191D", border=T.BORDER if not in_panel else "#262A30",
                         radius=16 if not in_panel else 28, width=0 if in_panel else T.WIDGET_W)
        self.in_panel = in_panel
        self.setFixedHeight(56)
        row = QHBoxLayout()
        row.setContentsMargins(18 if in_panel else 16, 0, 8, 0)
        row.setSpacing(10)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.addLayout(row)
        if not in_panel:
            logo = QPushButton()
            logo.setIcon(T.icon("logo", T.BRAND, 22))
            logo.setIconSize(QSize(22, 22))
            logo.setFixedSize(24, 24)
            logo.setFlat(True)
            logo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            logo.setCursor(Qt.CursorShape.PointingHandCursor)
            logo.setToolTip("Ustawienia")
            logo.setStyleSheet("QPushButton{border:0;background:transparent;}")
            logo.clicked.connect(self.settings.emit)
            row.addWidget(logo)
            sep = QFrame()
            sep.setFixedSize(1, 24)
            sep.setStyleSheet(f"background:{T.DIVIDER};")
            row.addWidget(sep)
        self.dot = Dot(T.BRAND, 10 if in_panel else 8, halo=True)
        row.addWidget(self.dot)
        self.timer_lbl = text("00:00", T.sans(15 if in_panel else 14, 700), T.TEXT)
        row.addWidget(self.timer_lbl)
        self.status_lbl = text("Oczekiwanie na rozmowę", T.sans(12), T.MUTED2)
        row.addWidget(self.status_lbl)
        if in_panel:
            sep = QFrame()
            sep.setFixedSize(1, 24)
            sep.setStyleSheet(f"background:{T.DIVIDER};")
            row.addWidget(sep)
        self.meters = QWidget()
        mrow = QHBoxLayout(self.meters)
        mrow.setContentsMargins(4, 0, 0, 0)
        mrow.setSpacing(5)
        isz = 15 if in_panel else 14
        mrow.addWidget(IconLabel("mic", T.MUTED2, isz))
        self.mic_meter = Meter(T.ACCENT)
        mrow.addWidget(self.mic_meter)
        mrow.addSpacing(4)
        mrow.addWidget(IconLabel("headphones", T.MUTED2, isz))
        self.line_meter = Meter(T.CLIENT)
        mrow.addWidget(self.line_meter)
        row.addWidget(self.meters)
        row.addStretch(1)
        self.offline = QWidget()
        orow = QHBoxLayout(self.offline)
        orow.setContentsMargins(0, 0, 0, 0)
        orow.setSpacing(6)
        orow.addWidget(Dot(T.ERROR, 5))
        orow.addWidget(text("brak\npołączenia", T.sans(11), T.ERROR))
        row.addWidget(self.offline)
        self.latency = QWidget()
        lrow = QHBoxLayout(self.latency)
        lrow.setContentsMargins(0, 0, 0, 0)
        lrow.setSpacing(4)
        lrow.addWidget(IconLabel("bolt", T.ACCENT, 11))
        self.latency_lbl = text("–", T.sans(12), T.ACCENT)
        lrow.addWidget(self.latency_lbl)
        row.addWidget(self.latency)
        if in_panel:
            self.latency.hide()  # shown in the panel header instead
        bs = 40 if in_panel else 38
        br = bs // 2 if in_panel else 11
        self.pause_btn = button(icon="pause", bg="#23262C" if in_panel else T.BUTTON_BG, fg=T.TEXT, w=bs, h=bs,
                                radius=br, icon_size=13, tip="Wstrzymaj słuchanie i podpowiedzi (rozmowa trwa dalej)")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._on_pause)
        row.addWidget(self.pause_btn)
        self.stop_btn = button(icon="stop", bg="#23262C" if in_panel else T.BUTTON_BG, fg=T.ERROR, w=bs, h=bs,
                               radius=br, icon_size=12, tip="Zakończ rozmowę i wyczyść ekran")
        self.stop_btn.clicked.connect(self.stop.emit)
        row.addWidget(self.stop_btn)
        self.expand_btn = button(icon="collapse" if in_panel else "expand", bg=T.TEXT_STRONG, fg=T.BG, w=bs, h=bs,
                                 radius=br, icon_size=16 if in_panel else 15,
                                 tip="Zwiń do panelu" if in_panel else "Rozwiń aplikację")
        self.expand_btn.clicked.connect(self.expand.emit)
        row.addWidget(self.expand_btn)
        self.set_state("waiting")

    def _on_pause(self, paused: bool) -> None:
        set_button_fg(self.pause_btn, "play" if paused else "pause", T.TEXT, 13)
        self.pause_btn.setToolTip("Wznów słuchanie i podpowiedzi" if paused else
                                  "Wstrzymaj słuchanie i podpowiedzi (rozmowa trwa dalej)")
        self.set_state(self.state)
        self.pause_toggled.emit(paused)

    def set_paused(self, paused: bool) -> None:
        if self.pause_btn.isChecked() != paused:
            self.pause_btn.setChecked(paused)

    def set_state(self, state: str) -> None:
        """call | waiting | offline (offline keeps timer if a call is running)."""
        self.state = state
        in_call = state in ("call", "offline_call")
        self.dot.set_color(T.BRAND if in_call else T.MUTED, halo=in_call)
        self.timer_lbl.setVisible(in_call)
        self.meters.setVisible(in_call)
        paused = in_call and self.pause_btn.isChecked()
        self.status_lbl.setVisible(state == "waiting" or paused)
        self.status_lbl.setText("wstrzymano" if paused else "Oczekiwanie na rozmowę")
        self.meters.setVisible(in_call and not paused)
        self.stop_btn.setVisible(in_call)
        self.offline.setVisible(state.startswith("offline") and not self.in_panel)
        self.latency.setVisible(state == "call" and not self.in_panel)
        if state == "offline":
            self.status_lbl.setVisible(not self.in_panel)
            self.status_lbl.setText("Oczekiwanie na rozmowę")

    def set_time(self, seconds: float) -> None:
        self.timer_lbl.setText(T.fmt_seconds(seconds))

    def set_latency(self, ms: int) -> None:
        self.latency_lbl.setText(T.fmt_latency(ms))

    def set_levels(self, mic: float, line: float) -> None:
        self.mic_meter.set_level(mic)
        self.line_meter.set_level(line)


# ------------------------------------------------------------------ expanded panel
class TranscriptView(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea{background:transparent;} QScrollBar:vertical{width:6px;background:transparent;}"
                           f"QScrollBar::handle:vertical{{background:{T.BORDER};border-radius:3px;}}"
                           "QScrollBar::add-line,QScrollBar::sub-line{height:0;}"
                           # without this Qt fills the track with its default dotted pattern
                           "QScrollBar::add-page,QScrollBar::sub-page{background:none;}")
        inner = QWidget()
        inner.setStyleSheet("background:transparent;")
        self.lay = QVBoxLayout(inner)
        self.lay.setContentsMargins(18, 0, 18, 6)
        self.lay.setSpacing(10)
        self.lay.addStretch(1)  # rows are inserted above it: top-aligned like the mockup
        self.setWidget(inner)
        self.rows: list[QWidget] = []
        # follow new lines while the operator is at the bottom; leave them alone once they scroll up.
        # The range grows only after Qt lays out the wrapped text, so scroll on rangeChanged, not on add().
        self._follow = True
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(lambda v: setattr(self, "_follow", v >= bar.maximum() - 8))
        bar.rangeChanged.connect(lambda _lo, hi: self._follow and bar.setValue(hi))

    def clear(self) -> None:
        for r in self.rows:
            r.deleteLater()
        self.rows.clear()
        self._follow = True

    def add(self, t: float, speaker: str, s: str) -> None:
        row = QWidget()
        g = QGridLayout(row)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(8)
        g.setColumnMinimumWidth(0, 38)
        g.setColumnMinimumWidth(1, 78)
        g.setColumnStretch(2, 1)
        g.addWidget(text(T.fmt_seconds(t), T.sans(11), T.FAINT), 0, 0, Qt.AlignmentFlag.AlignTop)
        who = ("KONSULTANT", T.OPERATOR) if speaker == "operator" else ("KLIENT", T.CLIENT)
        g.addWidget(text(who[0], T.sans(10, 700), who[1]), 0, 1, Qt.AlignmentFlag.AlignTop)
        g.addWidget(text(s, T.serif(14), T.TEXT, wrap=True), 0, 2)
        self.lay.insertWidget(self.lay.count() - 1, row)
        self.rows.append(row)
        if len(self.rows) > 200:
            self.rows.pop(0).deleteLater()
        # older lines fade like in the mockup (1.0 / 0.75 / 0.5)
        for i, r in enumerate(reversed(self.rows)):
            eff = r.graphicsEffect() or QGraphicsOpacityEffect(r)
            eff.setOpacity(1.0 if i == 0 else 0.75 if i == 1 else 0.5)
            r.setGraphicsEffect(eff)


class ExpandedPanel(Card):
    collapse = pyqtSignal()
    minimize = pyqtSignal()
    settings = pyqtSignal()
    pin_toggled = pyqtSignal(bool)

    def __init__(self, opacity: float = 0.96):
        super().__init__(bg=f"rgba(11,12,14,{opacity})", border="#262930", radius=18, width=T.PANEL_W)
        self.bg = QColor(11, 12, 14, int(255 * opacity)).name(QColor.NameFormat.HexArgb)
        self.setFixedHeight(T.PANEL_H)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)

        head = QFrame()
        head.setObjectName("head")
        head.setStyleSheet("#head{background:transparent;border:0;border-bottom:1px solid #1F2227;}")
        h = QHBoxLayout(head)
        h.setContentsMargins(18, 14, 14, 12)
        h.setSpacing(10)
        h.addWidget(IconLabel("logo", T.BRAND, 22))
        h.addWidget(text("EMANAGER Dialer", T.sans(15, 700), T.TEXT))
        h.addStretch(1)
        self.pin = button(icon="pin", bg=T.SURFACE2, fg=T.ACCENT, w=30, h=30, radius=8, icon_size=15,
                          tip="Przypnij na wierzchu")
        self.pin.setCheckable(True)
        self.pin.setChecked(True)
        self.pin.toggled.connect(self._pin)
        h.addWidget(self.pin)
        mn = button(icon="minus", fg=T.MUTED, w=30, h=30, radius=8, icon_size=15, tip="Zminimalizuj")
        mn.clicked.connect(self.minimize.emit)
        h.addWidget(mn)
        st = button(icon="sliders", fg=T.MUTED, w=30, h=30, radius=8, icon_size=15, tip="Ustawienia")
        st.clicked.connect(self.settings.emit)
        h.addWidget(st)
        self.body.addWidget(head)

        info = QHBoxLayout()
        info.setContentsMargins(18, 10, 18, 10)
        info.setSpacing(8)
        self.call_dot = Dot(T.BRAND, 7)
        info.addWidget(self.call_dot)
        self.call_lbl = text("Oczekiwanie na rozmowę", T.sans(12, 600), T.TEXT)
        info.addWidget(self.call_lbl)
        self.line_lbl = text("· Zadarma", T.sans(12), T.MUTED2)
        info.addWidget(self.line_lbl)
        info.addStretch(1)
        info.addWidget(IconLabel("bolt", T.ACCENT, 12))
        self.lat_lbl = text("–", T.sans(12), T.ACCENT)
        info.addWidget(self.lat_lbl)
        self.body.addLayout(info)

        self.hint_slot = QVBoxLayout()
        self.hint_slot.setContentsMargins(12, 4, 12, 0)
        self.body.addLayout(self.hint_slot)
        self.empty_hint = text("Podpowiedzi pojawią się tutaj w trakcie rozmowy.", T.serif(14, italic=True), T.FAINT,
                               wrap=True)
        self.empty_hint.setContentsMargins(6, 8, 6, 8)
        self.hint_slot.addWidget(self.empty_hint)
        self.hint_card: HintCard | None = None

        self.chips = QHBoxLayout()
        self.chips.setContentsMargins(14, 12, 14, 4)
        self.chips.setSpacing(6)
        self.chips.addStretch(1)
        self.body.addLayout(self.chips)

        lbl = text("TRANSKRYPCJA", T.sans(11, 600, 6), T.FAINT)
        lbl.setContentsMargins(18, 14, 18, 6)
        self.body.addWidget(lbl)
        self.transcript = TranscriptView()
        self.body.addWidget(self.transcript, 1)

        self.bar = StatusBar(in_panel=True)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(12, 10, 12, 12)
        wrap.addWidget(self.bar)
        self.body.addLayout(wrap)
        self.bar.expand.connect(self.collapse.emit)

    def _pin(self, on: bool) -> None:
        set_button_fg(self.pin, "pin", T.ACCENT if on else T.MUTED, 15)
        self.pin.setStyleSheet(self.pin.styleSheet().replace(T.SURFACE2 if not on else "transparent",
                                                             "transparent" if not on else T.SURFACE2))
        self.pin_toggled.emit(on)

    def set_hint(self, card: HintCard | None) -> None:
        if self.hint_card:
            self.hint_card.deleteLater()
        self.hint_card = card
        self.empty_hint.setVisible(card is None)
        if card:
            self.hint_slot.addWidget(card)
        while self.chips.count() > 1:
            w = self.chips.takeAt(0).widget()
            if w:
                w.deleteLater()
        for i, src in enumerate((card.data.get("sources") if card else None) or []):
            chip = button(src["title"], icon="doc" if i == 0 else "lines", bg=T.SURFACE_IN_PANEL, fg=T.TEXT_SOFT,
                          border="#262A30", h=28, radius=14, weight=400, icon_size=12)
            chip.setIcon(T.icon("doc" if i == 0 else "lines", T.OPERATOR if i == 0 else T.ACCENT, 12))
            if src.get("ref", "").startswith("http"):
                from PyQt6.QtCore import QUrl
                from PyQt6.QtGui import QDesktopServices
                chip.clicked.connect(lambda _=False, u=src["ref"]: QDesktopServices.openUrl(QUrl(u)))
            self.chips.insertWidget(self.chips.count() - 1, chip)

    def set_call(self, active: bool, seconds: float = 0, line: str = "Zadarma") -> None:
        self.call_dot.set_color(T.BRAND if active else T.MUTED)
        self.call_lbl.setText(f"Rozmowa {T.fmt_seconds(seconds)}" if active else "Oczekiwanie na rozmowę")
        self.line_lbl.setText(f"· {line}")

    def set_latency(self, ms: int) -> None:
        self.lat_lbl.setText(T.fmt_latency(ms))
