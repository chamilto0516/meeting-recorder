from __future__ import annotations

import os
import json
import tempfile
import unittest
import wave
from contextlib import nullcontext
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from meeting_recorder import audio, cli, config, state
from meeting_recorder.errors import SummarizationError


def make_session(root: Path) -> state.Session:
    identity = state.ProcessIdentity(123, "/usr/bin/ffmpeg", "abc", 456)
    return state.Session(
        session_id="session",
        pid=456,
        process=identity,
        started_at="2026-01-01T00:00:00+00:00",
        session_dir=str(root),
        mixed_file=str(root / "mixed.wav"),
        track_files={"mixed": str(root / "mixed.wav")},
        mics=["mic"],
        system_source="system.monitor",
        sample_rate=48000,
        whisper=config.WhisperConfig().to_dict(),
        llm=config.LLMConfig(model="remote/model", endpoint="https://example.test").to_session_dict(),
    )


def write_wav(path: Path, sample_rate: int = 48000, frames: int = 32) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\0\0" * frames)


class StateSafetyTests(unittest.TestCase):
    def test_session_name_is_human_readable_and_path_safe(self) -> None:
        self.assertEqual(
            state.sanitize_session_name("First: AI Class(Lecture"),
            "First-AI-Class-Lecture",
        )
        self.assertEqual(state.sanitize_session_name("../../  résumé  "), "resume")
        with self.assertRaisesRegex(Exception, "letter or number"):
            state.sanitize_session_name("///")

    def test_state_is_private_and_does_not_contain_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(state, "state_dir", return_value=root):
                state.save_session(make_session(root))
                contents = state.state_file().read_text(encoding="utf-8")
                self.assertNotIn("api_key", contents)
                self.assertEqual(os.stat(state.state_file()).st_mode & 0o777, 0o600)
                self.assertEqual(state.load_session().process.start_time_ticks, 123)

    def test_refuses_to_serialize_a_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = make_session(Path(temporary))
            session.llm["api_key"] = "secret"
            with self.assertRaisesRegex(Exception, "API key"):
                session.to_dict()

    def test_legacy_state_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "current_session.json"
            legacy.write_text('{"pid": 1}', encoding="utf-8")
            os.chmod(legacy, 0o600)
            with patch.object(state, "state_dir", return_value=root):
                with self.assertRaisesRegex(Exception, "unsupported version"):
                    state.load_session()

    def test_previous_secure_state_version_migrates_as_recording(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = make_session(root).to_dict()
            data["state_version"] = 2
            for field in ("status", "ended_at", "transcript_file", "summary_file", "last_error"):
                data.pop(field)
            previous = root / "current_session.json"
            previous.write_text(json.dumps(data), encoding="utf-8")
            os.chmod(previous, 0o600)
            with patch.object(state, "state_dir", return_value=root):
                self.assertEqual(state.load_session().status, "recording")

    def test_state_with_non_private_permissions_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(state, "state_dir", return_value=root):
                state.save_session(make_session(root))
                os.chmod(state.state_file(), 0o644)
                with self.assertRaisesRegex(Exception, "private permissions"):
                    state.load_session()

    def test_process_identity_requires_all_facts_and_group_leader(self) -> None:
        expected = state.ProcessIdentity(1, "/usr/bin/ffmpeg", "hash", 42)
        with patch.object(audio, "get_process_identity", return_value=expected):
            self.assertTrue(audio.verify_recording_process(42, expected))
        wrong_group = replace(expected, process_group=99)
        with patch.object(audio, "get_process_identity", return_value=wrong_group):
            self.assertFalse(audio.verify_recording_process(42, expected))
        reused_pid = replace(expected, start_time_ticks=2)
        with patch.object(audio, "get_process_identity", return_value=reused_pid):
            self.assertFalse(audio.verify_recording_process(42, expected))

    def test_stop_resolves_api_key_from_current_config_not_session_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = make_session(root)
            write_wav(Path(session.mixed_file))
            (root / "ffmpeg.log").write_text("", encoding="utf-8")
            transcript_path = root / "transcript.txt"
            transcript_path.write_text("transcript", encoding="utf-8")
            args = Namespace(
                skip_transcription=False,
                skip_summary=False,
                whisper_model=None,
                whisper_device=None,
                whisper_compute_type=None,
                language=None,
                llm_model=None,
                llm_endpoint=None,
                llm_api_key=None,
            )
            current = config.AppConfig(
                data_dir=root,
                sample_rate=48000,
                llm=config.LLMConfig(api_key="fresh-secret"),
            )
            with (
                patch.object(state, "session_lock", return_value=nullcontext()),
                patch.object(state, "load_session", return_value=session),
                patch.object(state, "save_session"),
                patch.object(state, "clear_session"),
                patch.object(audio, "verify_recording_process", return_value=True),
                patch.object(audio, "stop_recording", return_value=True),
                patch("meeting_recorder.transcribe.transcribe_audio", return_value="transcript"),
                patch("meeting_recorder.transcribe.save_transcript", return_value=transcript_path),
                patch("meeting_recorder.summarize.save_summary"),
                patch("meeting_recorder.summarize.summarize_transcript", return_value="summary") as summarize,
            ):
                self.assertEqual(cli.cmd_stop(args, current), 0)
            self.assertEqual(summarize.call_args.args[1].api_key, "fresh-secret")
            self.assertNotIn("api_key", session.llm)

    def test_summary_failure_keeps_a_retryable_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = make_session(root)
            session.status = "transcribed"
            write_wav(Path(session.mixed_file))
            (root / "ffmpeg.log").write_text("", encoding="utf-8")
            transcript = root / "transcript.txt"
            transcript.write_text("transcript", encoding="utf-8")
            session.transcript_file = str(transcript)
            args = Namespace(
                skip_summary=False,
                whisper_model=None,
                whisper_device=None,
                whisper_compute_type=None,
                language=None,
                llm_model=None,
                llm_endpoint=None,
                llm_api_key=None,
            )
            current = config.AppConfig(data_dir=root, sample_rate=48000)
            with (
                patch.object(state, "session_lock", return_value=nullcontext()),
                patch.object(state, "load_session", return_value=session),
                patch.object(state, "save_session") as save_session,
                patch.object(state, "clear_session") as clear_session,
                patch(
                    "meeting_recorder.summarize.summarize_transcript",
                    side_effect=SummarizationError("LLM unavailable"),
                ),
            ):
                with self.assertRaisesRegex(SummarizationError, "LLM unavailable"):
                    cli._process_session(session, args, current)
            self.assertEqual(session.status, "transcribed")
            self.assertEqual(session.last_error, "LLM unavailable")
            self.assertGreaterEqual(save_session.call_count, 2)
            self.assertEqual(save_session.call_args.args[0], session)
            clear_session.assert_not_called()
