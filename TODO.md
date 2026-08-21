# Meeting Recorder Tasks

## Next

- [ ] Bluetooth echo: Investigate mic/speaker acoustic echo and cross-talk; determine whether PipeWire echo cancellation, source selection, or post-processing can improve transcripts when the mic hears meeting audio from the speakers.
- [x] Smart mic and output selection: `--mic`/`--system-source` accept live short selectors, aliases, and safe close matches; optional LLM help is explicit and validated.
- [ ] Session cleanup command: Add safe retention management that removes bulky audio artifacts and appropriate capture logs while preserving transcripts, summaries, metadata, and capture-failure evidence; include dry-run, age/size filters, explicit confirmation or force, and safeguards for active or retryable sessions.
- [ ] End-to-end mode tests: Exercise every packaged mode with real audio, including a multi-hour game capture and representative lecture, CBT, journal, and meeting sessions; review documents and capture validity.
- [ ] Mode prompt snapshots: Decide whether to snapshot the mode manifest/prompt, or at least its version/hash, so a later `retry` remains reproducible after definitions change.
- [ ] Separate player prompt: Explore extracting the game-mode player recap into its own externally editable prompt while preserving the mode system’s combined game output.
- [ ] Recording status indicator: Detect an active recording and show a slow gray-to-red flashing status-bar indicator; offer a right-click `Stop` action that runs the parameterless stop command.

## Reliability and safety backlog

- [ ] Config doctor: Add comprehensive application-config validation errors and a `doctor` preflight command; mode-manifest YAML validation already exists.
- [x] Do not persist LLM API keys in session state; write state atomically with
  restrictive file permissions.
- [x] Verify the stored recording process identity before signalling it.
- [x] Retain session metadata until processing completes; support retrying
  transcription and summarization.
- [x] Harden FFmpeg capture and validate resulting tracks.
- [ ] Test and release tooling: Expand integration coverage and add automated lint/type checks plus reproducible dependency locking.

## Completed

- [x] Build a local GUI that uses the CLI/session lifecycle to start, stop, and monitor recordings, name sessions, open outputs, and report when processing is ready.
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
- [x] Replace lossy long-transcript map/reduce summarization with one
  full-context LLM call per mode, including strict multi-artifact parsing.
