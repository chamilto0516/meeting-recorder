"""Durable, non-secret metadata stored beside each session's artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meeting_recorder import modes, state
from meeting_recorder.errors import StateError

MANIFEST_FILENAME = "session.json"
MANIFEST_VERSION = 1


def manifest_path(session_dir: Path) -> Path:
    return session_dir / MANIFEST_FILENAME


def _basename(value: str | None) -> str | None:
    return Path(value).name if value else None


def _session_payload(session: state.Session, status: str | None = None) -> dict[str, Any]:
    if "api_key" in session.llm:
        raise StateError("Refusing to archive an LLM API key in session metadata.")
    return {
        "session_id": session.session_id,
        "meeting_name": session.meeting_name,
        "mode": session.mode,
        "status": status or session.status,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "sample_rate": session.sample_rate,
        "mics": list(session.mics),
        "system_source": session.system_source,
        "audio": {
            "mixed": _basename(session.mixed_file),
            "tracks": {name: Path(path).name for name, path in session.track_files.items()},
        },
        "log_file": _basename(session.log_file),
        "transcript_file": _basename(session.transcript_file),
        "summary_file": _basename(session.summary_file),
        "capture_report": _basename(session.capture_report),
        "player_context": (
            {"file": session.player_context_file, "sha256": session.player_context_sha256}
            if session.player_context_file else None
        ),
        "whisper": dict(session.whisper),
        "llm": dict(session.llm),
        "last_error": session.last_error,
    }


def load_manifest(session_dir: Path) -> dict[str, Any] | None:
    path = manifest_path(session_dir)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise StateError(f"Session manifest at {path} is not a safe regular file.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Session manifest at {path} is unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("manifest_version") != MANIFEST_VERSION:
        raise StateError(f"Session manifest at {path} has an unsupported format.")
    if not isinstance(data.get("session"), dict):
        raise StateError(f"Session manifest at {path} is malformed.")
    saved_llm = data["session"].get("llm", {})
    if not isinstance(saved_llm, dict):
        raise StateError(f"Session manifest at {path} has malformed LLM metadata.")
    if "api_key" in saved_llm:
        raise StateError(f"Session manifest at {path} contains a credential and was not used.")
    return data


def save_manifest(
    session: state.Session,
    *,
    mode: modes.ModeDefinition | None = None,
    status: str | None = None,
    reprocess_run: dict[str, Any] | None = None,
) -> Path:
    """Atomically create or update a durable session manifest."""
    session_dir = Path(session.session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_path(session_dir)
    existing = load_manifest(session_dir) if path.exists() else None
    snapshot = existing.get("mode_snapshot") if existing else None
    if snapshot is None and mode is not None:
        snapshot = modes.mode_snapshot(mode)
    runs = list(existing.get("reprocess_runs", [])) if existing else []
    if reprocess_run is not None:
        runs.append(reprocess_run)
    payload = {
        "manifest_version": MANIFEST_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "session": _session_payload(session, status),
        "mode_snapshot": snapshot,
        "reprocess_runs": runs,
    }
    encoded = json.dumps(payload, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=".session.", dir=session_dir)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise StateError(f"Could not save session manifest {path}: {exc}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)
    return path
