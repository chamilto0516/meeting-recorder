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

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from meeting_recorder.errors import DependencyError, RecordingError

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
    command: list[str]
    log_file: Path
    mixed_file: Path
    track_files: dict[str, Path]


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

    cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y"]
    for source in sources:
        cmd += ["-f", "pulse", "-i", source]

    filter_inputs = "".join(f"[{i}:a]" for i in range(len(labels)))
    filter_complex = f"{filter_inputs}amix=inputs={len(labels)}:normalize=0[mixed]"
    cmd += ["-filter_complex", filter_complex]

    track_files: dict[str, Path] = {}
    for idx, label in enumerate(labels):
        file_path = session_dir / f"{label}.wav"
        track_files[label] = file_path
        cmd += ["-map", f"{idx}:a", "-ac", "1", "-ar", str(sample_rate), str(file_path)]

    mixed_file = session_dir / "mixed.wav"
    cmd += ["-map", "[mixed]", "-ac", "1", "-ar", str(sample_rate), str(mixed_file)]

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
    session_dir.mkdir(parents=True, exist_ok=True)

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

    # Give ffmpeg a brief moment to fail fast (bad device name, busy device, ...)
    time.sleep(0.5)
    if proc.poll() is not None:
        log_tail = log_file.read_text(errors="replace")[-2000:]
        raise RecordingError(
            f"ffmpeg exited immediately (code {proc.returncode}). Log tail:\n{log_tail}"
        )

    return RecordingHandle(
        pid=proc.pid,
        command=cmd,
        log_file=log_file,
        mixed_file=mixed_file,
        track_files=track_files,
    )


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_recording(pid: int, timeout: float = 10.0) -> bool:
    """Gracefully stop the ffmpeg process group so output files are finalized.

    Returns True if a running process was found and signaled, False if it was
    already gone.
    """
    if not is_pid_running(pid):
        return False

    def _signal(sig: signal.Signals) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    # SIGINT is what ffmpeg expects for a clean shutdown (flushes/finalizes files).
    _signal(signal.SIGINT)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_running(pid):
            return True
        time.sleep(0.2)

    # Didn't exit cleanly -- escalate.
    _signal(signal.SIGTERM)
    time.sleep(1.0)
    if is_pid_running(pid):
        _signal(signal.SIGKILL)

    return True
