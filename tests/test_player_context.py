from __future__ import annotations

import stat
import tempfile
import unittest
from argparse import Namespace
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from meeting_recorder import cli, config, modes, session_archive, state
from meeting_recorder.errors import MeetingRecorderError


def _session(root: Path, context: bytes) -> state.Session:
    context_path = root / "player-context.md"
    cli._write_private_context(context_path, context)
    digest = cli.hashlib.sha256(context).hexdigest()
    return state.Session(
        session_id=root.name,
        pid=0,
        process=None,
        started_at="2026-08-27T12:00:00+00:00",
        ended_at="2026-08-27T15:00:00+00:00",
        session_dir=str(root),
        mixed_file=str(root / "mixed.wav"),
        track_files={},
        mics=["mic"],
        system_source="system.monitor",
        sample_rate=48000,
        mode="game-player",
        whisper=config.WhisperConfig().to_dict(),
        llm=config.LLMConfig().to_session_dict(),
        status="transcribed",
        transcript_file=str(root / "transcript.txt"),
        player_context_file="player-context.md",
        player_context_sha256=digest,
    )


def _reprocess_args(**updates) -> Namespace:
    values = dict(
        session="session", mode=None, retranscribe=False, use_saved_prompt=False,
        player_context=None, whisper_model=None, whisper_device=None,
        whisper_compute_type=None, language=None, llm_model=None,
        llm_endpoint=None, llm_api_key=None,
    )
    values.update(updates)
    return Namespace(**values)


class PlayerContextTests(unittest.TestCase):
    def test_context_reader_rejects_non_markdown_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text = root / "character.txt"
            text.write_text("notes", encoding="utf-8")
            with self.assertRaisesRegex(MeetingRecorderError, "Markdown"):
                cli._read_player_context(text)
            markdown = root / "character.md"
            markdown.write_text("notes", encoding="utf-8")
            link = root / "link.md"
            link.symlink_to(markdown)
            with self.assertRaisesRegex(MeetingRecorderError, "safe regular"):
                cli._read_player_context(link)

    def test_saved_context_must_remain_private_and_match_its_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = _session(root, b"# Character\nOriginal")
            self.assertEqual(cli._load_session_player_context(session)[1], "# Character\nOriginal")
            (root / "player-context.md").write_text("# Character\nChanged", encoding="utf-8")
            with self.assertRaisesRegex(MeetingRecorderError, "recorded hash"):
                cli._load_session_player_context(session)

    def test_start_snapshots_private_context_and_rejects_it_for_other_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "character.md"
            source.write_text("# Character\nAster", encoding="utf-8")
            args = cli.build_parser().parse_args([
                "start", "--mode", "game-player", "--player-context", str(source),
                "--mic", "mic", "--system-source", "system",
            ])
            captured = []

            def capture(_mics, _system, directory, _rate):
                directory.mkdir()
                return SimpleNamespace(
                    pid=7, process=SimpleNamespace(), mixed_file=directory / "mixed.wav",
                    track_files={"mic0": directory / "mic0.wav", "system": directory / "system.wav"},
                    log_file=directory / "ffmpeg.log",
                )

            candidates = [
                SimpleNamespace(source="mic", kind="mic"),
                SimpleNamespace(source="system", kind="output"),
            ]
            with (
                patch("meeting_recorder.cli.state.session_lock", return_value=nullcontext()),
                patch("meeting_recorder.cli.state.load_session", return_value=None),
                patch("meeting_recorder.cli.audio.check_dependencies"),
                patch("meeting_recorder.cli.device_selection.discover", return_value=candidates),
                patch("meeting_recorder.cli.device_selection.resolve", side_effect=lambda selector, *_args, **_kwargs: SimpleNamespace(source=selector)),
                patch("meeting_recorder.cli.audio.start_recording", side_effect=capture),
                patch("meeting_recorder.cli.state.save_session", side_effect=captured.append),
            ):
                self.assertEqual(cli.cmd_start(args, config.AppConfig(root, 48000)), 0)

            session = captured[0]
            snapshot = Path(session.session_dir) / "player-context.md"
            self.assertEqual(snapshot.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
            self.assertEqual(session.player_context_file, "player-context.md")
            self.assertIsNotNone(session.player_context_sha256)

            invalid = cli.build_parser().parse_args([
                "start", "--mode", "meeting", "--player-context", str(source),
            ])
            with patch("meeting_recorder.cli.state.session_lock", return_value=nullcontext()), patch(
                "meeting_recorder.cli.state.load_session", return_value=None
            ):
                self.assertEqual(cli.cmd_start(invalid, config.AppConfig(root, 48000)), 1)

    def test_reprocess_uses_snapshot_by_default_and_archives_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            root = data_dir / "session"
            root.mkdir()
            original = b"# Character\nOriginal secret"
            session = _session(root, original)
            (root / "transcript.txt").write_text("The party reached the tower.", encoding="utf-8")
            for filename in ("player-recap.md", "character-notebook.md", "context-updates.md"):
                (root / filename).write_text("old", encoding="utf-8")
            session_archive.save_manifest(session, mode=modes.get_mode("game-player"), status="completed")
            response = (
                "# Player Recap\n\nRecap\n\n# Character Notebook\n\nNotes\n\n"
                "# Context Updates\n\nUpdates"
            )
            cfg = config.AppConfig(data_dir, 48000)
            with (
                patch("meeting_recorder.cli.state.session_lock", return_value=nullcontext()),
                patch("meeting_recorder.cli.state.load_session", return_value=None),
                patch("meeting_recorder.summarize._call_llm", return_value=response) as call_llm,
            ):
                self.assertEqual(cli.cmd_reprocess(_reprocess_args(), cfg), 0)
            self.assertIn("Original secret", call_llm.call_args.args[0])
            manifest = session_archive.load_manifest(root)
            self.assertEqual(manifest["reprocess_runs"][-1]["player_context"]["source"], "session_snapshot")

            override = data_dir / "updated.md"
            override.write_text("# Character\nUpdated secret", encoding="utf-8")
            with (
                patch("meeting_recorder.cli.state.session_lock", return_value=nullcontext()),
                patch("meeting_recorder.cli.state.load_session", return_value=None),
                patch("meeting_recorder.summarize._call_llm", return_value=response) as call_llm,
            ):
                self.assertEqual(cli.cmd_reprocess(_reprocess_args(player_context=override), cfg), 0)
            self.assertIn("Updated secret", call_llm.call_args.args[0])
            manifest = session_archive.load_manifest(root)
            run = manifest["reprocess_runs"][-1]
            self.assertEqual(run["player_context"]["source"], "override")
            archived = root / run["player_context"]["history_file"]
            self.assertEqual(archived.read_text(encoding="utf-8"), override.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(archived.stat().st_mode), 0o600)
            self.assertEqual(manifest["session"]["player_context"]["sha256"], session.player_context_sha256)
