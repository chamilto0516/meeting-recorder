from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QRadioButton,
    QStackedWidget, QStyle, QSystemTrayIcon, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from meeting_recorder import gui_api

BG, SURFACE, TEXT, ACCENT, TINT, DARK, BORDER = (
    "#f2f2f3", "#e9e9ea", "#1d1f20", "#5980a6", "#eef6ff", "#1d2d3d", "#c9c9cb"
)
MODES = {"meeting": "Meeting", "game": "Game", "lecture": "Lecture", "cbt": "CBT", "journal": "Journal"}
SUCCESS, PROBLEM = "#3f7a4b", "#a64f4f"


class Worker(QObject):
    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(str, int)

    def __init__(self, action, *args, processing=False, **kwargs):
        super().__init__()
        self.action, self.args, self.kwargs = action, args, kwargs
        self.processing = processing

    def run(self):
        try:
            if self.processing:
                self.completed.emit(self.action(*self.args, self.progress.emit, **self.kwargs))
            else:
                self.completed.emit(self.action(*self.args, **self.kwargs))
        except Exception as exc:  # Expected recorder errors are rendered in history.
            self.failed.emit(str(exc))


class Panel(QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setFixedWidth(340)
        self.setObjectName("panel")
        self.phase, self.active, self.elapsed_seconds, self.worker_thread = "idle", None, 0, None
        self.result_status = "done"
        self.second_enabled = False
        self.microphones, self.systems = [], []
        self.timer = QTimer(self); self.timer.timeout.connect(self._tick)
        self.pulse_timer = QTimer(self); self.pulse_timer.timeout.connect(self._pulse); self.pulse_on = True
        # Query the same PipeWire/Pulse sources as `meeting-recorder list-devices`
        # before building the controls, so combo-box data holds usable source IDs.
        self.refresh_devices()
        self._build()
        self.recover()

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(16, 16, 16, 18); root.setSpacing(14)
        tabs = QHBoxLayout(); self.record_tab = QPushButton("Record"); self.history_tab = QPushButton("History")
        for button in (self.record_tab, self.history_tab): button.setCheckable(True); tabs.addWidget(button)
        self.record_tab.setChecked(True); self.record_tab.clicked.connect(lambda: self.stack.setCurrentIndex(0)); self.history_tab.clicked.connect(self.show_history)
        root.addLayout(tabs)
        self.stack = QStackedWidget(); root.addWidget(self.stack)
        self.record_page = QWidget(); self.record_layout = QVBoxLayout(self.record_page); self.record_layout.setContentsMargins(0, 0, 0, 0)
        self.stack.addWidget(self.record_page)
        self.history_page = QWidget(); self.history_layout = QVBoxLayout(self.history_page); self.history_layout.setContentsMargins(0, 0, 0, 0)
        self.history_list = QListWidget(); self.history_layout.addWidget(self.history_list); self.stack.addWidget(self.history_page)
        self.render_phase()

    def _clear(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget, child_layout = item.widget(), item.layout()
            if widget:
                widget.setParent(None)
                widget.deleteLater()
            elif child_layout:
                # Form fields are nested layouts.  Removing only their parent
                # layout leaves the child widgets visible in later phases.
                self._clear(child_layout)
                child_layout.deleteLater()

    def _field(self, label, widget):
        box = QVBoxLayout(); box.setSpacing(5); box.addWidget(QLabel(label)); box.addWidget(widget); self.record_layout.addLayout(box)

    def render_phase(self):
        # Radio-button teardown can emit after its associated button is gone.
        self.start_button = None
        self._clear(self.record_layout)
        if self.phase == "idle": self._idle()
        elif self.phase == "recording": self._recording()
        elif self.phase == "processing": self._processing()
        else: self._done()
        self._set_tray_status()

    def _idle(self):
        self.mic1 = QComboBox(); self.mic2 = QComboBox(); self.system = QComboBox(); self._populate_devices()
        self._field("Microphone", self.mic1)
        self.add_second = QPushButton("+ add a second mic"); self.add_second.setObjectName("ghost"); self.add_second.clicked.connect(self._show_second); self.record_layout.addWidget(self.add_second)
        self._field("System audio source", self.system)
        mode_box = QWidget(); modes = QHBoxLayout(mode_box); modes.setContentsMargins(0,0,0,0); modes.setSpacing(0); self.mode_group = QButtonGroup(self)
        for key, label in MODES.items():
            radio = QRadioButton(label); radio.setProperty("mode", key); self.mode_group.addButton(radio); modes.addWidget(radio); radio.toggled.connect(self._mode_changed)
            if key == "meeting": radio.setChecked(True)
        self._field("Mode", mode_box)
        self.name = QLineEdit(); self.name.setPlaceholderText("e.g. Weekly sync"); self._field("Session name (optional)", self.name)
        self.start_button = QPushButton("▶ Start Recording"); self.start_button.setObjectName("primary"); self.start_button.clicked.connect(self.start); self.record_layout.addWidget(self.start_button)
        self._mode_changed()

    def _show_second(self):
        self.add_second.hide(); self._field("Second microphone", self.mic2)
        remove = QPushButton("× Remove second mic"); remove.setObjectName("ghost"); remove.clicked.connect(self._hide_second); self.record_layout.addWidget(remove); self.second_enabled = True

    def _hide_second(self):
        self.second_enabled = False
        self.mic2.setEnabled(False)
        self.add_second.show()

    def _mode_changed(self):
        mode = self.mode_group.checkedButton().property("mode")
        mic_enabled, system_enabled = mode not in ("lecture", "cbt"), mode != "journal"
        self.mic1.setEnabled(mic_enabled); self.add_second.setEnabled(mic_enabled and mode == "meeting"); self.system.setEnabled(system_enabled)
        required = (not mic_enabled or self.mic1.currentData()) and (not system_enabled or self.system.currentData())
        button = getattr(self, "start_button", None)
        if button is not None and isValid(button):
            button.setEnabled(bool(required))

    def _recording(self):
        display = QLabel(f"●  {self._format_time(self.elapsed_seconds)}"); display.setObjectName("timer"); display.setAlignment(Qt.AlignmentFlag.AlignCenter); self.record_layout.addWidget(display)
        tags = QLabel(f"{MODES.get(self.active.mode, self.active.mode)}   ·   {self._mic_summary()}"); tags.setAlignment(Qt.AlignmentFlag.AlignCenter); self.record_layout.addWidget(tags)
        stop = QPushButton("■ Stop Recording"); stop.setObjectName("primary"); stop.clicked.connect(lambda: self.process(False)); self.record_layout.addWidget(stop)

    def _processing(self):
        self.stage = QLabel("Transcribing…"); self.record_layout.addWidget(self.stage)
        self.progress = QFrame(); self.progress.setObjectName("progress"); self.record_layout.addWidget(self.progress)
        self.helper = QLabel("This can take a minute — Whisper runs locally, the summary goes out to the configured LLM."); self.helper.setWordWrap(True); self.helper.setObjectName("muted"); self.record_layout.addWidget(self.helper)

    def _done(self):
        successful = self.result_status == "done"
        tag = QLabel("Done" if successful else "Problem")
        tag.setObjectName("status-good" if successful else "status-problem")
        tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.record_layout.addWidget(tag)
        self.record_layout.addWidget(QLabel(f"{self.active.name} — {MODES.get(self.active.mode)} — {self._format_time(self.elapsed_seconds)}"))
        if not successful:
            detail = QLabel("Processing did not complete. You can inspect the output folder or retry it from History.")
            detail.setObjectName("muted"); detail.setWordWrap(True); self.record_layout.addWidget(detail)
        open_folder = QPushButton("Open Output Folder ↗"); open_folder.clicked.connect(lambda: self.open_folder(self.active.directory)); self.record_layout.addWidget(open_folder)
        another = QPushButton("Start Another"); another.setObjectName("primary"); another.clicked.connect(self.reset); self.record_layout.addWidget(another)

    def refresh_devices(self):
        try: self.microphones, self.systems = gui_api.devices()
        except Exception: self.microphones, self.systems = [], []

    def _populate_devices(self):
        for combo, devices, fallback in ((self.mic1, self.microphones, "No microphone found"), (self.mic2, self.microphones, "No microphone found"), (self.system, self.systems, "No system-audio monitor found")):
            if devices:
                for device in devices:
                    # The visible value and item data are the exact source names
                    # accepted by `meeting-recorder start --mic/--system-source`.
                    combo.addItem(device.display, device.id)
                default_index = next((i for i, device in enumerate(devices) if device.is_default), 0)
                combo.setCurrentIndex(default_index)
            else: combo.addItem(fallback, None)

    def start(self):
        mode = self.mode_group.checkedButton().property("mode")
        mics = [] if mode in ("lecture", "cbt") else [self.mic1.currentData()]
        if getattr(self, "second_enabled", False) and mode == "meeting": mics.append(self.mic2.currentData())
        source = None if mode == "journal" else self.system.currentData()
        self._run(gui_api.start, mics=[m for m in mics if m], system_source=source, mode=mode, name=self.name.text().strip(), complete=self.started)

    @Slot(object)
    def started(self, session):
        self.active = session; self.elapsed_seconds = max(0, int((datetime.now(timezone.utc) - session.started_at).total_seconds())); self.phase = "recording"; self.timer.start(1000); self.pulse_timer.start(700); self.render_phase()

    def process(self, retry):
        self.timer.stop(); self.phase = "processing"; self.render_phase(); self.pulse_timer.start(700)
        self._run(gui_api.stop_or_retry, retry, complete=self._processing_done, processing=True)

    @Slot(str, int)
    def _apply_progress(self, stage, percent):
        if self.phase != "processing": return
        self.stage.setText("Transcribing…" if stage == "transcribing" else "Summarizing…")
        self.progress.setStyleSheet(f"QFrame#progress {{ border: 1px solid {BORDER}; background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT}, stop:{percent/100:.2f} {ACCENT}, stop:{percent/100:.2f} {SURFACE}, stop:1 {SURFACE}); min-height: 10px; }}")

    @Slot(object)
    def _processing_done(self, _result):
        self.result_status = "done"; self.phase = "done"; self.pulse_timer.stop(); self.render_phase()

    @Slot(str)
    def failed(self, message):
        self.pulse_timer.stop()
        if self.active is not None:
            self.result_status = "problem"; self.phase = "done"; self.render_phase()
        else:
            self.phase = "idle"; self.render_phase()
    def reset(self): self.active = None; self.elapsed_seconds = 0; self.result_status = "done"; self.phase = "idle"; self.render_phase()
    def _tick(self): self.elapsed_seconds += 1; self.render_phase()
    def _pulse(self): self.pulse_on = not self.pulse_on; self._set_tray_status()
    def _format_time(self, seconds): return f"{seconds // 60:02d}:{seconds % 60:02d}"
    def _mic_summary(self): return ", ".join(self.active.mics) if self.active and self.active.mics else "System audio"

    def recover(self):
        try: session = gui_api.active_session()
        except Exception: return
        if session and not session.error:
            self.active = session
            if session.phase == "recording": self.started(session)
            else: self.phase = "processing"; self.render_phase(); self.process(True)

    def show_history(self):
        self.record_tab.setChecked(False); self.history_tab.setChecked(True); self.stack.setCurrentIndex(1); self.history_list.clear()
        base = Path(os.environ.get("MEETING_RECORDER_DATA_DIR", Path.home() / ".local/share/meeting-recorder/sessions"))
        active = gui_api.active_session()
        rows = sorted((p for p in base.glob("*") if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        if active and active.error: rows = [active.directory, *[p for p in rows if p != active.directory]][:5]
        for directory in rows:
            metadata_path = directory / ".meeting-recorder-panel.json"
            try: metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError): metadata = {"name": directory.name, "mode": "meeting", "status": "done"}
            failed = metadata.get("status") == "failed" or bool(active and directory == active.directory and active.error)
            item = QListWidgetItem(f"{metadata.get('name', directory.name)} — {MODES.get(metadata.get('mode'), metadata.get('mode', 'meeting'))}\n{('Retry · Failed' if failed else 'Done')}")
            self.history_list.addItem(item)
        self.history_list.itemDoubleClicked.connect(lambda _: self.process(True) if active and active.error else None)

    def open_folder(self, directory):
        """Open this session's artifact directory with Cinnamon's file manager."""
        subprocess.Popen(["xdg-open", str(directory.resolve())], start_new_session=True)
    def show_under_tray(self, tray):
        rect = tray.geometry()
        anchor = rect.bottomLeft() if rect.isValid() else QCursor.pos()
        screen = QApplication.screenAt(anchor) or QApplication.primaryScreen()
        available = screen.availableGeometry()
        x = max(available.left(), min(anchor.x(), available.right() - self.width() + 1))
        y = anchor.y() if anchor.y() + self.sizeHint().height() <= available.bottom() else anchor.y() - self.sizeHint().height()
        self.move(QPoint(x, max(available.top(), y)))
        self.show(); self.raise_(); self.activateWindow()

    def focusOutEvent(self, event):
        if not self.underMouse(): self.hide()
        super().focusOutEvent(event)
    def _set_tray_status(self):
        if hasattr(self, "tray"):
            label = {"idle":"Ready", "recording":"Recording", "processing":"Processing", "done":"Done"}[self.phase]
            self.tray.setIcon(tray_icon(self.phase, self.pulse_on)); self.tray.setToolTip(f"Meeting Recorder — {label}")

    def _run(self, action, *args, complete, processing=False, **kwargs):
        self.worker_thread = QThread(self); worker = Worker(action, *args, processing=processing, **kwargs); self.worker = worker; worker.moveToThread(self.worker_thread)
        worker.progress.connect(self._apply_progress, Qt.ConnectionType.QueuedConnection)
        worker.completed.connect(complete, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self.failed, Qt.ConnectionType.QueuedConnection)
        self.worker_thread.started.connect(worker.run); worker.completed.connect(self.worker_thread.quit); worker.failed.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(worker.deleteLater); self.worker_thread.finished.connect(lambda: setattr(self, "worker", None)); self.worker_thread.start()


def tray_icon(phase, bright=True):
    pixmap = QPixmap(22, 22); pixmap.fill(Qt.GlobalColor.transparent); painter = QPainter(pixmap)
    color = QColor("#b7b7ba" if phase == "idle" else ACCENT); color.setAlpha(255 if bright else 70)
    painter.setBrush(color); painter.setPen(Qt.PenStyle.NoPen); painter.drawEllipse(5, 5, 12, 12); painter.end(); return QIcon(pixmap)


def main():
    app = QApplication(sys.argv); app.setApplicationName("Meeting Recorder")
    app.setStyleSheet(f"""QWidget#panel {{ background:{BG}; color:{TEXT}; border:1px solid {BORDER}; }} QLabel {{ font-family: Barlow, sans-serif; }} QComboBox,QLineEdit,QPushButton {{ min-height:34px; padding:4px 9px; background:{SURFACE}; border:1px solid {BORDER}; border-radius:4px; }} QPushButton#primary {{ background:{ACCENT}; color:{BG}; border-color:{ACCENT}; font-weight:bold; }} QPushButton#ghost {{ color:{ACCENT}; border:0; background:transparent; text-align:left; }} QRadioButton {{ padding:7px 3px; background:{SURFACE}; border:1px solid {BORDER}; }} QRadioButton::indicator {{ width:0; }} QRadioButton:checked {{ background:{ACCENT}; color:{BG}; }} QLabel#timer {{ font-size:28px; font-weight:bold; color:{DARK}; padding:16px; background:{TINT}; border:1px solid {ACCENT}; }} QLabel#status-good,QLabel#status-problem {{ min-height:32px; font-weight:bold; padding:5px 10px; }} QLabel#status-good {{ background:{SUCCESS}; color:white; }} QLabel#status-problem {{ background:{PROBLEM}; color:white; }} QLabel#muted {{ color:#6f7072; font-size:12px; }} QListWidget {{ border:0; background:transparent; }}""")
    panel = Panel(); tray = QSystemTrayIcon(tray_icon("idle"), app); panel.tray = tray
    def toggle(reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            panel.hide() if panel.isVisible() else panel.show_under_tray(tray)
    tray.activated.connect(toggle)
    menu = QMenu(); menu.addAction("Open", lambda: panel.show_under_tray(tray)); menu.addAction("Exit", app.quit); tray.setContextMenu(menu); tray.show()
    return app.exec()


if __name__ == "__main__": sys.exit(main())
