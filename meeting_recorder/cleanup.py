"""Safe planning and execution for retention-based session cleanup."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from meeting_recorder import session_archive, state
from meeting_recorder.config import CleanupConfig
from meeting_recorder.errors import MeetingRecorderError

_SESSION_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})(?:_|$)")
_RAW_AUDIO = re.compile(r"^mic\d+\.wav$")


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    session_id: str
    kind: str
    size: int
    device: int
    inode: int
    mtime_ns: int

    def identity(self) -> tuple[str, int, int, int, int]:
        return (str(self.path), self.size, self.device, self.inode, self.mtime_ns)


@dataclass
class CleanupPlan:
    data_dir: Path
    policy: CleanupConfig
    candidates: list[CleanupCandidate] = field(default_factory=list)
    sessions_scanned: int = 0
    recent_sessions: int = 0
    mixed_only_sessions: int = 0
    expired_sessions: int = 0
    protected_sessions: list[tuple[str, str]] = field(default_factory=list)
    skipped_files: list[Path] = field(default_factory=list)
    untouched_files: int = 0

    @property
    def reclaimable_bytes(self) -> int:
        return sum(candidate.size for candidate in self.candidates)

    def signature(self) -> tuple[tuple[str, int, int, int, int], ...]:
        return tuple(candidate.identity() for candidate in self.candidates)


@dataclass
class CleanupResult:
    deleted: list[CleanupCandidate] = field(default_factory=list)
    failures: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def reclaimed_bytes(self) -> int:
        return sum(candidate.size for candidate in self.deleted)


def _session_ended_at(session_dir: Path, manifest: dict | None) -> datetime | None:
    if manifest:
        value = manifest["session"].get("ended_at")
        if isinstance(value, str) and value:
            try:
                ended = datetime.fromisoformat(value)
            except ValueError:
                return None
            return ended.replace(tzinfo=timezone.utc) if ended.tzinfo is None else ended
    match = _SESSION_TIMESTAMP.match(session_dir.name)
    if not match:
        return None
    try:
        local = datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None
    return local.astimezone()


def _safe_name(value: object) -> str | None:
    if not isinstance(value, str) or not value or Path(value).name != value:
        return None
    return value


def _owned_files(session_dir: Path, manifest: dict | None) -> tuple[set[Path], Path, Path | None]:
    raw: set[Path] = set()
    mixed = session_dir / "mixed.wav"
    log: Path | None = None
    if manifest:
        saved = manifest["session"]
        audio = saved.get("audio", {})
        if not isinstance(audio, dict) or not isinstance(audio.get("tracks", {}), dict):
            raise MeetingRecorderError(
                f"Session manifest has malformed audio metadata: {session_archive.manifest_path(session_dir)}"
            )
        mixed_name = _safe_name(audio.get("mixed"))
        if mixed_name:
            mixed = session_dir / mixed_name
        for value in audio.get("tracks", {}).values():
            name = _safe_name(value)
            if name and name != mixed.name:
                raw.add(session_dir / name)
        log_name = _safe_name(saved.get("log_file"))
        if log_name:
            log = session_dir / log_name
    else:
        for path in session_dir.iterdir():
            if _RAW_AUDIO.fullmatch(path.name) or path.name == "system.wav":
                raw.add(path)
    return raw, mixed, log


def _candidate(path: Path, session_id: str, kind: str) -> CleanupCandidate | None:
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or not path.is_file():
        return None
    return CleanupCandidate(
        path=path,
        session_id=session_id,
        kind=kind,
        size=stat_result.st_size,
        device=stat_result.st_dev,
        inode=stat_result.st_ino,
        mtime_ns=stat_result.st_mtime_ns,
    )


def build_plan(
    data_dir: Path,
    policy: CleanupConfig,
    *,
    now: datetime | None = None,
) -> CleanupPlan:
    """Scan recorder-owned files and return an immutable deletion plan."""
    plan = CleanupPlan(data_dir=data_dir, policy=policy)
    if not data_dir.exists():
        return plan
    if not data_dir.is_dir() or data_dir.is_symlink():
        raise MeetingRecorderError(f"Session data directory is not a safe directory: {data_dir}")
    current = state.load_session()
    protected_dir = Path(current.session_dir).resolve() if current else None
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)

    for session_dir in sorted(data_dir.iterdir()):
        if session_dir.is_symlink() or not session_dir.is_dir():
            continue
        plan.sessions_scanned += 1
        try:
            resolved = session_dir.resolve()
        except OSError:
            plan.protected_sessions.append((session_dir.name, "unresolvable path"))
            continue
        if protected_dir is not None and resolved == protected_dir:
            plan.protected_sessions.append((session_dir.name, f"current {current.status} session"))
            continue
        manifest = session_archive.load_manifest(session_dir)
        if manifest:
            status = manifest["session"].get("status")
            if status != "completed":
                plan.protected_sessions.append((session_dir.name, f"status {status or 'unknown'}"))
                continue
        ended_at = _session_ended_at(session_dir, manifest)
        if ended_at is None or ended_at > clock:
            plan.protected_sessions.append((session_dir.name, "unknown or future session age"))
            continue
        age_days = (clock - ended_at.astimezone(clock.tzinfo)).total_seconds() / 86400
        raw_files, mixed_file, log_file = _owned_files(session_dir, manifest)
        selected: set[Path] = set()
        if age_days >= policy.raw_audio_days:
            selected.update(raw_files)
        if age_days >= policy.mixed_audio_days:
            selected.add(mixed_file)
            if manifest and manifest["session"].get("status") == "completed" and log_file:
                selected.add(log_file)
        if age_days < policy.raw_audio_days:
            plan.recent_sessions += 1
        elif age_days < policy.mixed_audio_days:
            plan.mixed_only_sessions += 1
        else:
            plan.expired_sessions += 1
        for path in sorted(selected):
            kind = "mixed" if path == mixed_file else "log" if path == log_file else "raw"
            item = _candidate(path, session_dir.name, kind)
            if item:
                plan.candidates.append(item)
            elif path.exists() or path.is_symlink():
                plan.skipped_files.append(path)
        for path in session_dir.iterdir():
            if path not in selected and path.is_file() and not path.is_symlink():
                plan.untouched_files += 1
            if path.suffix.lower() == ".wav" and path not in raw_files and path != mixed_file:
                plan.skipped_files.append(path)
    plan.candidates.sort(key=lambda item: str(item.path))
    plan.skipped_files = sorted(set(plan.skipped_files))
    return plan


def execute_plan(plan: CleanupPlan) -> CleanupResult:
    result = CleanupResult()
    for candidate in plan.candidates:
        try:
            stat_result = candidate.path.lstat()
            actual = (
                str(candidate.path),
                stat_result.st_size,
                stat_result.st_dev,
                stat_result.st_ino,
                stat_result.st_mtime_ns,
            )
            if candidate.path.is_symlink() or not candidate.path.is_file() or actual != candidate.identity():
                raise OSError("file changed after preview")
            candidate.path.unlink()
            result.deleted.append(candidate)
        except OSError as exc:
            result.failures.append((candidate.path, str(exc)))
    return result


def _format_size(size: int) -> str:
    value = float(size)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def render_plan(plan: CleanupPlan, *, verbose: bool = False) -> str:
    counts = {kind: 0 for kind in ("raw", "mixed", "log")}
    sizes = {kind: 0 for kind in counts}
    for candidate in plan.candidates:
        counts[candidate.kind] += 1
        sizes[candidate.kind] += candidate.size
    lines = [
        "Cleanup preview",
        f"Policy: raw tracks {plan.policy.raw_audio_days} days; mixed audio {plan.policy.mixed_audio_days} days",
        "",
        f"Scanned: {plan.sessions_scanned} sessions",
        "",
        "Will delete:",
        f"  {counts['raw']} raw track(s) ({_format_size(sizes['raw'])})",
        f"  {counts['mixed']} mixed track(s) ({_format_size(sizes['mixed'])})",
        f"  {counts['log']} completed-capture log(s) ({_format_size(sizes['log'])})",
        "",
        "Will keep:",
        f"  {plan.recent_sessions} recent session(s) with all audio",
        f"  {plan.mixed_only_sessions} session(s) with mixed audio only",
        f"  {plan.untouched_files} transcript, document, metadata, or other file(s)",
        f"  {len(plan.protected_sessions)} protected session(s)",
        f"  {len(plan.skipped_files)} unknown or unsafe file(s) skipped",
        "",
        f"Potential space reclaimed: {_format_size(plan.reclaimable_bytes)}",
    ]
    if verbose:
        if plan.candidates:
            lines.extend(("", "Files to delete:"))
            lines.extend(f"  {item.path} ({_format_size(item.size)})" for item in plan.candidates)
        if plan.protected_sessions:
            lines.extend(("", "Protected sessions:"))
            lines.extend(f"  {name}: {reason}" for name, reason in plan.protected_sessions)
        if plan.skipped_files:
            lines.extend(("", "Skipped files:"))
            lines.extend(f"  {path}" for path in plan.skipped_files)
    return "\n".join(lines)


def render_result(result: CleanupResult) -> str:
    lines = [
        "",
        f"Deleted {len(result.deleted)} file(s); reclaimed {_format_size(result.reclaimed_bytes)}.",
    ]
    if result.failures:
        lines.append(f"Failed to delete {len(result.failures)} file(s):")
        lines.extend(f"  {path}: {error}" for path, error in result.failures)
    return "\n".join(lines)
