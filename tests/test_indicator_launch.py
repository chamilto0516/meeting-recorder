from __future__ import annotations

import unittest
from argparse import Namespace
from unittest.mock import patch

from meeting_recorder import cli

INDICATOR_PATH = "/usr/bin/meeting-recorder-indicator"


def _start_args(**updates) -> Namespace:
    values = dict(no_indicator=False)
    values.update(updates)
    return Namespace(**values)


class LaunchIndicatorTests(unittest.TestCase):
    """`_launch_indicator` is best-effort and must never fail a recording."""

    @patch("meeting_recorder.cli.subprocess.Popen")
    @patch("meeting_recorder.cli.shutil.which", return_value=INDICATOR_PATH)
    def test_skipped_when_no_indicator_flag_set(self, _which, popen):
        with patch.dict("os.environ", {"DISPLAY": ":0"}, clear=False):
            cli._launch_indicator(_start_args(no_indicator=True))
        popen.assert_not_called()

    @patch("meeting_recorder.cli.subprocess.Popen")
    @patch("meeting_recorder.cli.shutil.which", return_value=INDICATOR_PATH)
    def test_skipped_when_env_var_set(self, _which, popen):
        with patch.dict("os.environ", {"DISPLAY": ":0", "MEETING_RECORDER_NO_INDICATOR": "1"}, clear=False):
            cli._launch_indicator(_start_args())
        popen.assert_not_called()

    @patch("meeting_recorder.cli.subprocess.Popen")
    @patch("meeting_recorder.cli.shutil.which", return_value=INDICATOR_PATH)
    def test_skipped_without_a_display(self, _which, popen):
        with patch.dict("os.environ", {}, clear=True):
            cli._launch_indicator(_start_args())
        popen.assert_not_called()

    @patch("meeting_recorder.cli.subprocess.Popen")
    @patch("meeting_recorder.cli.shutil.which", return_value=None)
    def test_skipped_when_indicator_is_not_installed(self, _which, popen):
        with patch.dict("os.environ", {"DISPLAY": ":0"}, clear=False):
            cli._launch_indicator(_start_args())
        popen.assert_not_called()

    @patch("meeting_recorder.cli.subprocess.Popen")
    @patch("meeting_recorder.cli.shutil.which", return_value=INDICATOR_PATH)
    def test_spawns_detached_indicator_on_wayland(self, _which, popen):
        with patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}, clear=True):
            cli._launch_indicator(_start_args())
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [INDICATOR_PATH, "--exit-when-idle"])
        self.assertTrue(kwargs["start_new_session"])

    @patch("meeting_recorder.cli.subprocess.Popen", side_effect=OSError("boom"))
    @patch("meeting_recorder.cli.shutil.which", return_value=INDICATOR_PATH)
    def test_never_raises_even_if_spawn_fails(self, _which, _popen):
        with patch.dict("os.environ", {"DISPLAY": ":0"}, clear=False):
            cli._launch_indicator(_start_args())  # must not raise
