"""Persist the in-progress recording session across separate CLI invocations.

`start` and `stop` are two different process runs, potentially far apart in
time, so anything `stop` needs to know (PID, output file paths, which
whisper/LLM settings were requested at start time) has to be written down
somewhere durable. We use a single JSON file under XDG_STATE_HOME, since only
one recording session is supported at a time.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

APP_NAME = "meeting-recorder"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_file() -> Path:
    return state_dir() / "current_session.json"


@dataclass
class Session:
    session_id: str
    pid: int
    started_at: str
    session_dir: str
    mixed_file: str
    track_files: dict[str, str]
    mics: list[str]
    system_source: str
    sample_rate: int
    whisper: dict[str, Any] = field(default_factory=dict)
    llm: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        return cls(**data)


def save_session(session: Session) -> None:
    with state_file().open("w", encoding="utf-8") as fh:
        json.dump(session.to_dict(), fh, indent=2)


def load_session() -> Optional[Session]:
    path = state_file()
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return Session.from_dict(data)


def clear_session() -> None:
    path = state_file()
    if path.exists():
        path.unlink()
