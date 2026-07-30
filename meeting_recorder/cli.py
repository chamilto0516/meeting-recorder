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
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from meeting_recorder import audio, config as config_mod, state, summarize, transcribe
from meeting_recorder.errors import CaptureValidationError, MeetingRecorderError

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

    retry_p = subparsers.add_parser(
        "retry", help="Resume transcription or summarization for the saved session"
    )
    retry_p.add_argument(
        "--skip-summary",
        action="store_true",
        help="Resume transcription but do not call the LLM for a summary",
    )
    _add_common_processing_args(retry_p)
    retry_p.set_defaults(func=cmd_retry)

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
    with state.session_lock():
        existing = state.load_session()
        if existing is not None:
            if existing.status != "recording":
                logger.error(
                    "Session %s is awaiting %s. Run 'meeting-recorder retry' "
                    "before starting another recording.",
                    existing.session_id,
                    "transcription" if existing.status == "recorded" else "summarization",
                )
            elif audio.verify_recording_process(existing.pid, existing.process):
                logger.error(
                    "A recording is already in progress (session %s, pid %s). "
                    "Run 'meeting-recorder stop' first.",
                    existing.session_id,
                    existing.pid,
                )
            else:
                logger.error(
                    "Existing session state cannot be verified safely; no process was "
                    "signaled and state was retained. Confirm the recorder is stopped, "
                    "then remove %s.",
                    state.state_file(),
                )
            return 1

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
            process=handle.process,
            started_at=datetime.now(timezone.utc).isoformat(),
            session_dir=str(session_dir),
            mixed_file=str(handle.mixed_file),
            track_files={k: str(v) for k, v in handle.track_files.items()},
            mics=mics,
            system_source=system_source,
            sample_rate=cfg.sample_rate,
            log_file=str(handle.log_file),
            whisper=cfg.whisper.to_dict(),
            llm=cfg.llm.to_session_dict(),
        )
        state.save_session(session)

        logger.info("Recording started (pid %s).", handle.pid)
        logger.info("Run 'meeting-recorder stop' when the meeting ends.")
        return 0


def cmd_stop(args: argparse.Namespace, cfg: config_mod.AppConfig) -> int:
    with state.session_lock():
        session = state.load_session()
        if session is None:
            logger.error("No active recording session found. Nothing to stop.")
            return 1
        if session.status != "recording":
            logger.error(
                "Session %s is already recorded; run 'meeting-recorder retry' to continue processing.",
                session.session_id,
            )
            return 1
        if not audio.verify_recording_process(session.pid, session.process):
            logger.error(
                "Session process cannot be verified safely; no signal was sent and "
                "state was retained. Confirm the recorder is stopped, then remove %s.",
                state.state_file(),
            )
            return 1

        logger.info("Stopping recording (pid %s)...", session.pid)
        was_running = audio.stop_recording(session.pid, session.process)
        if not was_running:
            logger.warning("Recording process exited before it could be stopped.")
        session.status = "recorded"
        session.ended_at = datetime.now(timezone.utc).isoformat()
        session.last_error = None
        state.save_session(session)

    logger.info("Recording saved. Tracks: %s", ", ".join(session.track_files.keys()))
    if args.skip_transcription:
        _validate_session_capture(session)
        logger.info("Processing is ready to resume with 'meeting-recorder retry'.")
        return 0
    return _process_session(session, args, cfg)


def _save_progress(session: state.Session) -> None:
    """Persist a processing transition without racing a second CLI invocation."""
    with state.session_lock():
        current = state.load_session()
        if current is None or current.session_id != session.session_id:
            raise MeetingRecorderError("Saved session changed while processing; refusing to overwrite it.")
        state.save_session(session)


def _record_processing_error(session: state.Session, exc: Exception) -> None:
    session.last_error = str(exc)
    _save_progress(session)


def _validate_session_capture(session: state.Session) -> None:
    """Persist a report for finalized audio and halt on an invalid capture."""
    track_files = {name: Path(path) for name, path in session.track_files.items()}
    session_dir = Path(session.session_dir)
    log_file = Path(session.log_file) if session.log_file else session_dir / "ffmpeg.log"
    report = audio.validate_capture(
        track_files, Path(session.mixed_file), session.sample_rate, log_file
    )
    report_path = audio.save_capture_validation(report, session_dir)
    session.capture_report = str(report_path)
    if not report.valid:
        exc = CaptureValidationError("Capture validation failed: " + "; ".join(report.errors))
        session.status = "capture_failed"
        _record_processing_error(session, exc)
        raise exc
    session.last_error = None
    _save_progress(session)


def _process_session(args_session: state.Session, args: argparse.Namespace, cfg: config_mod.AppConfig) -> int:
    """Resume the first incomplete stage of an already-stopped session."""
    session = args_session
    _validate_session_capture(session)
    mixed_file = Path(session.mixed_file)
    if not mixed_file.exists() or mixed_file.stat().st_size == 0:
        exc = MeetingRecorderError(f"No audio was captured at {mixed_file}; nothing to transcribe.")
        _record_processing_error(session, exc)
        raise exc

    whisper_cfg = config_mod.override_whisper(
        config_mod.WhisperConfig.from_dict(session.whisper), args
    )
    llm_cfg = replace(
        config_mod.LLMConfig.from_dict(session.llm), api_key=cfg.llm.api_key
    )
    llm_cfg = config_mod.override_llm(llm_cfg, args)
    session_dir = Path(session.session_dir)

    if session.status == "recorded":
        logger.info("Transcribing with faster-whisper (model=%s)...", whisper_cfg.model_size)
        try:
            transcript = transcribe.transcribe_audio(mixed_file, whisper_cfg)
            transcript_path = transcribe.save_transcript(transcript, session_dir)
        except Exception as exc:
            _record_processing_error(session, exc)
            raise
        session.status = "transcribed"
        session.transcript_file = str(transcript_path)
        session.last_error = None
        _save_progress(session)
        logger.info("Transcript saved to %s", transcript_path)

    if args.skip_summary:
        logger.info("Summary is ready to resume with 'meeting-recorder retry'.")
        return 0

    transcript_path = Path(session.transcript_file or session_dir / "transcript.txt")
    try:
        transcript = transcript_path.read_text(encoding="utf-8")
    except OSError as exc:
        _record_processing_error(session, exc)
        raise MeetingRecorderError(f"Could not read transcript at {transcript_path}: {exc}") from exc

    logger.info(
        "Summarizing via LiteLLM (model=%s, endpoint=%s)...",
        llm_cfg.model,
        llm_cfg.endpoint,
    )
    try:
        summary = summarize.summarize_transcript(transcript, llm_cfg)
        summary_path = summarize.save_summary(summary, session_dir)
    except Exception as exc:
        _record_processing_error(session, exc)
        raise
    session.summary_file = str(summary_path)
    session.last_error = None
    logger.info("Summary saved to %s", summary_path)
    print("\n" + summary + "\n")
    with state.session_lock():
        current = state.load_session()
        if current is None or current.session_id != session.session_id:
            raise MeetingRecorderError("Saved session changed while processing; refusing to clear it.")
        state.clear_session()
    return 0


def cmd_retry(args: argparse.Namespace, cfg: config_mod.AppConfig) -> int:
    with state.session_lock():
        session = state.load_session()
        if session is None:
            logger.error("No saved session needs processing.")
            return 1
        if session.status == "recording":
            logger.error("A recording is still active. Run 'meeting-recorder stop' first.")
            return 1
        if session.status == "capture_failed":
            logger.error(
                "Capture validation failed; inspect %s before starting a new recording.",
                session.capture_report or Path(session.session_dir) / "capture-validation.json",
            )
            return 1
    return _process_session(session, args, cfg)


def cmd_status(args: argparse.Namespace, cfg: config_mod.AppConfig) -> int:
    with state.session_lock():
        session = state.load_session()
        if session is None:
            print("No active recording.")
            return 0

        if session.status != "recording":
            print(f"Session:   {session.session_id}")
            if session.status == "capture_failed":
                print("Status:    capture failed (inspect capture-validation.json)")
            else:
                print(f"Status:    {session.status} (run 'meeting-recorder retry' to continue)")
            if session.last_error:
                print(f"Last error: {session.last_error}")
            print(f"Directory: {session.session_dir}")
            return 0

        verified = audio.verify_recording_process(session.pid, session.process)
        print(f"Session:   {session.session_id}")
        print(f"Status:    {'recording' if verified else 'UNVERIFIED OR STALE'}")
        print(f"PID:       {session.pid}")
        print(f"Started:   {session.started_at}")
        print(f"Mic(s):    {', '.join(session.mics)}")
        print(f"System:    {session.system_source}")
        print(f"Directory: {session.session_dir}")
        return 0 if verified else 1


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
