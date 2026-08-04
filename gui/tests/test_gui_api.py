from __future__ import annotations

import unittest
from unittest.mock import patch

from meeting_recorder import gui_api
from meeting_recorder.audio import SourceInfo


class GuiApiTests(unittest.TestCase):
    @patch("meeting_recorder.gui_api.audio.get_default_sink_monitor", return_value="system.monitor")
    @patch("meeting_recorder.gui_api.audio.get_default_source", return_value="mic.default")
    @patch("meeting_recorder.gui_api.audio.list_sources")
    def test_devices_split_microphones_and_monitors(self, sources, *_):
        sources.return_value = [
            SourceInfo("1", "mic.default", "RUNNING"),
            SourceInfo("2", "system.monitor", "IDLE"),
        ]
        microphones, systems = gui_api.devices()
        self.assertEqual([(d.id, d.is_default) for d in microphones], [("mic.default", True)])
        self.assertEqual([(d.id, d.is_default) for d in systems], [("system.monitor", True)])
