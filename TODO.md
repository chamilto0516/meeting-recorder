# Meeting Recorder Tasks

## Next

- [ ] Build a simple local GUI that runs through the project environment and
  can start, stop, monitor an active recording, name sessions, open outputs,
  and notify when processing is ready. Design the CLI/session lifecycle as its
  backend rather than duplicating recording logic in the GUI.
- [ ] Investigate Bluetooth mic/speaker acoustic echo and cross-talk. Determine
  whether PipeWire echo cancellation, source selection, or post-processing can
  improve transcripts when the mic hears meeting audio from the speakers.
- [ ] Make `--mic` easier to use: accept a close-match search or interactive
  smart selection instead of requiring a full PipeWire source name.
- [ ] Add a safe `cleanup` command for retention management: identify older
  sessions and reclaim disk space by removing bulky audio artifacts (`*.wav`,
  and related capture logs when appropriate) while preserving transcripts,
  summaries, session metadata, and capture-failure evidence. Include a dry-run,
  age/size filters, clear confirmation or an explicit force flag, and safeguards
  against deleting the active session or a session that still needs retrying.
- [ ] Exercise every packaged mode end to end with real audio, including a
  multi-hour game capture and representative lecture, CBT, journal, and meeting
  sessions. Review the resulting documents as well as capture validity.
- [ ] Decide whether a session should snapshot its mode manifest/prompt (or at
  least their version/hash) so a later `retry` remains reproducible after mode
  definitions change.

## Reliability and safety backlog

- [ ] Add comprehensive application-config validation errors and a `doctor`
  preflight command (mode-manifest YAML validation is already implemented).
- [x] Do not persist LLM API keys in session state; write state atomically with
  restrictive file permissions.
- [x] Verify the stored recording process identity before signalling it.
- [x] Retain session metadata until processing completes; support retrying
  transcription and summarization.
- [x] Harden FFmpeg capture and validate resulting tracks.
- [ ] Expand integration coverage and add automated lint/type checks plus
  reproducible dependency locking.

## Completed

- [x] Add optional human-readable, path-safe session names.
- [x] Make Markdown the default summary format.
- [x] Add a validated, data-driven mode registry in
  `meeting_recorder/modes/modes.yaml`, with packaged prompt files and declared
  output artifacts.
- [x] Provide mode-specific prompts for every implemented mode: meeting, game,
  lecture, computer-based training (CBT), and journal.
- [x] Support mic-and-system, system-only, and mic-only capture policies, with
  mode-specific microphone limits.
- [x] Give lecture and CBT modes their own study-oriented prompts and outputs
  instead of treating them as meetings.
- [x] Add tabletop-RPG outputs for a DM continuity brief, player recap, and
  combined archive; validate game mode with a successful real-world session.
- [x] Preserve mode-specific guidance through long-transcript map/reduce
  summarization.
