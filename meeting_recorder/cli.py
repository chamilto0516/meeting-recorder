"""argparse entry point for meeting-recorder.

    meeting-recorder start   [options]   # begin background recording
    meeting-recorder stop    [options]   # stop, transcribe, summarize
    meeting-recorder status              # is a recording currently running?
    meeting-recorder list-devices        # show available PipeWire/Pulse sources
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from meeting_recorder import audio, config as config_mod, state, summarize, transcribe
from meeting_recorder.errors import MeetingRecorderError

logger = logging.getLogger("meeting_recorder")


def _add_common_processing_args(parser: argparse.ArgumentParser) -> None:
    """Flags shared by `start` (to set defaults for the session) and `stop`
    (to override them at processing time)."""
    whisper_group = parser.add_argument_group("faster-whisper")
    whisper_group.add_argument(
        "--whisper-model",
        help="faster-whisper model size or path (default: base, or config file value)",
    )
    whisper_group.add_argument(
        "--whisper-device",
        choices=["cpu", "cuda"],
        help="Device to run whisper inference on (default: cpu)",
    )
    whisper_group.add_argument(
        "--whisper-compute-type",
        help="CTranslate2 compute type, e.g. int8, float16, float32 (default: int8)",
    )
    whisper_group.add_argument(
        "--language",
        help="Force transcription language (e.g. 'en'). Default: auto-detect.",
    )

    llm_group = parser.add_argument_group("LiteLLM / summarization")
    llm_group.add_argument(
        "--llm-model",
        help="LiteLLM model string, e.g. 'ollama/llama3.1' (default: config file value)",
    )
    llm_group.add_argument(
        "--llm-endpoint",
        help="Base URL of the inference server, e.g. http://localhost:11434",
    )
    llm_group.add_argument(
        "--llm-api-key",
        help="API key for the LLM endpoint, if required (unset for local Ollama)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meeting-recorder",
        description=(
            "Record a local meeting (mic + system audio) via PipeWire/FFmpeg, "
            "then transcribe with faster-whisper and summarize with an LLM via LiteLLM."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to a YAML config file (default: ~/.config/meeting-recorder/config.yaml)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Base directory for session recordings/transcripts/summaries",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    start_p = subparsers.add_parser(
        "start", help="Start a background recording of mic(s) + system audio"
    )
    start_p.add_argument(
        "--mic",
        action="append",
        dest="mics",
        metavar="SOURCE",
        help=(
            "PipeWire/Pulse source name for a microphone. Repeat to record "
            "multiple mics (e.g. --mic alsa_input.usb-... --mic alsa_input.pci-...). "
            "Defaults to the system default input source. Run "
            "'meeting-recorder list-devices' to see available names."
        ),
    )
    start_p.add_argument(
        "--system-source",
        dest="system_source",
        metavar="SOURCE",
        help=(
            "PipeWire/Pulse monitor source to capture system audio (browser tabs, "
            "Zoom, Google Meet, etc). Defaults to the default sink's monitor, i.e. "
            "'whatever is currently playing out loud'."
        ),
    )
    start_p.add_argument(
        "--sample-rate", type=int, help="Recording sample rate in Hz (default: 48000)"
    )
    _add_common_processing_args(start_p)
    start_p.set_defaults(func=cmd_start)

    stop_p = subparsers.add_parser(
        "stop", help="Stop recording, then transcribe and summarize the meeting"
    )
    stop_p.add_argument(
        "--skip-transcription",
        action="store_true",
        help="Stop the recording but do not run faster-whisper or summarization",
    )
    stop_p.add_argument(
        "--skip-summary",
        action="store_true",
        help="Transcribe but do not call the LLM for a summary",
    )
    _add_common_processing_args(stop_p)
    stop_p.set_defaults(func=cmd_stop)

    status_p = subparsers.add_parser("status", help="Show whether a recording is active")
    status_p.set_defaults(func=cmd_status)

    list_p = subparsers.add_parser(
        "list-devices", help="List available PipeWire/Pulse audio sources"
    )
    list_p.set_defaults(func=cmd_list_devices)

    return parser


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_start(args: argparse.Namespace, cfg: config_mod.AppConfig) -> int:
    existing = state.load_session()
    if existing is not None and audio.is_pid_running(existing.pid):
        logger.error(
            "A recording is already in progress (session %s, pid %s). "
            "Run 'meeting-recorder stop' first.",
            existing.session_id,
            existing.pid,
        )
        return 1
    if existing is not None:
        logger.warning(
            "Found a stale session record (process no longer running); replacing it."
        )
        state.clear_session()

    audio.check_dependencies()

    mics = args.mics or [audio.get_default_source()]
    system_source = args.system_source or audio.get_default_sink_monitor()

    session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    session_dir = cfg.data_dir / session_id

    logger.info("Mic source(s): %s", ", ".join(mics))
    logger.info("System audio source: %s", system_source)
    logger.info("Session directory: %s", session_dir)

    handle = audio.start_recording(mics, system_source, session_dir, cfg.sample_rate)

    session = state.Session(
        session_id=session_id,
        pid=handle.pid,
        started_at=datetime.now(timezone.utc).isoformat(),
        session_dir=str(session_dir),
        mixed_file=str(handle.mixed_file),
        track_files={k: str(v) for k, v in handle.track_files.items()},
        mics=mics,
        system_source=system_source,
        sample_rate=cfg.sample_rate,
        whisper=cfg.whisper.to_dict(),
        llm=cfg.llm.to_dict(),
    )
    state.save_session(session)

    logger.info("Recording started (pid %s).", handle.pid)
    logger.info("Run 'meeting-recorder stop' when the meeting ends.")
    return 0


def cmd_stop(args: argparse.Namespace, cfg: config_mod.AppConfig) -> int:
    session = state.load_session()
    if session is None:
        logger.error("No active recording session found. Nothing to stop.")
        return 1

    logger.info("Stopping recording (pid %s)...", session.pid)
    was_running = audio.stop_recording(session.pid)
    if not was_running:
        logger.warning(
            "Recording process was not running (it may have crashed). "
            "Attempting to process whatever audio was captured."
        )
    state.clear_session()

    mixed_file = Path(session.mixed_file)
    if not mixed_file.exists() or mixed_file.stat().st_size == 0:
        logger.error("No audio was captured at %s; nothing to transcribe.", mixed_file)
        return 1

    logger.info("Recording saved. Tracks: %s", ", ".join(session.track_files.keys()))

    if args.skip_transcription:
        logger.info("Skipping transcription/summarization (--skip-transcription).")
        return 0

    whisper_cfg = config_mod.override_whisper(
        config_mod.WhisperConfig.from_dict(session.whisper), args
    )
    llm_cfg = config_mod.override_llm(
        config_mod.LLMConfig.from_dict(session.llm), args
    )

    session_dir = Path(session.session_dir)

    logger.info("Transcribing with faster-whisper (model=%s)...", whisper_cfg.model_size)
    transcript = transcribe.transcribe_audio(mixed_file, whisper_cfg)
    transcript_path = transcribe.save_transcript(transcript, session_dir)
    logger.info("Transcript saved to %s", transcript_path)

    if args.skip_summary:
        logger.info("Skipping summarization (--skip-summary).")
        print(transcript)
        return 0

    logger.info(
        "Summarizing via LiteLLM (model=%s, endpoint=%s)...",
        llm_cfg.model,
        llm_cfg.endpoint,
    )
    summary = summarize.summarize_transcript(transcript, llm_cfg)
    summary_path = summarize.save_summary(summary, session_dir)
    logger.info("Summary saved to %s", summary_path)

    print("\n" + summary + "\n")
    return 0


def cmd_status(args: argparse.Namespace, cfg: config_mod.AppConfig) -> int:
    session = state.load_session()
    if session is None:
        print("No active recording.")
        return 0

    running = audio.is_pid_running(session.pid)
    print(f"Session:   {session.session_id}")
    print(f"Status:    {'recording' if running else 'STALE (process not running)'}")
    print(f"PID:       {session.pid}")
    print(f"Started:   {session.started_at}")
    print(f"Mic(s):    {', '.join(session.mics)}")
    print(f"System:    {session.system_source}")
    print(f"Directory: {session.session_dir}")
    return 0 if running else 1


def cmd_list_devices(args: argparse.Namespace, cfg: config_mod.AppConfig) -> int:
    audio.check_dependencies()
    sources = audio.list_sources()
    default_source = audio.get_default_source()
    default_monitor = audio.get_default_sink_monitor()

    print("Microphones / input sources:")
    for src in sources:
        if src.is_monitor:
            continue
        marker = " (default)" if src.name == default_source else ""
        print(f"  [{src.index}] {src.name}{marker}")

    print("\nSystem audio (sink monitors -- captures whatever is playing):")
    for src in sources:
        if not src.is_monitor:
            continue
        marker = " (default sink's monitor)" if src.name == default_monitor else ""
        print(f"  [{src.index}] {src.name}{marker}")

    print(
        "\nUse '--mic <name>' (repeatable) and '--system-source <name>' with "
        "'meeting-recorder start' to override the defaults."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    try:
        cfg = config_mod.load_config(args)
        return args.func(args, cfg)
    except MeetingRecorderError as exc:
        logger.error(str(exc))
        return 1
    except KeyboardInterrupt:
        logger.error("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
