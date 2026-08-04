from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from meeting_recorder.gui_api import ActiveSession
    from meeting_recorder_panel.app import Panel
except ImportError:  # PySide6 is an optional dependency for console-only installs.
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class PanelLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @patch("meeting_recorder_panel.app.gui_api.devices", return_value=([], []))
    def test_done_phase_replaces_recording_controls(self, _devices):
        panel = Panel()
        panel.active = ActiveSession(
            phase="recording", session_id="test", name="Weekly sync", mode="meeting",
            mics=["mic"], system_source="system.monitor", started_at=datetime.now(timezone.utc),
            directory=Path("/tmp/session"),
        )
        panel.phase = "recording"
        panel.render_phase()
        panel.phase = "done"
        panel.render_phase()
        text = "\n".join(widget.text() for widget in panel.findChildren(type(panel.record_tab)) if hasattr(widget, "text"))
        self.assertIn("Open Output Folder", text)
        self.assertNotIn("Stop Recording", text)
        panel.deleteLater()
