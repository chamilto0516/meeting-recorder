"""Small, typed bridge used by the PySide6 control panel.

It deliberately calls the same command functions as the console interface so
the panel retains the recorder's locking and process-identity guarantees.
"""
from __future__ import annotations

from argparse import Namespace
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from typing import Callable, Optional

from meeting_recorder import cli, config, device_selection, state


@dataclass(frozen=True)
class Device:
    id: str
    label: str
    is_default: bool
    token: str
    aliases: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        parts = [self.token, *self.aliases, self.label]
        text = " · ".join(parts)
        return text + (" (default)" if self.is_default else "")


@dataclass(frozen=True)
class ActiveSession:
    phase: str
    session_id: str
    name: str
    mode: str
    mics: list[str]
    system_source: Optional[str]
    started_at: datetime
    directory: Path
    error: Optional[str] = None


def devices() -> tuple[list[Device], list[Device]]:
    cfg = config.load_config(_args())
    candidates = device_selection.discover(cfg.device_aliases)
    device = lambda candidate: Device(
        candidate.source, candidate.label, candidate.is_default, candidate.token, candidate.aliases
    )
    return (
        [device(candidate) for candidate in candidates if candidate.kind == "mic"],
        [device(candidate) for candidate in candidates if candidate.kind == "output"],
    )


def active_session() -> ActiveSession | None:
    session = state.load_session()
    if session is None:
        return None
    phase = "recording" if session.status == "recording" else "processing"
    return ActiveSession(
        phase=phase, session_id=session.session_id, name=session.meeting_name or session.session_id,
        mode=session.mode, mics=session.mics, system_source=session.system_source,
        started_at=datetime.fromisoformat(session.started_at), directory=Path(session.session_dir),
        error=session.last_error,
    )


def _args(**values) -> Namespace:
    defaults = dict(
        config=None, data_dir=None, verbose=False, mode="meeting", mics=None,
        device_config=None, system_source=None, sample_rate=None, meeting_name=None, lecture=False, game=False,
        player_context=None,
        allow_llm_device_selection=False,
        skip_transcription=False, skip_summary=False, whisper_model=None,
        whisper_device=None, whisper_compute_type=None, language=None, llm_model=None,
        llm_endpoint=None, llm_api_key=None,
    )
    defaults.update(values)
    return Namespace(**defaults)


def start(
    *, mics: list[str], system_source: str | None, mode: str, name: str | None,
    player_context: Path | None = None,
) -> ActiveSession:
    args = _args(
        mics=mics or None, system_source=system_source, mode=mode,
        meeting_name=name or None, player_context=player_context,
    )
    cfg = config.load_config(args)
    if cli.cmd_start(args, cfg) != 0:
        raise RuntimeError("Recording could not be started; see the application log for details.")
    session = active_session()
    if session is None:
        raise RuntimeError("Recording started but no active session was saved.")
    _write_history(session, "recording")
    return session


def stop_or_retry(retry: bool, callback: Callable[[str, int], None]) -> None:
    session = active_session()
    if session is None:
        raise RuntimeError("No saved session needs processing.")
    args = _args()
    cfg = config.load_config(args)
    command = cli.cmd_retry if retry else cli.cmd_stop
    try:
        # The CLI prints the completed Markdown summary for terminal users.
        # The panel exposes the session folder instead, so keep stdout quiet.
        with redirect_stdout(io.StringIO()):
            result = command(args, cfg, progress_callback=callback)
        if result != 0:
            raise RuntimeError("Processing could not be started; see the application log for details.")
    except Exception as exc:
        failed = active_session() or session
        _write_history(failed, "failed", str(exc))
        raise
    _write_history(session, "done")


def _write_history(session: ActiveSession, status: str, error: str | None = None) -> None:
    """Store panel-only, non-secret history next to session artifacts."""
    payload = {
        "name": session.name,
        "mode": session.mode,
        "started_at": session.started_at.isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "error": error,
    }
    session.directory.mkdir(parents=True, exist_ok=True)
    (session.directory / ".meeting-recorder-panel.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
