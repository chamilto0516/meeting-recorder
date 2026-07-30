"""Transcription of the mixed-down meeting audio via faster-whisper."""

from __future__ import annotations

from pathlib import Path

from meeting_recorder.config import WhisperConfig
from meeting_recorder.errors import TranscriptionError


def transcribe_audio(audio_path: Path, config: WhisperConfig) -> str:
    """Run faster-whisper over `audio_path` and return the full transcript text.

    The faster-whisper model is imported lazily so that commands which don't
    need it (start/status/list-devices) never pay the import cost or require
    the dependency to be installed for a quick sanity check.
    """
    if not audio_path.exists():
        raise TranscriptionError(f"Audio file not found: {audio_path}")

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - environment issue
        raise TranscriptionError(
            "faster-whisper is not installed. Install it with: pip install faster-whisper"
        ) from exc

    try:
        model = WhisperModel(
            config.model_size,
            device=config.device,
            compute_type=config.compute_type,
        )
        segments, _info = model.transcribe(
            str(audio_path),
            language=config.language,
            vad_filter=True,
        )
        text_parts = [segment.text.strip() for segment in segments]
    except Exception as exc:  # noqa: BLE001 - surface as our own error type
        raise TranscriptionError(f"faster-whisper transcription failed: {exc}") from exc

    return " ".join(part for part in text_parts if part)


def save_transcript(transcript: str, session_dir: Path, filename: str = "transcript.txt") -> Path:
    path = session_dir / filename
    path.write_text(transcript, encoding="utf-8")
    return path
