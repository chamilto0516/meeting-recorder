# Meeting Recorder Tasks

## Next

- [ ] Add an optional meeting-name flag to `start`. Use a safe, human-readable
  name in the session-folder name while retaining a timestamp to prevent collisions.
- [ ] Make Markdown the default summary output format (likely a summarization
  prompt/config default change).
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
