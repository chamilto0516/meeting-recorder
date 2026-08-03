from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path

from meeting_recorder import audio


def write_wav(path: Path, sample_rate: int = 48000, frames: int = 480) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\0\0" * frames)


class CaptureValidationTests(unittest.TestCase):
    def test_ffmpeg_command_uses_safe_explicit_capture_options(self) -> None:
        command, tracks, mixed = audio._build_ffmpeg_command(
            ["mic-source"], "system.monitor", Path("/tmp/session"), 48000
        )
        self.assertIn("-nostdin", command)
        self.assertIn("-n", command)
        self.assertEqual(command.count("-thread_queue_size"), 2)
        self.assertEqual(command.count("pcm_s16le"), 3)
        self.assertEqual(tracks["mic0"], Path("/tmp/session/mic0.wav"))
        self.assertEqual(mixed, Path("/tmp/session/mixed.wav"))

    def test_ffmpeg_command_supports_system_audio_only_capture(self) -> None:
        command, tracks, mixed = audio._build_ffmpeg_command(
            [], "system.monitor", Path("/tmp/session"), 48000
        )
        self.assertEqual(list(tracks), ["system"])
        self.assertEqual(command.count("pcm_s16le"), 2)
        self.assertEqual(mixed, Path("/tmp/session/mixed.wav"))

    def test_ffmpeg_command_supports_microphone_only_capture(self) -> None:
        command, tracks, mixed = audio._build_ffmpeg_command(
            ["mic-source"], None, Path("/tmp/session"), 48000
        )
        self.assertEqual(list(tracks), ["mic0"])
        self.assertEqual(command.count("pcm_s16le"), 2)
        self.assertEqual(mixed, Path("/tmp/session/mixed.wav"))

    def test_accepts_complete_consistent_pcm_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracks = {name: root / f"{name}.wav" for name in ("mic0", "system")}
            mixed = root / "mixed.wav"
            for path in [*tracks.values(), mixed]:
                write_wav(path)
            log = root / "ffmpeg.log"
            log.write_text("", encoding="utf-8")

            report = audio.validate_capture(tracks, mixed, 48000, log)

            self.assertTrue(report.valid)
            self.assertEqual(len(report.tracks), 3)
            self.assertTrue(all(track.frames == 480 for track in report.tracks))

    def test_rejects_truncated_or_empty_track(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mic = root / "mic0.wav"
            system = root / "system.wav"
            mixed = root / "mixed.wav"
            write_wav(mic)
            system.write_bytes(b"not a wav")
            write_wav(mixed)
            log = root / "ffmpeg.log"
            log.write_text("", encoding="utf-8")

            report = audio.validate_capture({"mic0": mic, "system": system}, mixed, 48000, log)

            self.assertFalse(report.valid)
            self.assertTrue(any(error.startswith("system: unreadable WAV") for error in report.errors))

    def test_rejects_mismatched_format_duration_and_fatal_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mic = root / "mic0.wav"
            system = root / "system.wav"
            mixed = root / "mixed.wav"
            write_wav(mic, frames=480)
            write_wav(system, sample_rate=44100, frames=480)
            write_wav(mixed, frames=48_000 * 5)
            log = root / "ffmpeg.log"
            log.write_text("Conversion failed!", encoding="utf-8")

            report = audio.validate_capture({"mic0": mic, "system": system}, mixed, 48000, log)

            self.assertFalse(report.valid)
            self.assertTrue(any("expected 48000 Hz" in error for error in report.errors))
            self.assertTrue(any("track durations differ" in error for error in report.errors))
            self.assertTrue(any(error.startswith("ffmpeg:") for error in report.errors))
