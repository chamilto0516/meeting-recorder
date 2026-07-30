"""Shared exception types."""


class MeetingRecorderError(Exception):
    """Base class for all expected/handled errors in this tool."""


class DependencyError(MeetingRecorderError):
    """Raised when a required external binary (ffmpeg, pactl, ...) is missing."""


class RecordingError(MeetingRecorderError):
    """Raised when starting/stopping the background recording fails."""


class CaptureValidationError(RecordingError):
    """Raised when recorded audio tracks are missing, corrupt, or inconsistent."""


class StateError(MeetingRecorderError):
    """Raised when active session state cannot be safely used."""


class NoActiveSessionError(MeetingRecorderError):
    """Raised when `stop`/`status` is invoked but no session is recorded."""


class TranscriptionError(MeetingRecorderError):
    """Raised when faster-whisper transcription fails."""


class SummarizationError(MeetingRecorderError):
    """Raised when the LiteLLM summarization call fails."""
