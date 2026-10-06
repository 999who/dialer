"""Frameless, always-on-top, non-activating overlay window.

Collapsed mode: status bar (400 × 56) in a screen corner with a stack of
widgets (hints, errors, call summary) growing away from the corner
(bottom corner -> upwards, top corner -> downwards). The newest widget sits
next to the bar; older ones fade to 45 % and disappear.
Expanded mode: the 420 × 720 panel with the current hint and live transcript.
"""
from __future__ import annotations

import sys

from PyQt6.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QGraphicsOpacityEffect, QVBoxLayout, QWidget

from . import theme as T
from .widgets import Card, ErrorCard, ExpandedPanel, HintCard, StatusBar, SummaryCard

MAX_STACK = 3
CORNERS = {"bottom-right": "Prawy dolny róg", "bottom-left": "Lewy dolny róg",
           "top-right": "Prawy górny róg", "top-left": "Lewy górny róg"}


def make_noactivate(widget: QWidget, taskbar: bool = True) -> None:
    """On Windows Qt's WindowDoesNotAcceptFocus is not always enough: add WS_EX_NOACTIVATE.

    A WS_EX_NOACTIVATE window gets no taskbar button by default, so WS_EX_APPWINDOW puts it back
    when `taskbar` is on; otherwise WS_EX_TOOLWINDOW keeps it tray-only.
    """
    if sys.platform != "win32":
        return
    import ctypes

    GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_APPWINDOW = (
        -20, 0x08000000, 0x00000080, 0x00000008, 0x00040000)
    hwnd = int(widget.winId())
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE | WS_EX_TOPMOST
    style = (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW if taskbar else (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


class Overlay(QWidget):
    hint_copied = pyqtSignal(str)
    hint_feedback = pyqtSignal(str, bool)
    error_action = pyqtSignal(str)
    pause_toggled = pyqtSignal(bool)
    settings_requested = pyqtSignal(QPoint)
    save_crm = pyqtSignal(dict)
    open_transcript = pyqtSignal(dict)

    def __init__(self, corner: str = "bottom-right", hint_seconds: float = 20, summary_seconds: float = 45,
                 opacity: float = 0.96, taskbar: bool = True):
        super().__init__(None)
        self.setWindowTitle("EMANAGER Dialer")
        self.taskbar = taskbar
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint |
                 Qt.WindowType.WindowDoesNotAcceptFocus)
        if not taskbar:  # Qt.Tool = no taskbar button, the tray icon is the only handle
            flags |= Qt.WindowType.Tool
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.corner = corner if corner in CORNERS else "bottom-right"
        self.hint_seconds = hint_seconds
        self.summary_seconds = summary_seconds
        self.expanded = False
        self.errors: dict[str, ErrorCard] = {}
        self.cards: list[Card] = []  # newest last
        self.dismissed_errors: set[str] = set()

        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(0, 0, 0, 0)
        self.root.setSpacing(T.GAP)
        self.root.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)

        self.stack_host = QWidget()
        self.stack = QVBoxLayout(self.stack_host)
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.stack.setSpacing(T.GAP)
        self.bar = StatusBar()
        self.panel = ExpandedPanel(opacity)
        self.panel.hide()

        self.bar.expand.connect(lambda: self.set_expanded(True))
        self.panel.collapse.connect(lambda: self.set_expanded(False))
        self.panel.minimize.connect(self.showMinimized)
        self.bar.pause_toggled.connect(self._pause)
        self.panel.bar.pause_toggled.connect(self._pause)
        self.bar.settings.connect(lambda: self.settings_requested.emit(self.bar.mapToGlobal(QPoint(0, 0))))
        self.panel.settings.connect(lambda: self.settings_requested.emit(self.panel.mapToGlobal(QPoint(300, 50))))
        self.panel.pin_toggled.connect(self._pin)
        self._layout()

    # ------------------------------------------------------------ layout / position
    def _layout(self) -> None:
        for w in (self.stack_host, self.bar, self.panel):
            self.root.removeWidget(w)
        top = self.corner.startswith("top")
        if self.expanded:
            self.root.addWidget(self.panel)
            if top:
                self.root.addWidget(self.stack_host)
            else:
                self.root.insertWidget(0, self.stack_host)
        else:
            order = (self.bar, self.stack_host) if top else (self.stack_host, self.bar)
            for w in order:
                self.root.addWidget(w)
        self._restack()

    def _restack(self) -> None:
        top = self.corner.startswith("top")
        for c in self.cards:
            self.stack.removeWidget(c)
        # newest card next to the bar
        ordered = list(reversed(self.cards)) if top else list(self.cards)
        for c in ordered:
            self.stack.addWidget(c)
        for i, c in enumerate(reversed(self.cards)):
            eff = c.graphicsEffect()
            if not isinstance(eff, QGraphicsOpacityEffect):
                eff = QGraphicsOpacityEffect(c)
                c.setGraphicsEffect(eff)
            eff.setOpacity(1.0 if i == 0 or isinstance(c, ErrorCard) else 0.45)
        self.stack_host.setVisible(any(not (self.expanded and isinstance(c, HintCard)) for c in self.cards))
        QTimer.singleShot(0, self.reposition)

    def reposition(self) -> None:
        self.adjustSize()
        scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        x = scr.left() + T.EDGE if self.corner.endswith("left") else scr.right() - self.width() - T.EDGE + 1
        y = scr.top() + T.EDGE if self.corner.startswith("top") else scr.bottom() - self.height() - T.EDGE + 1
        self.move(x, y)

    def set_corner(self, corner: str) -> None:
        self.corner = corner
        self._layout()

    def set_expanded(self, on: bool) -> None:
        self.expanded = on
        self.bar.setVisible(not on)
        self.panel.setVisible(on)
        # in the expanded panel the current hint lives inside the panel, only errors/summary stack outside
        for c in self.cards:
            c.setVisible(not (on and isinstance(c, HintCard)))
        self._layout()

    def showEvent(self, e):
        super().showEvent(e)
        make_noactivate(self, self.taskbar)
        QTimer.singleShot(0, self.reposition)

    def _pin(self, on: bool) -> None:
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        self.show()

    def _pause(self, paused: bool) -> None:
        self.bar.set_paused(paused)
        self.panel.bar.set_paused(paused)
        self.pause_toggled.emit(paused)

    # ------------------------------------------------------------ cards
    def _push(self, card: Card) -> None:
        self.cards.append(card)
        # keep at most MAX_STACK non-error cards
        regular = [c for c in self.cards if not isinstance(c, ErrorCard)]
        for old in regular[:-MAX_STACK]:
            self._remove(old, restack=False)
        if self.expanded and isinstance(card, HintCard):
            card.setVisible(False)
        self._restack()

    def _remove(self, card: Card, restack: bool = True) -> None:
        if card in self.cards:
            self.cards.remove(card)
            self.stack.removeWidget(card)
            card.deleteLater()
        if restack:
            self._restack()

    def show_hint(self, data: dict) -> None:
        for c in [c for c in self.cards if isinstance(c, SummaryCard)]:
            self._remove(c, restack=False)
        card = HintCard(data)
        _, color = T.CATEGORY.get(data.get("category"), ("", T.ACCENT))
        card.start_autohide(self.hint_seconds, color)
        card.expired.connect(lambda c=card: self._remove(c))
        card.copied.connect(self.hint_copied.emit)
        card.feedback.connect(self.hint_feedback.emit)
        self._push(card)
        panel_card = HintCard(data, in_panel=True)
        panel_card.copied.connect(self.hint_copied.emit)
        panel_card.feedback.connect(self.hint_feedback.emit)
        self.panel.set_hint(panel_card)

    def show_summary(self, data: dict) -> None:
        for c in [c for c in self.cards if isinstance(c, HintCard)]:
            self._remove(c, restack=False)
        card = SummaryCard(data)
        card.start_autohide(self.summary_seconds, T.OPERATOR)
        card.expired.connect(lambda c=card: self._remove(c))
        card.closed.connect(lambda c=card: self._remove(c))
        card.save_crm.connect(lambda d=data: self.save_crm.emit(d))
        card.open_transcript.connect(lambda d=data: self.open_transcript.emit(d))
        self._push(card)
        self.panel.set_hint(None)

    def show_error(self, kind: str, note: str = "") -> None:
        if kind in self.dismissed_errors:
            return
        if kind in self.errors:
            self.errors[kind].set_note(note)
            return
        card = ErrorCard(kind, note)
        card.action.connect(self.error_action.emit)
        card.dismissed.connect(self._dismiss_error)
        self.errors[kind] = card
        self._push(card)

    def clear_error(self, kind: str) -> None:
        self.dismissed_errors.discard(kind)
        card = self.errors.pop(kind, None)
        if card:
            self._remove(card)

    def _dismiss_error(self, kind: str) -> None:
        self.dismissed_errors.add(kind)  # stays hidden until the condition clears and comes back
        card = self.errors.pop(kind, None)
        if card:
            self._remove(card)

    # ------------------------------------------------------------ live state
    def set_call(self, active: bool, seconds: float = 0) -> None:
        offline = self.bar.state.startswith("offline")
        self.bar.set_state(("offline_call" if active else "offline") if offline else ("call" if active else "waiting"))
        self.panel.bar.set_state("call" if active else "waiting")
        self.bar.set_time(seconds)
        self.panel.bar.set_time(seconds)
        self.panel.set_call(active, seconds)

    def set_online(self, online: bool, in_call: bool) -> None:
        self.bar.set_state(("call" if in_call else "waiting") if online else ("offline_call" if in_call else "offline"))

    def set_levels(self, mic: float, line: float) -> None:
        self.bar.set_levels(mic, line)
        self.panel.bar.set_levels(mic, line)

    def set_latency(self, ms: int) -> None:
        self.bar.set_latency(ms)
        self.panel.set_latency(ms)

    def add_transcript(self, t: float, speaker: str, s: str) -> None:
        self.panel.transcript.add(t, speaker, s)

    def clear_transcript(self) -> None:
        self.panel.transcript.clear()
