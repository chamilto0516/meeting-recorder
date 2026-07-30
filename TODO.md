# Meeting Recorder Tasks

## Next

- [ ] Add an optional meeting-name flag to `start`. Use a safe, human-readable
  name in the session-folder name while retaining a timestamp to prevent collisions.

## Reliability and safety backlog

- [ ] Add clear YAML/config validation errors and a `doctor` preflight command.
- [ ] Do not persist LLM API keys in session state; write state atomically with
  restrictive file permissions.
- [ ] Verify the stored recording process identity before signalling it.
- [ ] Retain session metadata until processing completes; support retrying
  transcription and summarization.
- [ ] Harden FFmpeg capture and validate resulting tracks.
- [ ] Add tests, lint/type checks, and reproducible dependency locking.
