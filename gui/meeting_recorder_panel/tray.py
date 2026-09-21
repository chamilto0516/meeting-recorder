"""Shared system-tray controller.

Both the control panel (`app.py`) and the standalone indicator
(`indicator.py`) build their tray icon through `TrayController` so the two
processes never draw or poll state differently. Only one process may show
the tray icon at a time; `tray_lock()` enforces that with a non-blocking
flock next to the session state file, so a CLI-spawned indicator, an
autostart indicator, and the panel can all start without producing more
than one visible icon.
"""
from __future__ import annotations

import fcntl
import os
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from meeting_recorder import gui_api, state

IDLE_GRAY = "#b7b7ba"
RECORD_RED = "#c2352f"
ACCENT = "#5980a6"
DONE_GREEN = "#3f7a4b"

POLL_INTERVAL_MS = 2000
PULSE_INTERVAL_MS = 120
PULSE_CYCLE_MS = 2400  # one full gray -> red -> gray sweep while recording

PHASE_LABELS = {"idle": "Ready", "recording": "Recording", "processing": "Processing"}


def tray_lock() -> Optional[int]:
    """Try to become the process that owns the visible tray icon.

    Returns the held file descriptor, or None if another process already
    holds it. Callers that get None should run without a tray rather than
    contest the lock.
    """
    lock_path = state.state_dir() / "tray.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_tray_lock(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _lerp(start: QColor, end: QColor, t: float) -> QColor:
    return QColor(
        round(start.red() + (end.red() - start.red()) * t),
        round(start.green() + (end.green() - start.green()) * t),
        round(start.blue() + (end.blue() - start.blue()) * t),
    )


def draw_icon(phase: str, tick: int = 0) -> QIcon:
    """Render the 22x22 tray dot for a phase at animation step `tick`."""
    pixmap = QPixmap(22, 22)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    if phase == "recording":
        cycle_steps = max(1, PULSE_CYCLE_MS // PULSE_INTERVAL_MS)
        half = cycle_steps / 2
        position = tick % cycle_steps
        t = (position / half) if position <= half else (2 - position / half)
        color = _lerp(QColor(IDLE_GRAY), QColor(RECORD_RED), t)
    elif phase == "processing":
        color = QColor(ACCENT)
        color.setAlpha(255 if tick % 2 == 0 else 70)
    else:
        color = QColor(IDLE_GRAY)
    painter.setBrush(color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(5, 5, 12, 12)
    painter.end()
    return QIcon(pixmap)


class TrayController(QObject):
    """Owns one `QSystemTrayIcon`, polling session state to drive its phase."""

    stop_requested = Signal()
    open_requested = Signal()
    phase_changed = Signal(str)

    def __init__(self, app: QApplication, *, hide_when_idle: bool = False, parent=None):
        super().__init__(parent)
        self.hide_when_idle = hide_when_idle
        self.phase = "idle"
        self._tick = 0

        self.tray = QSystemTrayIcon(draw_icon("idle"), app)
        self._stop_action = None
        self._build_menu()
        self.tray.activated.connect(self._on_activated)

        self.pulse_timer = QTimer(self)
        self.pulse_timer.timeout.connect(self._on_pulse)
        self.pulse_timer.start(PULSE_INTERVAL_MS)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll)
        self.poll_timer.start(POLL_INTERVAL_MS)

        self._apply_phase("idle")
        if not hide_when_idle:
            self.tray.show()

    def _build_menu(self) -> None:
        menu = QMenu()
        self._stop_action = menu.addAction("Stop", self.stop_requested.emit)
        self._stop_action.setVisible(False)
        menu.addSeparator()
        menu.addAction("Open", self.open_requested.emit)
        menu.addAction("Exit", QApplication.instance().quit)
        self.tray.setContextMenu(menu)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_requested.emit()

    def stopping(self) -> None:
        """Disable Stop the instant it's clicked, ahead of the next poll."""
        self._stop_action.setEnabled(False)

    def poll(self) -> None:
        try:
            session = gui_api.active_session()
        except Exception:
            return
        if session is None:
            phase = "idle"
        elif session.phase == "recording":
            phase = "recording"
        else:
            phase = "processing"
        if phase != self.phase:
            self._apply_phase(phase)

    def _apply_phase(self, phase: str) -> None:
        self.phase = phase
        self._stop_action.setVisible(phase == "recording")
        self._stop_action.setEnabled(True)
        self.tray.setVisible(not (self.hide_when_idle and phase == "idle"))
        self._render()
        self.phase_changed.emit(phase)

    def _on_pulse(self) -> None:
        self._tick += 1
        if self.phase in ("recording", "processing"):
            self._render()

    def _render(self) -> None:
        self.tray.setIcon(draw_icon(self.phase, self._tick))
        self.tray.setToolTip(f"Meeting Recorder — {PHASE_LABELS[self.phase]}")
