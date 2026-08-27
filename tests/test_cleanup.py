from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from meeting_recorder import cleanup, cli, config, modes, session_archive, state


def make_session(root: Path, ended_at: datetime, status: str = "completed") -> state.Session:
    session = state.Session(
        session_id=root.name,
        pid=0,
        process=None,
        started_at=(ended_at - timedelta(hours=1)).isoformat(),
        ended_at=ended_at.isoformat(),
        session_dir=str(root),
        mixed_file=str(root / "mixed.wav"),
        track_files={"mic0": str(root / "mic0.wav"), "system": str(root / "system.wav")},
        mics=["mic"],
        system_source="system.monitor",
        sample_rate=48000,
        log_file=str(root / "ffmpeg.log"),
        mode="meeting",
        whisper=config.WhisperConfig().to_dict(),
        llm=config.LLMConfig().to_session_dict(),
        status="transcribed" if status == "completed" else status,
        transcript_file=str(root / "transcript.txt"),
        summary_file=str(root / "summary.md"),
    )
    root.mkdir(parents=True)
    for name, size in (
        ("mic0.wav", 10), ("system.wav", 20), ("mixed.wav", 30),
        ("ffmpeg.log", 4), ("transcript.txt", 5), ("summary.md", 6),
    ):
        (root / name).write_bytes(b"x" * size)
    session_archive.save_manifest(session, mode=modes.get_mode("meeting"), status=status)
    return session


def args(**updates) -> Namespace:
    values = dict(
        session="", mode=None, retranscribe=False, use_saved_prompt=False,
        whisper_model=None, whisper_device=None, whisper_compute_type=None,
        language=None, llm_model=None, llm_endpoint=None, llm_api_key=None,
        verbose=False, dry_run=False, yes=False,
    )
    values.update(updates)
    return Namespace(**values)


class CleanupTests(unittest.TestCase):
    def test_retention_boundaries_keep_documents_and_count_exact_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
            make_session(data_dir / "recent", now - timedelta(days=6, hours=23))
            make_session(data_dir / "middle", now - timedelta(days=7))
            make_session(data_dir / "expired", now - timedelta(days=14))
            with patch.object(state, "load_session", return_value=None):
                plan = cleanup.build_plan(data_dir, config.CleanupConfig(), now=now)

            self.assertEqual((plan.recent_sessions, plan.mixed_only_sessions, plan.expired_sessions), (1, 1, 1))
            self.assertEqual([item.kind for item in plan.candidates].count("raw"), 4)
            self.assertEqual([item.kind for item in plan.candidates].count("mixed"), 1)
            self.assertEqual([item.kind for item in plan.candidates].count("log"), 1)
            self.assertEqual(plan.reclaimable_bytes, 94)
            self.assertNotIn(data_dir / "expired" / "transcript.txt", [item.path for item in plan.candidates])

    def test_current_and_failed_sessions_are_always_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            now = datetime(2026, 8, 27, tzinfo=timezone.utc)
            active = make_session(data_dir / "active", now - timedelta(days=30))
            failed = make_session(data_dir / "failed", now - timedelta(days=30), "capture_failed")
            active.status = "recorded"
            with patch.object(state, "load_session", return_value=active):
                plan = cleanup.build_plan(data_dir, config.CleanupConfig(), now=now)
            self.assertEqual(plan.candidates, [])
            self.assertEqual({name for name, _ in plan.protected_sessions}, {"active", "failed"})

    def test_unknown_wav_and_symlink_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            now = datetime(2026, 8, 27, tzinfo=timezone.utc)
            root = make_session(data_dir / "old", now - timedelta(days=20))
            unknown = root.session_dir and Path(root.session_dir) / "edited.wav"
            unknown.write_bytes(b"manual")
            link = Path(root.session_dir) / "mic9.wav"
            link.symlink_to(unknown)
            with patch.object(state, "load_session", return_value=None):
                plan = cleanup.build_plan(data_dir, config.CleanupConfig(), now=now)
            self.assertIn(unknown, plan.skipped_files)
            self.assertNotIn(link, [item.path for item in plan.candidates])

    def test_execution_refuses_a_file_changed_after_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            now = datetime(2026, 8, 27, tzinfo=timezone.utc)
            make_session(data_dir / "old", now - timedelta(days=20))
            with patch.object(state, "load_session", return_value=None):
                plan = cleanup.build_plan(data_dir, config.CleanupConfig(), now=now)
            target = plan.candidates[0].path
            target.write_bytes(b"changed")
            result = cleanup.execute_plan(plan)
            self.assertTrue(target.exists())
            self.assertEqual(len(result.failures), 1)

    def test_cleanup_config_validation(self) -> None:
        with self.assertRaisesRegex(Exception, "non-negative"):
            config.CleanupConfig.from_dict({"raw_audio_days": -1, "mixed_audio_days": 14})
        with self.assertRaisesRegex(Exception, "greater than or equal"):
            config.CleanupConfig.from_dict({"raw_audio_days": 15, "mixed_audio_days": 14})


class ReprocessTests(unittest.TestCase):
    def test_prompt_only_reprocess_uses_current_prompt_without_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            root = data_dir / "session"
            session = make_session(root, datetime.now(timezone.utc) - timedelta(days=20))
            (root / "mic0.wav").unlink()
            (root / "system.wav").unlink()
            (root / "mixed.wav").unlink()
            cfg = config.AppConfig(data_dir, 48000)
            output = root / "summary.md"
            with (
                patch.object(state, "session_lock", return_value=nullcontext()),
                patch.object(state, "load_session", return_value=None),
                patch("meeting_recorder.summarize.summarize_mode", return_value=[("summary.md", "# Meeting Summary\n\nNew")]) as call,
                patch("meeting_recorder.summarize.save_mode_summaries", return_value=[output]),
            ):
                self.assertEqual(cli.cmd_reprocess(args(session="session"), cfg), 0)
            self.assertEqual(call.call_args.args[0], "xxxxx")
            manifest = session_archive.load_manifest(root)
            self.assertEqual(manifest["reprocess_runs"][-1]["prompt_source"], "current")
            self.assertTrue((root / ".reprocess-history").is_dir())

    def test_retranscribe_requires_unexpired_mixed_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            root = data_dir / "session"
            make_session(root, datetime.now(timezone.utc) - timedelta(days=20))
            (root / "mixed.wav").unlink()
            cfg = config.AppConfig(data_dir, 48000)
            with (
                patch.object(state, "session_lock", return_value=nullcontext()),
                patch.object(state, "load_session", return_value=None),
            ):
                with self.assertRaisesRegex(Exception, "expired under the cleanup policy"):
                    cli.cmd_reprocess(args(session="session", retranscribe=True), cfg)
            self.assertFalse((root / ".reprocess-history").exists())

    def test_manifest_rejects_api_keys_and_round_trips_saved_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "session"
            session = make_session(root, datetime.now(timezone.utc))
            manifest = session_archive.load_manifest(root)
            saved_mode = modes.mode_from_snapshot(manifest["mode_snapshot"])
            self.assertEqual(saved_mode.prompt, modes.get_mode("meeting").prompt)
            session.llm["api_key"] = "secret"
            with self.assertRaisesRegex(Exception, "API key"):
                session_archive.save_manifest(session)
