from __future__ import annotations

import io
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from meeting_recorder import cli, config, device_selection
from meeting_recorder.audio import SourceInfo
from meeting_recorder.errors import (
    AmbiguousDeviceSelectionError,
    ConfigError,
    DeviceNotFoundError,
    DeviceSelectionError,
)


def candidate(kind: str, token: str, source: str, label: str, *, aliases=()):
    return device_selection.DeviceCandidate(
        kind=kind, token=token, source=source, index=token[1:], label=label,
        is_default=token in {"m1", "o1"}, aliases=aliases,
    )


class DeviceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [
            candidate("mic", "m1", "alsa_input.usb-Desk_Mic-00", "Desk Mic", aliases=("desk-mic",)),
            candidate("mic", "m2", "alsa_input.usb-Headset-00", "USB Headset"),
            candidate("output", "o1", "alsa_output.usb-Headset-00.monitor", "USB Headset"),
        ]
        self.aliases = {"desk-mic": "alsa_input.usb-Desk_Mic-00", "headset-output": "alsa_output.usb-Headset-00.monitor"}

    @patch("meeting_recorder.device_selection.audio.get_default_sink_monitor", return_value="alsa_output.z.monitor")
    @patch("meeting_recorder.device_selection.audio.get_default_source", return_value="alsa_input.z")
    @patch("meeting_recorder.device_selection.audio.list_sources")
    def test_discovery_assigns_default_first_deterministic_tokens(self, list_sources, *_):
        list_sources.return_value = [
            SourceInfo("4", "alsa_input.a", "IDLE"),
            SourceInfo("2", "alsa_output.z.monitor", "RUNNING"),
            SourceInfo("1", "alsa_input.z", "RUNNING"),
            SourceInfo("3", "alsa_output.a.monitor", "IDLE"),
        ]
        candidates = device_selection.discover({"desk": "alsa_input.z"})
        self.assertEqual([(item.kind, item.token, item.source) for item in candidates], [
            ("mic", "m1", "alsa_input.z"), ("mic", "m2", "alsa_input.a"),
            ("output", "o1", "alsa_output.z.monitor"), ("output", "o2", "alsa_output.a.monitor"),
        ])
        self.assertEqual(candidates[0].aliases, ("desk",))

    def test_resolves_exact_token_alias_and_unique_fuzzy_match(self):
        self.assertEqual(device_selection.resolve_local("alsa_input.usb-Desk_Mic-00", "mic", self.candidates, self.aliases).token, "m1")
        self.assertEqual(device_selection.resolve_local("m2", "mic", self.candidates, self.aliases).source, self.candidates[1].source)
        self.assertEqual(device_selection.resolve_local("desk-mic", "mic", self.candidates, self.aliases).token, "m1")
        self.assertEqual(device_selection.resolve_local("hedset", "mic", self.candidates, self.aliases).token, "m2")

    def test_rejects_ambiguous_and_wrong_type_matches(self):
        candidates = [*self.candidates, candidate("mic", "m3", "alsa_input.usb-Other_Headset-00", "Other Headset")]
        with self.assertRaises(AmbiguousDeviceSelectionError):
            device_selection.resolve_local("headset", "mic", candidates, self.aliases)
        with self.assertRaisesRegex(DeviceSelectionError, "output source"):
            device_selection.resolve_local("headset-output", "mic", self.candidates, self.aliases)
        with self.assertRaisesRegex(DeviceSelectionError, "unavailable"):
            device_selection.resolve_local("desk-mic", "mic", self.candidates, {"desk-mic": "gone"})

    def test_unmatched_selector_never_calls_llm_without_explicit_opt_in(self):
        fake_llm = SimpleNamespace(completion=lambda **_: self.fail("LLM should not be called"))
        with patch.dict(sys.modules, {"litellm": fake_llm}):
            with self.assertRaises(DeviceNotFoundError):
                device_selection.resolve(
                    "a natural language hint", "mic", self.candidates, self.aliases,
                    allow_llm=False, llm=config.LLMConfig(),
                )

    def test_llm_result_is_limited_to_live_candidates(self):
        called = []
        fake_llm = SimpleNamespace(completion=lambda **_: (called.append(True) or {"choices": [{"message": {"content": '{"selector": "m2"}'}}]}))
        with patch.dict(sys.modules, {"litellm": fake_llm}):
            resolved = device_selection.resolve(
                "the thing on my head", "mic", self.candidates, self.aliases,
                allow_llm=True, llm=config.LLMConfig(),
            )
        self.assertEqual(resolved.token, "m2")
        self.assertEqual(called, [True])

    def test_llm_device_selection_forwards_configured_reasoning_effort(self):
        completion = Mock(
            return_value={"choices": [{"message": {"content": '{"selector": "m2"}'}}]}
        )
        fake_llm = SimpleNamespace(completion=completion)
        with patch.dict(sys.modules, {"litellm": fake_llm}):
            device_selection.resolve(
                "the thing on my head", "mic", self.candidates, self.aliases,
                allow_llm=True,
                llm=config.LLMConfig(reasoning_effort="high"),
            )
        self.assertEqual(completion.call_args.kwargs["reasoning_effort"], "high")

    def test_llm_invalid_candidate_is_rejected(self):
        fake_llm = SimpleNamespace(completion=lambda **_: {"choices": [{"message": {"content": '{"selector": "o1"}'}}]})
        with patch.dict(sys.modules, {"litellm": fake_llm}):
            with self.assertRaisesRegex(DeviceSelectionError, "invalid selector"):
                device_selection.resolve(
                    "the thing on my head", "mic", self.candidates, self.aliases,
                    allow_llm=True, llm=config.LLMConfig(),
                )


class DeviceConfigAndCliTests(unittest.TestCase):
    def test_alias_config_validates_schema_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "devices.yaml"
            path.write_text("version: 1\naliases:\n  desk: mic.a\n  desk: mic.b\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                config.load_device_aliases(path)
            path.write_text("version: 1\naliases:\n  m1: mic.a\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "reserved"):
                config.load_device_aliases(path)

    def test_devices_init_is_protected_and_writes_editable_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "devices.yaml"
            cfg = config.AppConfig(Path(temporary), 48000, device_config=path)
            args = Namespace(force=False)
            candidates = [candidate("mic", "m1", "mic.a", "Desk Mic"), candidate("output", "o1", "out.a.monitor", "Speakers")]
            with patch("meeting_recorder.cli.device_selection.discover", return_value=candidates):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.cmd_devices_init(args, cfg), 0)
            self.assertIn("mic-1: mic.a", path.read_text(encoding="utf-8"))
            self.assertIn("Created device alias config", output.getvalue())
            with self.assertLogs("meeting_recorder", level="ERROR"):
                self.assertEqual(cli.cmd_devices_init(args, cfg), 1)

    def test_list_devices_shows_short_selectors_and_aliases(self):
        candidates = [candidate("mic", "m1", "mic.a", "Desk Mic", aliases=("desk",)), candidate("output", "o1", "out.a.monitor", "Speakers")]
        cfg = config.AppConfig(Path("/tmp"), 48000, device_aliases={"desk": "mic.a"})
        with patch("meeting_recorder.cli.device_selection.discover", return_value=candidates):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.cmd_list_devices(Namespace(), cfg), 0)
        self.assertIn("[m1] Desk Mic", output.getvalue())
        self.assertIn("aliases: desk", output.getvalue())
        self.assertIn("--system-source o1", output.getvalue())

    def test_list_devices_reports_disconnected_saved_aliases(self):
        candidates = [candidate("mic", "m1", "mic.a", "Desk Mic")]
        cfg = config.AppConfig(Path("/tmp"), 48000, device_aliases={"away": "mic.disconnected"})
        with patch("meeting_recorder.cli.device_selection.discover", return_value=candidates):
            output = io.StringIO()
            with redirect_stdout(output):
                cli.cmd_list_devices(Namespace(), cfg)
        self.assertIn("Saved aliases not currently available", output.getvalue())
        self.assertIn("away: mic.disconnected", output.getvalue())

    def test_start_resolves_short_selectors_before_invoking_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = config.AppConfig(root, 48000)
            args = cli.build_parser().parse_args(["start", "--mic", "m1", "--system-source", "o1"])
            candidates = [candidate("mic", "m1", "mic.a", "Desk Mic"), candidate("output", "o1", "out.a.monitor", "Speakers")]
            handle = SimpleNamespace(
                pid=42, process=SimpleNamespace(), mixed_file=root / "mixed.wav",
                track_files={"mic0": root / "mic0.wav"}, log_file=root / "ffmpeg.log",
            )
            with (
                patch("meeting_recorder.cli.state.session_lock", return_value=nullcontext()),
                patch("meeting_recorder.cli.state.load_session", return_value=None),
                patch("meeting_recorder.cli.audio.check_dependencies"),
                patch("meeting_recorder.cli.device_selection.discover", return_value=candidates),
                patch("meeting_recorder.cli.audio.start_recording", return_value=handle) as start_recording,
                patch("meeting_recorder.cli.state.save_session"),
            ):
                self.assertEqual(cli.cmd_start(args, cfg), 0)
            start_recording.assert_called_once()
            self.assertEqual(start_recording.call_args.args[0], ["mic.a"])
            self.assertEqual(start_recording.call_args.args[1], "out.a.monitor")
            self.assertEqual(start_recording.call_args.args[2].parent, root)
            self.assertEqual(start_recording.call_args.args[3], 48000)
