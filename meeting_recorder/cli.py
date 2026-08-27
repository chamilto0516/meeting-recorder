"""argparse entry point for meeting-recorder.

    meeting-recorder start   [options]   # begin background recording
    meeting-recorder stop    [options]   # stop, transcribe, summarize
    meeting-recorder reprocess SESSION   # regenerate a completed session
    meeting-recorder cleanup             # preview/delete expired audio
    meeting-recorder status              # is a recording currently running?
    meeting-recorder list-devices        # show available PipeWire/Pulse sources
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from meeting_recorder import (
    audio,
    cleanup as cleanup_mod,
    config as config_mod,
    device_selection,
    modes,
    session_archive,
    state,
    summarize,
    transcribe,
)
from meeting_recorder.errors import CaptureValidationError, ConfigError, MeetingRecorderError

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
        epilog="Use 'meeting-recorder COMMAND --help' to see options for a command, "
        "for example 'meeting-recorder start --help'.",
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
        "--device-config",
        type=Path,
        help="Path to the device-alias YAML file (default: ~/.config/meeting-recorder/devices.yaml)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    start_p = subparsers.add_parser(
        "start",
        help="Start a background recording (use 'start --help' for recording options)",
    )
    start_p.add_argument(
        "--mode", default="meeting", metavar="MODE",
        help="Recording mode (default: meeting; available modes are read from the packaged registry)",
    )
    start_p.add_argument(
        "--allow-llm-device-selection",
        action="store_true",
        help="Allow the configured LLM to resolve an otherwise unmatched mic/output hint; sends only device names.",
    )
    start_p.add_argument(
        "--mic",
        action="append",
        dest="mics",
        metavar="SOURCE",
        help=(
            "Microphone source selector: m1/m2, alias, close match, or exact "
            "PipeWire/Pulse name. Repeat to record multiple mics. Defaults to "
            "the system default input source; run 'meeting-recorder list-devices'."
        ),
    )
    start_p.add_argument(
        "--system-source",
        dest="system_source",
        metavar="SOURCE",
        help=(
            "Output-monitor selector: o1/o2, alias, close match, or exact "
            "PipeWire/Pulse name. Defaults to the default sink's monitor, i.e. "
            "'whatever is currently playing out loud'."
        ),
    )
    start_p.add_argument(
        "--sample-rate", type=int, help="Recording sample rate in Hz (default: 48000)"
    )
    start_p.add_argument(
        "--name", "--meeting-name",
        dest="meeting_name",
        metavar="NAME",
        help="Optional meeting name appended safely to the timestamped session folder",
    )
    start_p.add_argument(
        "--lecture", "--lecture-mode",
        dest="lecture",
        action="store_true",
        help="Deprecated alias for '--mode lecture'",
    )
    start_p.add_argument(
        "--game",
        action="store_true",
        help="Deprecated alias for '--mode game'",
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

    reprocess_p = subparsers.add_parser(
        "reprocess", help="Regenerate documents for a completed session"
    )
    reprocess_p.add_argument("session", metavar="SESSION_ID", help="Exact session directory name")
    reprocess_p.add_argument(
        "--retranscribe",
        action="store_true",
        help="Recreate transcript.txt from mixed.wav before regenerating documents",
    )
    reprocess_p.add_argument(
        "--mode",
        metavar="MODE",
        help="Override the recorded mode (required for legacy sessions without metadata)",
    )
    reprocess_p.add_argument(
        "--use-saved-prompt",
        action="store_true",
        help="Use the prompt snapshot recorded with the session instead of the current mode prompt",
    )
    _add_common_processing_args(reprocess_p)
    reprocess_p.set_defaults(func=cmd_reprocess)

    cleanup_p = subparsers.add_parser(
        "cleanup", help="Preview and remove expired session audio"
    )
    cleanup_p.add_argument("--dry-run", action="store_true", help="Print the plan without prompting or deleting")
    cleanup_p.add_argument("--yes", action="store_true", help="Delete without an interactive confirmation")
    cleanup_p.add_argument("--raw-audio-days", type=int, help="Override raw-track retention days")
    cleanup_p.add_argument("--mixed-audio-days", type=int, help="Override mixed-track retention days")
    cleanup_p.set_defaults(func=cmd_cleanup)

    status_p = subparsers.add_parser("status", help="Show whether a recording is active")
    status_p.set_defaults(func=cmd_status)

    list_p = subparsers.add_parser(
        "list-devices", help="List available PipeWire/Pulse audio sources"
    )
    list_p.set_defaults(func=cmd_list_devices)

    devices_p = subparsers.add_parser(
        "devices", help="Manage the user-editable device alias file"
    )
    devices_subparsers = devices_p.add_subparsers(dest="devices_command", required=True)
    init_p = devices_subparsers.add_parser(
        "init", help="Create an editable alias file from currently connected devices"
    )
    init_p.add_argument(
        "--force", action="store_true", help="Replace an existing device alias file"
    )
    init_p.set_defaults(func=cmd_devices_init)

    modes_p = subparsers.add_parser(
        "list-modes", help="List available recording modes"
    )
    modes_p.set_defaults(func=cmd_list_modes)

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

        legacy_modes = [name for name, selected in (("lecture", args.lecture), ("game", args.game)) if selected]
        if len(legacy_modes) > 1:
            logger.error("--lecture and --game cannot be combined.")
            return 1
        if legacy_modes and args.mode != "meeting":
            logger.error("Deprecated mode flags cannot be combined with --mode.")
            return 1
        mode_name = legacy_modes[0] if legacy_modes else args.mode
        if legacy_modes:
            logger.warning("--%s is deprecated; use '--mode %s'.", mode_name, mode_name)
        try:
            mode = modes.get_mode(mode_name)
        except MeetingRecorderError as exc:
            logger.error(str(exc))
            return 1
        if mode.capture == "system-only" and args.mics:
            logger.error("--mode %s records system audio only and cannot be combined with --mic.", mode.name)
            return 1
        requested_mics = [] if mode.capture == "system-only" else (args.mics or [audio.get_default_source()])
        if len(requested_mics) < mode.min_mics or (mode.max_mics is not None and len(requested_mics) > mode.max_mics):
            maximum = str(mode.max_mics) if mode.max_mics is not None else "any number of"
            logger.error("--mode %s requires %s to %s microphone(s).", mode.name, mode.min_mics, maximum)
            return 1

        audio.check_dependencies()
        candidates = device_selection.discover(cfg.device_aliases)
        mics = [
            device_selection.resolve(
                selector, "mic", candidates, cfg.device_aliases,
                allow_llm=getattr(args, "allow_llm_device_selection", False), llm=cfg.llm,
            ).source
            for selector in requested_mics
        ]
        if mode.capture == "mic-only":
            if args.system_source:
                logger.error("--mode %s records microphone audio only and cannot use --system-source.", mode.name)
                return 1
            system_source = None
        else:
            requested_system = args.system_source or audio.get_default_sink_monitor()
            system_source = device_selection.resolve(
                requested_system, "output", candidates, cfg.device_aliases,
                allow_llm=getattr(args, "allow_llm_device_selection", False), llm=cfg.llm,
            ).source
        session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        if getattr(args, "meeting_name", None):
            session_id += "_" + state.sanitize_session_name(args.meeting_name)
        session_dir = cfg.data_dir / session_id

        logger.info("Mode: %s", mode.name)
        logger.info("Mic source(s): %s", ", ".join(mics) if mics else "none")
        logger.info("System audio source: %s", system_source or "none")
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
            meeting_name=getattr(args, "meeting_name", None),
            mode=mode.name,
            whisper=cfg.whisper.to_dict(),
            llm=cfg.llm.to_session_dict(),
        )
        state.save_session(session)
        session_archive.save_manifest(session, mode=mode)

        logger.info("Recording started (pid %s).", handle.pid)
        logger.info("Run 'meeting-recorder stop' when the meeting ends.")
        return 0


def cmd_stop(args: argparse.Namespace, cfg: config_mod.AppConfig, progress_callback=None) -> int:
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
            if session.log_file:
                audio.log_recording_event(
                    Path(session.log_file),
                    f"stop requested but recording process could not be verified (pid={session.pid})",
                )
            logger.error(
                "Session process cannot be verified safely; no signal was sent and "
                "state was retained. Confirm the recorder is stopped, then remove %s.",
                state.state_file(),
            )
            return 1

        logger.info("Stopping recording (pid %s)...", session.pid)
        if session.log_file:
            audio.log_recording_event(Path(session.log_file), f"stop requested (pid={session.pid})")
        was_running = audio.stop_recording(session.pid, session.process)
        if not was_running:
            if session.log_file:
                audio.log_recording_event(
                    Path(session.log_file),
                    f"recording process was already gone when stop was requested (pid={session.pid})",
                )
            logger.warning("Recording process exited before it could be stopped.")
        elif session.log_file:
            audio.log_recording_event(Path(session.log_file), f"stop completed (pid={session.pid})")
        session.status = "recorded"
        session.ended_at = datetime.now(timezone.utc).isoformat()
        session.last_error = None
        state.save_session(session)
        session_archive.save_manifest(session)

    logger.info("Recording saved. Tracks: %s", ", ".join(session.track_files.keys()))
    if args.skip_transcription:
        _validate_session_capture(session)
        logger.info("Processing is ready to resume with 'meeting-recorder retry'.")
        return 0
    return _process_session(session, args, cfg, progress_callback=progress_callback)


def _save_progress(session: state.Session) -> None:
    """Persist a processing transition without racing a second CLI invocation."""
    with state.session_lock():
        current = state.load_session()
        if current is None or current.session_id != session.session_id:
            raise MeetingRecorderError("Saved session changed while processing; refusing to overwrite it.")
        state.save_session(session)
        session_archive.save_manifest(session)


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


def _process_session(
    args_session: state.Session,
    args: argparse.Namespace,
    cfg: config_mod.AppConfig,
    progress_callback=None,
) -> int:
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
        if progress_callback:
            progress_callback("transcribing", 0)
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
        if progress_callback and session.status == "transcribed":
            progress_callback("summarizing", 50)
        transcript = transcript_path.read_text(encoding="utf-8")
    except OSError as exc:
        _record_processing_error(session, exc)
        raise MeetingRecorderError(f"Could not read transcript at {transcript_path}: {exc}") from exc

    logger.info("Summarizing via LiteLLM (model=%s, endpoint=%s)...", llm_cfg.model, llm_cfg.endpoint)
    try:
        mode = modes.get_mode(session.mode)
        summaries = summarize.summarize_mode(transcript, llm_cfg, mode)
        output_paths = summarize.save_mode_summaries(summaries, session_dir, mode)
        summary_path = output_paths[-1]
        summary = summaries[0][1]
    except Exception as exc:
        _record_processing_error(session, exc)
        raise
    session.summary_file = str(summary_path)
    session.last_error = None
    for output_path in output_paths:
        logger.info("Mode output saved to %s", output_path)
    print("\n" + summary + "\n")
    with state.session_lock():
        current = state.load_session()
        if current is None or current.session_id != session.session_id:
            raise MeetingRecorderError("Saved session changed while processing; refusing to clear it.")
        session_archive.save_manifest(session, status="completed")
        state.clear_session()
    if progress_callback:
        progress_callback("done", 100)
    return 0


def cmd_retry(args: argparse.Namespace, cfg: config_mod.AppConfig, progress_callback=None) -> int:
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
    return _process_session(session, args, cfg, progress_callback=progress_callback)


def _safe_manifest_file(session_dir: Path, value: object, default: str) -> Path:
    name = value if isinstance(value, str) and value else default
    if Path(name).name != name:
        raise MeetingRecorderError(f"Unsafe artifact path in {session_archive.manifest_path(session_dir)}.")
    return session_dir / name


def _resolve_completed_session(args: argparse.Namespace, cfg: config_mod.AppConfig) -> tuple[state.Session, dict | None]:
    if Path(args.session).name != args.session or args.session in {".", ".."}:
        raise MeetingRecorderError("SESSION_ID must be an exact session directory name.")
    session_dir = cfg.data_dir / args.session
    if not session_dir.is_dir() or session_dir.is_symlink():
        raise MeetingRecorderError(f"Session directory was not found: {session_dir}")
    manifest = session_archive.load_manifest(session_dir)
    if manifest is None:
        if not args.mode:
            raise MeetingRecorderError(
                "This legacy session has no session.json; specify --mode MODE to reprocess it."
            )
        return state.Session(
            session_id=args.session,
            pid=0,
            process=None,
            started_at="",
            ended_at=None,
            session_dir=str(session_dir),
            mixed_file=str(session_dir / "mixed.wav"),
            track_files={},
            mics=[],
            system_source=None,
            sample_rate=cfg.sample_rate,
            mode=args.mode,
            whisper=cfg.whisper.to_dict(),
            llm=cfg.llm.to_session_dict(),
            status="transcribed",
            transcript_file=str(session_dir / "transcript.txt"),
        ), None
    saved = manifest["session"]
    audio_info = saved.get("audio", {})
    if not isinstance(audio_info, dict):
        raise MeetingRecorderError("Saved session audio metadata is malformed.")
    mode_name = args.mode or saved.get("mode")
    if not isinstance(mode_name, str):
        raise MeetingRecorderError("Saved session mode metadata is missing.")
    return state.Session(
        session_id=saved.get("session_id", args.session),
        pid=0,
        process=None,
        started_at=saved.get("started_at", ""),
        ended_at=saved.get("ended_at"),
        session_dir=str(session_dir),
        mixed_file=str(_safe_manifest_file(session_dir, audio_info.get("mixed"), "mixed.wav")),
        track_files={},
        mics=list(saved.get("mics", [])),
        system_source=saved.get("system_source"),
        sample_rate=saved.get("sample_rate", cfg.sample_rate),
        log_file=str(_safe_manifest_file(session_dir, saved.get("log_file"), "ffmpeg.log")),
        meeting_name=saved.get("meeting_name"),
        mode=mode_name,
        whisper=dict(saved.get("whisper", {})),
        llm=dict(saved.get("llm", {})),
        status="transcribed",
        transcript_file=str(_safe_manifest_file(session_dir, saved.get("transcript_file"), "transcript.txt")),
        summary_file=saved.get("summary_file"),
        capture_report=saved.get("capture_report"),
    ), manifest


def _backup_reprocess_inputs(session_dir: Path, filenames: set[str]) -> Path | None:
    existing = [
        session_dir / name
        for name in sorted(filenames)
        if (session_dir / name).is_file() and not (session_dir / name).is_symlink()
    ]
    if not existing:
        return None
    history_root = session_dir / ".reprocess-history"
    if history_root.is_symlink() or (history_root.exists() and not history_root.is_dir()):
        raise MeetingRecorderError(f"Reprocess history path is unsafe: {history_root}")
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    destination = history_root / stamp
    destination.mkdir(parents=True, exist_ok=False)
    for path in existing:
        shutil.copy2(path, destination / path.name)
    return destination


def cmd_reprocess(args: argparse.Namespace, cfg: config_mod.AppConfig) -> int:
    with state.session_lock():
        session, manifest = _resolve_completed_session(args, cfg)
        current = state.load_session()
        if current is not None and Path(current.session_dir) == Path(session.session_dir):
            raise MeetingRecorderError(
                "This session is active or retryable; use stop/retry before reprocessing it."
            )

    if args.use_saved_prompt:
        snapshot = manifest.get("mode_snapshot") if manifest else None
        if not isinstance(snapshot, dict):
            raise MeetingRecorderError("This session has no saved mode prompt.")
        mode = modes.mode_from_snapshot(snapshot)
        if args.mode and args.mode != mode.name:
            raise MeetingRecorderError("--mode cannot be combined with a different saved prompt.")
    else:
        mode = modes.get_mode(session.mode)

    session_dir = Path(session.session_dir)
    transcript_path = Path(session.transcript_file or session_dir / "transcript.txt")
    if transcript_path.is_symlink():
        raise MeetingRecorderError(f"Refusing to follow transcript symlink: {transcript_path}")
    whisper_cfg = config_mod.override_whisper(
        config_mod.WhisperConfig.from_dict(session.whisper), args
    )
    llm_cfg = config_mod.override_llm(
        replace(config_mod.LLMConfig.from_dict(session.llm), api_key=cfg.llm.api_key), args
    )
    if args.retranscribe:
        mixed_file = Path(session.mixed_file)
        if not mixed_file.is_file() or mixed_file.is_symlink() or mixed_file.stat().st_size == 0:
            raise MeetingRecorderError(
                f"Cannot retranscribe: mixed audio is missing or expired under the cleanup policy: {mixed_file}"
            )
        logger.info("Retranscribing %s with faster-whisper (model=%s)...", session.session_id, whisper_cfg.model_size)
        transcript = transcribe.transcribe_audio(mixed_file, whisper_cfg)
    else:
        try:
            transcript = transcript_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MeetingRecorderError(f"Could not read transcript at {transcript_path}: {exc}") from exc
    logger.info("Regenerating %s documents with the %s prompt...", session.session_id, mode.name)
    summaries = summarize.summarize_mode(transcript, llm_cfg, mode)
    backup_names = {"transcript.txt"} if args.retranscribe else set()
    backup_names.update(artifact.filename for artifact in mode.artifacts)
    unsafe_outputs = [session_dir / name for name in backup_names if (session_dir / name).is_symlink()]
    if unsafe_outputs:
        raise MeetingRecorderError(f"Refusing to replace output symlink: {unsafe_outputs[0]}")
    history_dir = _backup_reprocess_inputs(session_dir, backup_names)
    if args.retranscribe:
        transcript_path = transcribe.save_transcript(transcript, session_dir)
    output_paths = summarize.save_mode_summaries(summaries, session_dir, mode)
    session.mode = mode.name
    session.transcript_file = str(transcript_path)
    session.summary_file = str(output_paths[-1])
    run = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "retranscribed": bool(args.retranscribe),
        "prompt_source": "saved" if args.use_saved_prompt else "current",
        "mode": mode.name,
        "history_dir": str(history_dir.relative_to(session_dir)) if history_dir else None,
        "outputs": [path.name for path in output_paths],
    }
    session_archive.save_manifest(session, mode=mode, status="completed", reprocess_run=run)
    print(f"Reprocessed {session.session_id}.")
    for output_path in output_paths:
        print(f"  {output_path}")
    if history_dir:
        print(f"Previous outputs: {history_dir}")
    return 0


def cmd_cleanup(args: argparse.Namespace, cfg: config_mod.AppConfig) -> int:
    plan = cleanup_mod.build_plan(cfg.data_dir, cfg.cleanup)
    print(cleanup_mod.render_plan(plan, verbose=args.verbose))
    if args.dry_run or not plan.candidates:
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            logger.error("Cleanup requires an interactive terminal or --yes; nothing was deleted.")
            return 1
        answer = input("\nContinue? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cleanup cancelled.")
            return 0
    with state.session_lock():
        confirmed = cleanup_mod.build_plan(cfg.data_dir, cfg.cleanup)
        if confirmed.signature() != plan.signature():
            logger.error("Cleanup plan changed after preview; nothing was deleted. Run cleanup again.")
            return 1
        result = cleanup_mod.execute_plan(confirmed)
    print(cleanup_mod.render_result(result))
    return 0 if not result.failures else 1


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
        if session.meeting_name:
            print(f"Name:      {session.meeting_name}")
        print(f"Mode:      {session.mode}")
        print(f"Mic(s):    {', '.join(session.mics) if session.mics else 'none'}")
        print(f"System:    {session.system_source or 'none'}")
        print(f"Directory: {session.session_dir}")
        return 0 if verified else 1


def cmd_list_devices(args: argparse.Namespace, cfg: config_mod.AppConfig) -> int:
    candidates = device_selection.discover(cfg.device_aliases)

    def show(kind: str, title: str) -> None:
        print(title)
        for candidate in candidates:
            if candidate.kind != kind:
                continue
            marker = " (default)" if candidate.is_default else ""
            aliases = f"; aliases: {', '.join(candidate.aliases)}" if candidate.aliases else ""
            print(f"  [{candidate.token}] {candidate.label}{marker}{aliases}")
            print(f"       {candidate.source}")

    show("mic", "Microphones / input sources:")
    print()
    show("output", "System audio (sink monitors -- captures whatever is playing):")
    available = {candidate.source for candidate in candidates}
    unavailable = [(alias, source) for alias, source in cfg.device_aliases.items() if source not in available]
    if unavailable:
        print("\nSaved aliases not currently available:")
        for alias, source in unavailable:
            print(f"  {alias}: {source}")
    print(
        "\nUse '--mic m1' (repeatable) and '--system-source o1' with "
        "'meeting-recorder start'. Exact source IDs and aliases remain supported."
    )
    return 0


def cmd_devices_init(args: argparse.Namespace, cfg: config_mod.AppConfig) -> int:
    """Create the separate alias file without changing primary application config."""
    path = cfg.device_config
    if path is None:  # Defensive: AppConfig always supplies a resolved default.
        raise ConfigError("No device config path was resolved.")
    if path.exists() and not args.force:
        logger.error("Device config already exists: %s. Re-run with --force to replace it.", path)
        return 1
    candidates = device_selection.discover({})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(device_selection.render_device_config(candidates), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    print(f"Created device alias config: {path}")
    print("Edit the alias names, then run 'meeting-recorder list-devices' to verify them.")
    return 0


def _mode_capture_label(capture: str) -> str:
    return {
        "mic-and-system": "mic + system",
        "system-only": "system only",
        "mic-only": "mic only",
    }[capture]


def _mode_mic_label(mode: modes.ModeDefinition) -> str:
    if mode.max_mics is None:
        return f"{mode.min_mics}+"
    if mode.min_mics == mode.max_mics:
        return str(mode.min_mics)
    return f"{mode.min_mics}-{mode.max_mics}"


def cmd_list_modes(args: argparse.Namespace, cfg: config_mod.AppConfig) -> int:
    print("Available recording modes:\n")
    print(f"{'MODE':<8} {'CAPTURE':<15} {'MICS':<4} DESCRIPTION")
    for name in sorted(modes.load_modes()):
        mode = modes.get_mode(name)
        print(
            f"{mode.name:<8} {_mode_capture_label(mode.capture):<15} "
            f"{_mode_mic_label(mode):<4} {mode.description}"
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
