# Meeting Recorder Tasks

## Next

- [ ] Build a simple local GUI that runs through the project environment and
  can start, stop, monitor an active recording, name sessions, open outputs,
  and notify when processing is ready. Design the CLI/session lifecycle as its
  backend rather than duplicating recording logic in the GUI.
- [x] Add an optional `--name` flag to `start` that creates a safe,
  human-readable timestamped session folder.
- [x] Add a system-audio-only lecture/class capture mode with no microphone.
- [ ] Give lecture mode its own summarization prompt and output structure,
  rather than treating a lecture like a business meeting.
- [x] Add a tabletop-RPG game mode with a dedicated long-session prompt that
  summarizes plot events, NPCs, locations, discoveries, unresolved hooks, and
  player goals. Test it against a roughly three-hour game session.
- [x] Make Markdown the default summary output format.
- [ ] Investigate Bluetooth mic/speaker acoustic echo and cross-talk. Determine
  whether PipeWire echo cancellation, source selection, or post-processing can
  improve transcripts when the mic hears meeting audio from the speakers.
- [ ] Add a lecture/class capture mode that records only system audio, with no
  microphone track.
- [ ] Make `--mic` easier to use: accept a close-match search or interactive
  smart selection instead of requiring a full PipeWire source name.

## Reliability and safety backlog

- [ ] Add clear YAML/config validation errors and a `doctor` preflight command.
- [x] Do not persist LLM API keys in session state; write state atomically with
  restrictive file permissions.
- [x] Verify the stored recording process identity before signalling it.
- [x] Retain session metadata until processing completes; support retrying
  transcription and summarization.
- [x] Harden FFmpeg capture and validate resulting tracks.
- [ ] Add tests, lint/type checks, and reproducible dependency locking.
