"""Private, crash-safe persistence for the in-progress recording session.

The state file is an authorization handle for stopping a detached FFmpeg
process.  It therefore contains a process identity as well as a PID, is
written atomically, and must never contain credentials.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from meeting_recorder.errors import StateError

APP_NAME = "meeting-recorder"
STATE_VERSION = 4
SUPPORTED_STATE_VERSIONS = {2, 3, STATE_VERSION}
SESSION_STATUSES = {"recording", "recorded", "transcribed", "capture_failed"}


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    directory = Path(base) / APP_NAME
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def state_file() -> Path:
    return state_dir() / "current_session.json"


@contextmanager
def session_lock() -> Iterator[None]:
    """Serialize state operations across separate CLI invocations."""
    lock_path = state_dir() / "current_session.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@dataclass(frozen=True)
class ProcessIdentity:
    """Linux-specific facts that prevent a reused PID from being signaled."""

    start_time_ticks: int
    executable: str
    command_fingerprint: str
    process_group: int


@dataclass
class Session:
    session_id: str
    pid: int
    process: Optional[ProcessIdentity]
    started_at: str
    session_dir: str
    mixed_file: str
    track_files: dict[str, str]
    mics: list[str]
    system_source: str
    sample_rate: int
    log_file: Optional[str] = None
    whisper: dict[str, Any] = field(default_factory=dict)
    # Deliberately excludes api_key.  Credentials are resolved at stop time.
    llm: dict[str, Any] = field(default_factory=dict)
    status: str = "recording"
    ended_at: Optional[str] = None
    transcript_file: Optional[str] = None
    summary_file: Optional[str] = None
    capture_report: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state_version"] = STATE_VERSION
        if "api_key" in data["llm"]:
            raise StateError("Refusing to persist an LLM API key in session state.")
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        version = data.get("state_version")
        if version not in SUPPORTED_STATE_VERSIONS:
            raise StateError(
                "Session state is from an unsupported version and was not used. "
                f"Remove {state_file()} after confirming no recording is active."
            )
        data = dict(data)
        data.pop("state_version", None)
        try:
            process_data = data.pop("process")
            process = ProcessIdentity(**process_data) if process_data else None
            session = cls(process=process, **data)
        except (KeyError, TypeError) as exc:
            raise StateError("Session state is malformed and was not used.") from exc
        if not isinstance(session.llm, dict) or not isinstance(session.whisper, dict):
            raise StateError("Session state is malformed and was not used.")
        if "api_key" in session.llm:
            raise StateError("Session state contains a credential and was not used.")
        if session.status not in SESSION_STATUSES:
            raise StateError("Session state has an unsupported status and was not used.")
        if session.status == "recording" and session.process is None:
            raise StateError("Recording session state lacks a process identity and was not used.")
        return session


def save_session(session: Session) -> None:
    """Atomically write a private state file, preserving the prior file on failure."""
    path = state_file()
    payload = json.dumps(session.to_dict(), indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=".current_session.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise StateError(f"Could not save session state: {exc}") from exc
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def load_session() -> Optional[Session]:
    path = state_file()
    if not path.exists():
        return None
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise StateError(
            f"Session state at {path} does not have private permissions and was not used."
        )
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(
            f"Session state at {path} is unreadable and was not used. "
            "Remove it after confirming no recording is active."
        ) from exc
    if not isinstance(data, dict):
        raise StateError("Session state is malformed and was not used.")
    return Session.from_dict(data)


def clear_session() -> None:
    path = state_file()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise StateError(f"Could not clear session state: {exc}") from exc
