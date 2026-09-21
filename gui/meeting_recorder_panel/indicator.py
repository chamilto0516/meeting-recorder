"""Standalone tray-only process for the recording status indicator.

`meeting-recorder start` spawns this detached (see `cli._launch_indicator`)
so a recording started from the terminal still shows a status-bar icon. It
can also be installed as an autostart entry to stay resident across login.
Either way, `tray.tray_lock()` ensures at most one process — this one, an
autostart copy, or the control panel — ever shows the icon.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication

from meeting_recorder import gui_api
from meeting_recorder_panel.app import Worker
from meeting_recorder_panel.tray import TrayController, release_tray_lock, tray_lock

logger = logging.getLogger(__name__)

IDLE_EXIT_DELAY_MS = 5000


class _StopRunner:
    """Runs one `stop_or_retry` call on a background thread, at most once."""

    def __init__(self, controller: TrayController):
        self.controller = controller
        self.thread = None
        self.worker = None

    def start(self) -> None:
        if self.thread is not None:
            return
        self.controller.stopping()
        self.thread = QThread()
        self.worker = Worker(gui_api.stop_or_retry, False, processing=True)
        self.worker.moveToThread(self.thread)
        self.worker.completed.connect(self._finished)
        self.worker.failed.connect(self._failed)
        self.thread.started.connect(self.worker.run)
        self.thread.start()

    def _finished(self, _result) -> None:
        self._teardown()

    def _failed(self, message: str) -> None:
        logger.warning("Stop from the tray did not finish cleanly: %s", message)
        self._teardown()

    def _teardown(self) -> None:
        self.thread.quit()
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread, self.worker = None, None


def _open_panel() -> None:
    executable = shutil.which("meeting-recorder-panel")
    if executable is None:
        logger.debug("meeting-recorder-panel is not on PATH; cannot open it from the tray.")
        return
    subprocess.Popen([executable], start_new_session=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="meeting-recorder-indicator")
    parser.add_argument(
        "--exit-when-idle",
        action="store_true",
        help="Quit shortly after the active session finishes, instead of staying resident.",
    )
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)

    lock_fd = tray_lock()
    if lock_fd is None:
        # Another indicator or the panel already owns the tray icon.
        return 0
    app.aboutToQuit.connect(lambda: release_tray_lock(lock_fd))

    controller = TrayController(app, hide_when_idle=True)
    runner = _StopRunner(controller)
    controller.stop_requested.connect(runner.start)
    controller.open_requested.connect(_open_panel)

    if args.exit_when_idle:
        idle_timer = QTimer()
        idle_timer.setSingleShot(True)
        idle_timer.timeout.connect(lambda: app.quit() if controller.phase == "idle" else None)
        controller.phase_changed.connect(lambda _phase: idle_timer.start(IDLE_EXIT_DELAY_MS))
        idle_timer.start(IDLE_EXIT_DELAY_MS)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
