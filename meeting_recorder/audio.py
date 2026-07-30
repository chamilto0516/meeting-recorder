"""PipeWire audio capture via FFmpeg.

Linux Mint (and most modern distros) run PipeWire with the `pipewire-pulse`
compatibility shim, which exposes the standard PulseAudio control/client
protocol on top of PipeWire's graph. That means:

  * `pactl` / `pw-cli` can be used to enumerate sources (mics) and sinks
    (speakers/headphones) exactly as on a PulseAudio system.
  * FFmpeg's battle-tested `-f pulse` input device talks to that same
    protocol, so we get PipeWire capture without depending on FFmpeg being
    built with the (less commonly packaged) native `--enable-libpipewire`
    input.

System audio (whatever a browser tab, Zoom, or Google Meet is playing) is
captured via the *monitor* source of the default sink -- i.e. "everything
currently being played out of the speakers/headphones", rather than trying to
hook into any specific application. This is robust to whichever app happens
to be making noise and requires no per-app configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from meeting_recorder.errors import CaptureValidationError, DependencyError, RecordingError
from meeting_recorder.state import ProcessIdentity

REQUIRED_BINARIES = ("ffmpeg", "pactl")


@dataclass
class SourceInfo:
    index: str
    name: str
    state: str

    @property
    def is_monitor(self) -> bool:
        return self.name.endswith(".monitor")


@dataclass
class RecordingHandle:
    pid: int
    process: ProcessIdentity
    command: list[str]
    log_file: Path
    mixed_file: Path
    track_files: dict[str, Path]


@dataclass
class TrackValidation:
    name: str
    path: str
    valid: bool
    sample_rate: int | None = None
    channels: int | None = None
    frames: int | None = None
    duration_seconds: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


@dataclass
class CaptureValidation:
    valid: bool
    tracks: list[TrackValidation]
    errors: list[str]
    log_errors: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "tracks": [track.to_dict() for track in self.tracks],
            "errors": self.errors,
            "log_errors": self.log_errors,
        }


def _command_fingerprint(command: bytes) -> str:
    return hashlib.sha256(command).hexdigest()


def get_process_identity(pid: int) -> ProcessIdentity | None:
    """Read stable identity facts for a Linux process from ``/proc``.

    A PID alone is not safe: Linux can reuse it after FFmpeg exits.  The
    process start time, argv fingerprint, executable, and process-group
    leader together identify the detached recorder we launched.
    """
    proc_dir = Path("/proc") / str(pid)
    try:
        stat = (proc_dir / "stat").read_text(encoding="utf-8")
        # The command field can itself contain spaces or parentheses.  Fields
        # after the final ')' begin at stat field 3; starttime is field 22.
        fields = stat[stat.rfind(")") + 2 :].split()
        start_time_ticks = int(fields[19])
        command = (proc_dir / "cmdline").read_bytes()
        executable = os.readlink(proc_dir / "exe")
        process_group = os.getpgid(pid)
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError, IndexError):
        return None
    return ProcessIdentity(
        start_time_ticks=start_time_ticks,
        executable=executable,
        command_fingerprint=_command_fingerprint(command),
        process_group=process_group,
    )


def verify_recording_process(pid: int, expected: ProcessIdentity) -> bool:
    """Return whether PID is still the exact detached FFmpeg we launched."""
    actual = get_process_identity(pid)
    return actual == expected and actual.process_group == pid


def check_dependencies() -> None:
    missing = [b for b in REQUIRED_BINARIES if shutil.which(b) is None]
    if missing:
        raise DependencyError(
            "Missing required tool(s): "
            + ", ".join(missing)
            + ". On Linux Mint, install with:\n"
            "  sudo apt install ffmpeg pulseaudio-utils\n"
            "(pactl is provided by pulseaudio-utils and talks to PipeWire's "
            "pipewire-pulse compatibility layer.)"
        )


def _run(cmd: list[str]) -> str:
    try:
        result = subprocess.run(
            cmd, check=True, capture_output=True, text=True, timeout=10
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RecordingError(f"Command failed: {' '.join(cmd)}\n{exc}") from exc
    return result.stdout


def list_sources() -> list[SourceInfo]:
    """Enumerate all PipeWire/Pulse audio *sources* (mic inputs and sink monitors)."""
    check_dependencies()
    output = _run(["pactl", "list", "short", "sources"])
    sources = []
    for line in output.strip().splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 4:
            continue
        index, name = fields[0], fields[1]
        state = fields[-1] if len(fields) > 4 else ""
        sources.append(SourceInfo(index=index, name=name, state=state))
    return sources


def validate_sources(mics: list[str], system_source: str) -> None:
    """Ensure selected Pulse sources still exist immediately before capture."""
    available = {source.name for source in list_sources()}
    missing = [source for source in [*mics, system_source] if source not in available]
    if missing:
        raise RecordingError(
            "Selected audio source(s) are no longer available: "
            + ", ".join(missing)
            + ". Run 'meeting-recorder list-devices' and reconnect/select them again."
        )


def get_default_source() -> str:
    """Default microphone/input source."""
    check_dependencies()
    return _run(["pactl", "get-default-source"]).strip()


def get_default_sink() -> str:
    check_dependencies()
    return _run(["pactl", "get-default-sink"]).strip()


def get_default_sink_monitor() -> str:
    """Monitor source of the default sink -- i.e. system/output audio."""
    return f"{get_default_sink()}.monitor"


def _build_ffmpeg_command(
    mics: list[str],
    system_source: str,
    session_dir: Path,
    sample_rate: int,
) -> tuple[list[str], dict[str, Path], Path]:
    labels = [f"mic{i}" for i in range(len(mics))] + ["system"]
    sources = list(mics) + [system_source]

    cmd: list[str] = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-n"]
    for source in sources:
        cmd += ["-thread_queue_size", "512", "-f", "pulse", "-i", source]

    filter_inputs = "".join(f"[{i}:a]" for i in range(len(labels)))
    filter_complex = f"{filter_inputs}amix=inputs={len(labels)}:normalize=0[mixed]"
    cmd += ["-filter_complex", filter_complex]

    track_files: dict[str, Path] = {}
    for idx, label in enumerate(labels):
        file_path = session_dir / f"{label}.wav"
        track_files[label] = file_path
        cmd += [
            "-map", f"{idx}:a", "-ac", "1", "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", str(file_path),
        ]

    mixed_file = session_dir / "mixed.wav"
    cmd += [
        "-map", "[mixed]", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(mixed_file),
    ]

    return cmd, track_files, mixed_file


def start_recording(
    mics: list[str],
    system_source: str,
    session_dir: Path,
    sample_rate: int,
) -> RecordingHandle:
    """Launch a detached FFmpeg process capturing all mic sources plus the
    system audio monitor, both as individual tracks and as a single mixed-down
    file (used later for transcription)."""
    check_dependencies()
    validate_sources(mics, system_source)
    try:
        session_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise RecordingError(f"Session directory already exists: {session_dir}") from exc
    try:
        probe = session_dir / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise RecordingError(f"Session directory is not writable: {session_dir}: {exc}") from exc

    cmd, track_files, mixed_file = _build_ffmpeg_command(
        mics, system_source, session_dir, sample_rate
    )

    log_file = session_dir / "ffmpeg.log"
    with open(log_file, "wb") as log_fh:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group -> can signal cleanly, survives CLI exit
            )
        except OSError as exc:
            raise RecordingError(f"Failed to launch ffmpeg: {exc}") from exc

    # Give ffmpeg a moment to fail fast and create valid WAV headers.
    time.sleep(0.75)
    if proc.poll() is not None:
        log_tail = log_file.read_text(errors="replace")[-2000:]
        raise RecordingError(
            f"ffmpeg exited immediately (code {proc.returncode}). Log tail:\n{log_tail}"
        )

    process = get_process_identity(proc.pid)
    if process is None or process.process_group != proc.pid:
        # Do not signal an unverified PID or process group, even during error
        # cleanup. A state record is not created, so a later `stop` cannot
        # authorize control of this process either.
        raise RecordingError("Could not verify identity of the FFmpeg recording process.")

    expected_files = {**track_files, "mixed": mixed_file}
    startup_errors = _validate_tracks(expected_files, sample_rate, require_frames=False)
    if startup_errors:
        stop_recording(proc.pid, process)
        log_tail = log_file.read_text(errors="replace")[-2000:]
        raise RecordingError(
            "ffmpeg did not create valid output headers: " + "; ".join(startup_errors)
            + f"\nLog tail:\n{log_tail}"
        )

    return RecordingHandle(
        pid=proc.pid,
        process=process,
        command=cmd,
        log_file=log_file,
        mixed_file=mixed_file,
        track_files=track_files,
    )


def _validate_track(
    name: str, path: Path, expected_sample_rate: int, require_frames: bool
) -> TrackValidation:
    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            frames = wav.getnframes()
            compression = wav.getcomptype()
            sample_width = wav.getsampwidth()
    except (EOFError, FileNotFoundError, OSError, wave.Error) as exc:
        return TrackValidation(name, str(path), False, error=f"unreadable WAV: {exc}")

    errors = []
    if compression != "NONE" or sample_width != 2:
        errors.append("expected uncompressed 16-bit PCM WAV")
    if channels != 1:
        errors.append(f"expected mono audio, got {channels} channel(s)")
    if sample_rate != expected_sample_rate:
        errors.append(f"expected {expected_sample_rate} Hz, got {sample_rate} Hz")
    if require_frames and frames <= 0:
        errors.append("contains no audio frames")
    return TrackValidation(
        name=name,
        path=str(path),
        valid=not errors,
        sample_rate=sample_rate,
        channels=channels,
        frames=frames,
        duration_seconds=frames / sample_rate if sample_rate else None,
        error="; ".join(errors) if errors else None,
    )


def _validate_tracks(
    files: dict[str, Path], expected_sample_rate: int, require_frames: bool
) -> list[str]:
    tracks = [
        _validate_track(name, path, expected_sample_rate, require_frames)
        for name, path in files.items()
    ]
    return [f"{track.name}: {track.error}" for track in tracks if not track.valid]


def _fatal_log_errors(log_file: Path) -> list[str]:
    try:
        text = log_file.read_text(errors="replace")
    except OSError as exc:
        return [f"could not read ffmpeg log: {exc}"]
    patterns = ("conversion failed", "error opening input", "error opening output", "invalid data found")
    return [line.strip() for line in text.splitlines() if any(pattern in line.lower() for pattern in patterns)]


def validate_capture(
    track_files: dict[str, Path], mixed_file: Path, sample_rate: int, log_file: Path
) -> CaptureValidation:
    """Validate finalized capture files and return a report suitable for saving."""
    files = {**track_files, "mixed": mixed_file}
    tracks = [
        _validate_track(name, path, sample_rate, require_frames=True)
        for name, path in files.items()
    ]
    errors = [f"{track.name}: {track.error}" for track in tracks if not track.valid]
    durations = [track.duration_seconds for track in tracks if track.duration_seconds is not None]
    if durations and max(durations) - min(durations) > max(2.0, max(durations) * 0.02):
        errors.append("track durations differ by more than 2 seconds or 2%")
    log_errors = _fatal_log_errors(log_file)
    errors.extend(f"ffmpeg: {line}" for line in log_errors)
    return CaptureValidation(not errors, tracks, errors, log_errors)


def save_capture_validation(report: CaptureValidation, session_dir: Path) -> Path:
    path = session_dir / "capture-validation.json"
    path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def require_valid_capture(report: CaptureValidation) -> None:
    if not report.valid:
        raise CaptureValidationError("Capture validation failed: " + "; ".join(report.errors))


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_recording(
    pid: int, expected: ProcessIdentity, timeout: float = 10.0
) -> bool:
    """Gracefully stop the ffmpeg process group so output files are finalized.

    Returns True if a running process was found and signaled, False if it was
    already gone.
    """
    if not verify_recording_process(pid, expected):
        return False

    def _signal(sig: signal.Signals) -> None:
        # Recheck immediately before every signal: the process might have
        # exited and its PID been reused since the preceding check.
        if not verify_recording_process(pid, expected):
            return
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    # SIGINT is what ffmpeg expects for a clean shutdown (flushes/finalizes files).
    _signal(signal.SIGINT)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not verify_recording_process(pid, expected):
            return True
        time.sleep(0.2)

    # Didn't exit cleanly -- escalate.
    _signal(signal.SIGTERM)
    time.sleep(1.0)
    if verify_recording_process(pid, expected):
        _signal(signal.SIGKILL)

    return True
