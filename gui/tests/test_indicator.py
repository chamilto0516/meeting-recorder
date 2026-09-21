from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from meeting_recorder.gui_api import ActiveSession
    from meeting_recorder_panel import tray as tray_mod
except ImportError:  # PySide6 is an optional dependency for console-only installs.
    QApplication = None


def _active_session(phase: str) -> "ActiveSession":
    return ActiveSession(
        phase=phase, session_id="s", name="Weekly sync", mode="meeting",
        mics=["mic"], system_source="system.monitor", started_at=datetime.now(timezone.utc),
        directory=Path("/tmp/session"),
    )


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class TrayControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @patch("meeting_recorder_panel.tray.gui_api.active_session", return_value=None)
    def test_stop_action_only_visible_while_recording(self, active_session):
        controller = tray_mod.TrayController(self.app)
        self.assertFalse(controller._stop_action.isVisible())

        active_session.return_value = _active_session("recording")
        controller.poll()
        self.assertEqual(controller.phase, "recording")
        self.assertTrue(controller._stop_action.isVisible())

        active_session.return_value = _active_session("processing")
        controller.poll()
        self.assertEqual(controller.phase, "processing")
        self.assertFalse(controller._stop_action.isVisible())

    @patch("meeting_recorder_panel.tray.gui_api.active_session")
    def test_hide_when_idle_hides_and_shows_the_icon(self, active_session):
        active_session.return_value = None
        controller = tray_mod.TrayController(self.app, hide_when_idle=True)
        self.assertFalse(controller.tray.isVisible())

        active_session.return_value = _active_session("recording")
        controller.poll()
        self.assertTrue(controller.tray.isVisible())

        active_session.return_value = None
        controller.poll()
        self.assertFalse(controller.tray.isVisible())

    @patch("meeting_recorder_panel.tray.gui_api.active_session", side_effect=RuntimeError("bad state file"))
    def test_a_raising_poll_leaves_the_controller_alive(self, _active_session):
        controller = tray_mod.TrayController(self.app)
        controller.poll()  # must not raise
        self.assertEqual(controller.phase, "idle")

    def test_stopping_disables_the_action_immediately(self):
        controller = tray_mod.TrayController(self.app)
        controller._apply_phase("recording")
        self.assertTrue(controller._stop_action.isEnabled())
        controller.stopping()
        self.assertFalse(controller._stop_action.isEnabled())


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class TrayIconDrawingTests(unittest.TestCase):
    def _center_color(self, phase: str, tick: int):
        icon = tray_mod.draw_icon(phase, tick)
        image = icon.pixmap(22, 22).toImage()
        return image.pixelColor(11, 11)

    def test_recording_pulse_sweeps_from_gray_to_red_and_back(self):
        cycle_steps = tray_mod.PULSE_CYCLE_MS // tray_mod.PULSE_INTERVAL_MS
        start_color = self._center_color("recording", 0)
        peak_color = self._center_color("recording", cycle_steps // 2)
        end_color = self._center_color("recording", cycle_steps)
        self.assertEqual((start_color.red(), start_color.green(), start_color.blue()), (0xB7, 0xB7, 0xBA))
        self.assertEqual((peak_color.red(), peak_color.green(), peak_color.blue()), (0xC2, 0x35, 0x2F))
        self.assertEqual((end_color.red(), end_color.green(), end_color.blue()), (0xB7, 0xB7, 0xBA))

    def test_idle_icon_is_static_gray(self):
        first = self._center_color("idle", 0)
        second = self._center_color("idle", 7)
        self.assertEqual((first.red(), first.green(), first.blue()), (second.red(), second.green(), second.blue()))


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class TrayLockTests(unittest.TestCase):
    def test_second_lock_attempt_returns_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("meeting_recorder_panel.tray.state.state_dir", return_value=Path(temporary)):
                first = tray_mod.tray_lock()
                try:
                    self.assertIsNotNone(first)
                    second = tray_mod.tray_lock()
                    self.assertIsNone(second)
                finally:
                    tray_mod.release_tray_lock(first)
