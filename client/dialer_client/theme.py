"""Design tokens from 'EMANAGER Dialer · projekt dla deweloperów'."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtSvg import QSvgRenderer

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# colours
BG = "#0B0C0E"
SURFACE = "#121519"
SURFACE_IN_PANEL = "#15181C"
SURFACE2 = "#1A1D22"
BUTTON_BG = "#1D2026"
BORDER = "#272B31"
BORDER_SOFT = "#2C3037"
DIVIDER = "#2A2D33"
TEXT_STRONG = "#F2EFE9"
TEXT = "#E8E6E3"
TEXT_SOFT = "#C9CCD2"
MUTED = "#8A8F98"
MUTED2 = "#A3A8B0"
FAINT = "#6C717A"
BRAND = "#FF4D4F"
ACCENT = "#2FD6B5"
ACCENT_INK = "#06231D"
CLIENT = "#FFAE5C"
OPERATOR = "#6EA8FF"
ERROR = "#FF6B6B"
ERROR_INK = "#2A0709"
ERROR_BORDER = "#5A2A2E"
METER_OFF = "#3A3E45"

CATEGORY = {
    # category: (label, colour)
    "objection": ("OBIEKCJA", CLIENT),
    "info": ("INFORMACJA", ACCENT),
    "script": ("SKRYPT", OPERATOR),
    "warning": ("UWAGA", ERROR),
}

# sizes / behaviour
WIDGET_W = 400
PANEL_W, PANEL_H = 420, 720
EDGE = 16
GAP = 10

SANS = "Geist"
SERIF = "Source Serif 4"


# hover text for cut-off card lines: dark like the card, wide enough for a call summary
TOOLTIP_QSS = (f"QToolTip{{background:{SURFACE2};color:{TEXT};border:1px solid {BORDER_SOFT};"
               "border-radius:8px;padding:8px 10px;font-size:13px;opacity:255;}")


def load_fonts() -> None:
    global SANS, SERIF
    fams = set()
    for f in (ASSETS / "fonts").glob("*.ttf"):
        fid = QFontDatabase.addApplicationFont(str(f))
        fams.update(QFontDatabase.applicationFontFamilies(fid))
    SANS = "Geist" if "Geist" in fams else "Segoe UI"
    SERIF = "Source Serif 4" if "Source Serif 4" in fams else "Georgia"


def sans(px: int, weight: int = 400, spacing: float | None = None) -> QFont:
    f = QFont(SANS)
    f.setPixelSize(px)
    f.setWeight(QFont.Weight(weight))
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100 + spacing)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def serif(px: int, italic: bool = False) -> QFont:
    f = QFont(SERIF)
    f.setPixelSize(px)
    f.setItalic(italic)
    return f


def label_font() -> QFont:
    """'ETYKIETA TYPU — Geist 700, 11, +6%'."""
    return sans(11, 700, 6)


# ------------------------------------------------------------------ icons (from the mockup)
_S = 'xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' \
     'stroke-linecap="round" stroke-linejoin="round"'
SVG = {
    "pin": f'<svg {_S} stroke-width="2"><path d="M12 17v5"/><path d="M9 3h6l-1 7 4 3H6l4-3z"/></svg>',
    "minus": f'<svg {_S} stroke-width="2"><path d="M5 12h14"/></svg>',
    "sliders": f'<svg {_S} stroke-width="2"><path d="M4 7h10"/><path d="M18 7h2"/><circle cx="16" cy="7" r="2"/>'
               '<path d="M4 17h4"/><path d="M12 17h8"/><circle cx="10" cy="17" r="2"/></svg>',
    "bolt": f'<svg {_S} stroke-width="2.4"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg>',
    "copy": f'<svg {_S} stroke-width="2.2"><rect x="9" y="9" width="12" height="12" rx="2"/>'
            '<path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>',
    "up": f'<svg {_S} stroke-width="2"><path d="M7 10v11"/><path d="M15 5.9 14 10h5.8a2 2 0 0 1 2 2.3l-1.4 7A2 2 0 0 1 '
          '18.4 21H7V10l4.3-7.2A1.7 1.7 0 0 1 15 5.9z"/></svg>',
    "down": f'<svg {_S} stroke-width="2"><path d="M17 14V3"/><path d="M9 18.1 10 14H4.2a2 2 0 0 1-2-2.3l1.4-7A2 2 0 0 1 '
            '5.6 3H17v11l-4.3 7.2A1.7 1.7 0 0 1 9 18.1z"/></svg>',
    "doc": f'<svg {_S} stroke-width="2.2"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/>'
           '<path d="M14 3v6h6"/></svg>',
    "lines": f'<svg {_S} stroke-width="2.2"><path d="M4 6h16"/><path d="M4 12h10"/><path d="M4 18h7"/></svg>',
    "mic": f'<svg {_S} stroke-width="2"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/>'
           '<path d="M12 18v4"/></svg>',
    "headphones": f'<svg {_S} stroke-width="2"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><rect x="3" y="15" width="4" '
                  'height="6" rx="1.5"/><rect x="17" y="15" width="4" height="6" rx="1.5"/></svg>',
    "pause": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" '
             'width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>',
    "stop": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">'
            '<rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
    "play": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">'
            '<path d="M7 4.5v15a1 1 0 0 0 1.5.9l12-7.5a1 1 0 0 0 0-1.8l-12-7.5A1 1 0 0 0 7 4.5z"/></svg>',
    "collapse": f'<svg {_S} stroke-width="2.4"><path d="M4 14h6v6"/><path d="M20 10h-6V4"/><path d="M14 10l7-7"/>'
                '<path d="M3 21l7-7"/></svg>',
    "expand": f'<svg {_S} stroke-width="2.4"><path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/>'
              '<path d="M3 21l7-7"/></svg>',
    "user": f'<svg {_S} stroke-width="2.2"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
    "check": f'<svg {_S} stroke-width="2.2"><path d="M20 6 9 17l-5-5"/></svg>',
    "download": f'<svg {_S} stroke-width="2.2"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>',
    "close": f'<svg {_S} stroke-width="2.2"><path d="M6 6l12 12"/><path d="M18 6 6 18"/></svg>',
    "eye": f'<svg {_S} stroke-width="2"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/>'
           '<circle cx="12" cy="12" r="3"/></svg>',
    "eye_off": f'<svg {_S} stroke-width="2"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/>'
               '<circle cx="12" cy="12" r="3"/><path d="M3 3l18 18"/></svg>',
    "spinner": f'<svg {_S} stroke-width="2.6"><path d="M12 3a9 9 0 1 0 9 9"/></svg>',
    "alert": f'<svg {_S} stroke-width="2.2"><path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 '
             '0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
    "retry": f'<svg {_S} stroke-width="2.2"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>',
    "logo": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="1 1 48 48"><path fill-rule="evenodd" fill="currentColor" '
            'd="M12 4H28a8 8 0 0 1 8 8V36H12a8 8 0 0 1-8-8V12a8 8 0 0 1 8-8Z M11 14h18v6H11Z"/>'
            '<rect x="38" y="38" width="9" height="9" rx="2" fill="currentColor"/></svg>',
    "logo_full": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="1 1 48 48"><path fill-rule="evenodd" '
                 'fill="currentColor" d="M12 4H28a8 8 0 0 1 8 8V36H12a8 8 0 0 1-8-8V12a8 8 0 0 1 8-8Z '
                 'M11.5 13.5h17v5h-17Z M11.5 22.5h10v5h-10Z"/><rect x="38.5" y="38.5" width="8" height="8" rx="2" '
                 'fill="currentColor"/></svg>',
    "logo_16": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="1 1 48 48"><path fill="currentColor" '
               'd="M12 4H28a8 8 0 0 1 8 8V36H12a8 8 0 0 1-8-8V12a8 8 0 0 1 8-8Z"/>'
               '<rect x="38" y="38" width="10" height="10" rx="2" fill="currentColor"/></svg>',
}


@lru_cache(maxsize=256)
def pixmap(name: str, color: str, size: int, dpr: float = 2.0) -> QPixmap:
    svg = SVG[name].replace("currentColor", color)
    r = QSvgRenderer(QByteArray(svg.encode()))
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r.render(p, QRectF(0, 0, size * dpr, size * dpr))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def icon(name: str, color: str, size: int) -> QIcon:
    return QIcon(pixmap(name, color, size))


def app_tile(size: int) -> QPixmap:
    """The app icon: the red logo on a dark rounded tile (exe, taskbar, tray, windows).

    Like the mockup's 64 px tile: radius 16/64, logo 40/64; small sizes get the simpler marks.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    edge = max(1.0, size / 64)
    r = QRectF(edge / 2, edge / 2, size - edge, size - edge)
    p.setPen(QPen(QColor(DIVIDER), edge) if size >= 24 else Qt.PenStyle.NoPen)
    p.setBrush(QColor(SURFACE))
    p.drawRoundedRect(r, size * 16 / 64, size * 16 / 64)
    name = "logo_full" if size >= 32 else "logo" if size >= 24 else "logo_16"
    logo = size * (40 / 64 if size >= 32 else 0.7)
    off = (size - logo) / 2
    svg = SVG[name].replace("currentColor", BRAND)
    QSvgRenderer(QByteArray(svg.encode())).render(p, QRectF(off, off, logo, logo))
    p.end()
    return pm


def app_icon() -> QIcon:
    ic = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        ic.addPixmap(app_tile(size))
    return ic


def fmt_seconds(s: float) -> str:
    s = int(max(0, s))
    return f"{s // 60:02d}:{s % 60:02d}"


def fmt_latency(ms: int) -> str:
    return f"{ms / 1000:.1f} s".replace(".", ",")
