from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from meeting_recorder import gui_api
from meeting_recorder.audio import SourceInfo


class GuiApiTests(unittest.TestCase):
    @patch("meeting_recorder.device_selection.audio.get_default_sink_monitor", return_value="system.monitor")
    @patch("meeting_recorder.device_selection.audio.get_default_source", return_value="mic.default")
    @patch("meeting_recorder.device_selection.audio.list_sources")
    def test_devices_split_microphones_and_monitors(self, sources, *_):
        sources.return_value = [
            SourceInfo("1", "mic.default", "RUNNING"),
            SourceInfo("2", "system.monitor", "IDLE"),
        ]
        microphones, systems = gui_api.devices()
        self.assertEqual([(d.id, d.is_default) for d in microphones], [("mic.default", True)])
        self.assertEqual([(d.id, d.is_default) for d in systems], [("system.monitor", True)])
        self.assertEqual(microphones[0].token, "m1")
        self.assertEqual(systems[0].token, "o1")

    @patch("meeting_recorder.gui_api.config.load_config")
    @patch("meeting_recorder.gui_api.cli.cmd_start", return_value=0)
    @patch("meeting_recorder.gui_api.active_session")
    def test_start_forwards_player_context(self, active_session, start, load_config):
        context = Path("/tmp/character.md")
        active_session.return_value = gui_api.ActiveSession(
            phase="recording", session_id="session", name="session", mode="game-player",
            mics=["mic"], system_source="system", started_at=datetime.now(timezone.utc),
            directory=Path("/tmp/session"),
        )
        load_config.return_value = object()
        with patch("meeting_recorder.gui_api._write_history"):
            gui_api.start(
                mics=["mic"], system_source="system", mode="game-player",
                name=None, player_context=context,
            )
        self.assertEqual(start.call_args.args[0].player_context, context)
