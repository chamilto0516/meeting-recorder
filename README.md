# meeting-recorder

A local, privacy-friendly meeting recorder for Linux Mint (and other
PipeWire-based Linux desktops). It records your microphone(s) and whatever
audio is playing out loud (browser tab, Zoom, Google Meet, ...), then
transcribes the recording with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
and summarizes the transcript with a local LLM through
[LiteLLM](https://github.com/BerriAI/litellm) (Ollama by default).

Everything runs locally: no audio or transcript ever has to leave your
machine unless you point the LLM step at a remote endpoint yourself.

## How it works

```
meeting-recorder start                 meeting-recorder stop
        |                                       |
        v                                       v
 pick mic(s) + system                stop ffmpeg (SIGINT,
 audio monitor via                   flush/finalize files)
 pactl, launch a                              |
 detached ffmpeg                              v
 process that records                faster-whisper transcribes
 each track + a mixed-               the mixed-down track
 down track, remembers                        |
 the pid/paths in a                           v
 small state file                    LiteLLM sends the transcript
                                      to your LLM endpoint (Ollama
                                      by default) for a structured
                                      summary
```

* **Capture**: PipeWire on Linux Mint runs a `pipewire-pulse` compatibility
  layer that speaks the standard PulseAudio protocol. `pactl` is used to
  discover devices, and FFmpeg's `-f pulse` input is used to record from
  them -- no need for FFmpeg to be built with native PipeWire support.
* **System audio** is captured from the *monitor* of your default output
  sink -- i.e. "everything currently being played out loud". This means it
  transparently captures a browser tab, Zoom, Google Meet, or anything else
  making sound, with no per-app configuration.
* **Microphones**: you can record one or several mics as separate tracks
  (handy if a couple of people are in the room on different mics). Each mic
  and the system audio are saved as individual `.wav` files *and* mixed down
  into a single `mixed.wav` used for transcription.
* **`start` is fire-and-forget**: FFmpeg runs detached in its own session, so
  it keeps recording after the CLI invocation that started it exits. `stop`
  looks up the running process via a small state file and signals it to shut
  down cleanly (so the WAV headers are finalized correctly).
* **Summarization goes through LiteLLM**, so the exact same code path works
  whether you're pointing at a local Ollama server (the default:
  `ollama/llama3.1` @ `http://localhost:11434`) or any other LiteLLM-backed
  provider/proxy/model. Swap `endpoint` / `model` / `api_key` and you're done.

## Requirements

* Linux Mint (or another PipeWire-based distro) with `pipewire-pulse` running
  (this is the default on modern Mint/Ubuntu/Fedora).
* System packages:

  ```bash
  sudo apt install ffmpeg pulseaudio-utils
  ```

  (`pulseaudio-utils` provides `pactl`, which talks to PipeWire's PulseAudio
  compatibility layer.)
* Python 3.9+.
* [Ollama](https://ollama.com/) running locally with a model pulled, e.g.:

  ```bash
  ollama pull llama3.1
  ```

  (or any other LiteLLM-supported endpoint -- see [Configuring the summarization backend](#configuring-the-summarization-backend)).

## Installation

```bash
cd meeting-recorder
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `meeting-recorder` console command (also runnable as
`python -m meeting_recorder`).

The first `stop` run will download the requested faster-whisper model from
Hugging Face (cached locally afterwards).

## Usage

### 1. See what devices are available

```bash
meeting-recorder list-devices
```

```
Microphones / input sources:
  [m1] usb Blue Microphones Yeti 00 (default)
       alsa_input.usb-Blue_Microphones_Yeti-00
  [m2] pci 0000 00 1f 3 analog stereo
       alsa_input.pci-0000_00_1f.3.analog-stereo

System audio (sink monitors -- captures whatever is playing):
  [o1] pci 0000 00 1f 3 analog stereo (default)
       alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
```

### 2. Start recording

```bash
# simplest: default mic + default system output
meeting-recorder start

# multiple mics (e.g. two people, two headsets)
meeting-recorder start --mic alsa_input.usb-Blue_Microphones_Yeti-00 \
                        --mic alsa_input.usb-Another_Mic-00

# compact selectors from list-devices; m1/o1 are live, current-device numbers
meeting-recorder start --mic m1 --system-source o1

# pin a specific system audio source instead of the default sink's monitor
meeting-recorder start --system-source alsa_output.usb-Headset.monitor

# label a session; its folder becomes e.g. 2026-07-30_13-38-44_First-AI-Class-Lecture
meeting-recorder start --name "First: AI Class(Lecture"

# record a lecture/class or CBT with system audio only (no local microphone)
meeting-recorder start --mode lecture
meeting-recorder start --mode cbt

# record a tabletop-RPG game: one DM mic plus player/system audio
meeting-recorder start --mode game --name "The Ruins of Asterfall"

# record as a player: your mic plus call/VTT audio, with optional private context
meeting-recorder start --mode game-player \
  --player-context examples/game-player-context.md \
  --name "The Ruins of Asterfall"

# record spoken thoughts from the microphone only
meeting-recorder start --mode journal --name "Sunday reflection"
```

Join your Zoom/Meet/browser call as usual -- capture is independent of which
app is making the sound.

### 3. Stop, transcribe, and summarize

```bash
meeting-recorder stop
```

This stops FFmpeg, runs faster-whisper on the mixed-down recording, and
sends the transcript to your configured LLM for summarization. Both the
transcript and summary are saved next to the audio files, and the summary is
printed to stdout.

Useful flags:

```bash
meeting-recorder stop --skip-summary          # only transcribe
meeting-recorder stop --skip-transcription    # only stop the recording
meeting-recorder stop --llm-model ollama/mistral --llm-endpoint http://localhost:11434
```

### Retry interrupted processing

If transcription or summarization fails, the recording session is retained and
`status` shows the completed stage plus the last error. Fix the underlying
issue (for example, start Ollama or choose a different model), then resume:

```bash
meeting-recorder retry
```

`retry` starts at the first unfinished stage: it does not re-transcribe when a
valid transcript was already saved. While a session is awaiting processing,
finish it with `retry` before starting a new recording.

### Reprocess a completed session

Completed sessions can regenerate their documents from the saved transcript
using the mode's current packaged prompt. This is useful after improving a
standard mode prompt and does not require retained audio:

```bash
meeting-recorder reprocess 2026-08-20_14-30-00_Project-Review
```

Add `--retranscribe` to recreate `transcript.txt` from the retained
`mixed.wav` before summarizing, for example after changing Whisper settings:

```bash
meeting-recorder reprocess 2026-08-20_14-30-00_Project-Review \
  --retranscribe --whisper-model small
```

The prior transcript and generated documents are copied to the session's
`.reprocess-history/<timestamp>/` directory before replacement. New sessions
also store the original mode definition and prompt in `session.json`; pass
`--use-saved-prompt` to reproduce that prompt instead of using the current
one. For a legacy session without `session.json`, specify `--mode MODE`.

### Clean up retained audio

`cleanup` scans all session directories and prints a size-aware deletion plan.
By default it keeps every recorder-owned WAV file for 7 days, keeps only
`mixed.wav` from day 7 through day 14, and removes recorder-owned WAV files
after day 14. Transcripts, summaries, metadata, capture-validation reports,
failed captures, and active or retryable sessions are left untouched.

```bash
meeting-recorder cleanup --dry-run  # report only
meeting-recorder cleanup            # report, then ask [y/N]
meeting-recorder cleanup --yes      # report and run non-interactively
```

The command never follows symlinks or deletes unexpected WAV filenames. It
rescans after confirmation and refuses to continue if the plan changed. For
completed, successfully validated sessions, `ffmpeg.log` expires with
`mixed.wav`; failed-capture logs are retained.

### Tabletop-RPG game mode

`--mode game` records one DM microphone and the player/system-audio track, then
uses the game continuity prompt rather than the meeting-summary prompt. It
creates `dm-continuity-brief.md`, `player-recap.md`, and an archival combined
`game-summary.md`. The transcript remains mixed deliberately: the prompt does
not trust speaker diarization when audio overlaps or a microphone hears output.

### Tabletop-RPG player mode

`--mode game-player` is a separate, player-focused online-play mode. It records
one local microphone plus system audio (normally your headset mic and the
voice-call/VTT output), then creates a shareable `player-recap.md` and private
`character-notebook.md` and `context-updates.md` files. It is not an in-person
table-capture mode: system audio is required and only one microphone is allowed.

Pass optional character and campaign reference material with `--player-context
PATH`. The file must be UTF-8 Markdown; an adaptable starting template is in
[`examples/game-player-context.md`](examples/game-player-context.md). The
recorder copies its exact contents to a private `player-context.md` snapshot in
the session so retries and normal reprocessing use the same context. To
regenerate an old session with revised notes, use:

```bash
meeting-recorder reprocess SESSION_ID --player-context updated-character.md
```

That explicit override is saved privately with that reprocessing run and does
not replace the original snapshot. Player context, private notebook, and context
updates may contain character secrets; they are written owner-readable only.
They are included in the same summarization request sent to your configured LLM,
so use a local endpoint or provider you trust with that information. The
shareable recap is instructed never to reveal unrevealed personal secrets.

### Recording modes

`start --mode MODE` selects a packaged mode and defaults to `meeting`. The
built-in modes are `meeting` (mic + system audio), `game` (one mic + system
audio), `game-player` (one mic + system), `lecture` and `cbt` (system audio
only), and `journal` (one mic only).
Their capture rules, output files, and prompt assets are defined in
`meeting_recorder/modes/modes.yaml`; adding a manifest entry and its `.md`
prompt makes another mode selectable. `--game` and `--lecture` remain
deprecated aliases for compatibility.

| Mode | Capture | Output |
| --- | --- | --- |
| `meeting` | one or more mics + system | `summary.md` |
| `game` | one mic + system | DM brief, player recap, and `game-summary.md` |
| `game-player` | one mic + system | shareable recap, private character notebook, private context updates |
| `lecture` | system only | `lecture-notes.md` |
| `cbt` | system only | `cbt-notes.md` |
| `journal` | one mic only | `journal.md` |

### Other commands

```bash
meeting-recorder status    # recording status or a retryable processing stage
meeting-recorder retry     # resume a saved transcription/summarization
meeting-recorder reprocess SESSION_ID  # regenerate a completed session
meeting-recorder cleanup --dry-run     # preview expired audio
```

## Where files go

Each session gets its own timestamped directory (default:
`~/.local/share/meeting-recorder/sessions/<timestamp>/`):

```
mic0.wav          # raw mic 1 track
mic1.wav          # raw mic 2 track (if a second --mic was given)
system.wav        # raw system audio track
mixed.wav         # mic(s) + system mixed down, used for transcription
ffmpeg.log        # ffmpeg's own log for that session
capture-validation.json  # per-track WAV format/duration validation report
transcript.txt    # faster-whisper output
summary.md or mode-specific Markdown outputs
player-context.md  # private character/campaign snapshot for game-player sessions
session.json      # durable non-secret metadata and original mode/prompt snapshot
.reprocess-history/  # previous outputs retained when a session is reprocessed
```

Override the base directory with `--data-dir` or `MEETING_RECORDER_DATA_DIR`.
The optional `--name` (or `--meeting-name`) is saved as the meeting display
name and converted to a safe, readable folder suffix: punctuation and path
characters become hyphens, repeated hyphens are collapsed, and the timestamp
remains first to prevent collisions.

## Configuration

Settings can be set in three layers, from lowest to highest precedence:

1. Built-in defaults
2. A YAML config file (default `~/.config/meeting-recorder/config.yaml`,
   see [`config.example.yaml`](./config.example.yaml) for every available
   key)
3. Environment variables (`MEETING_RECORDER_LLM_MODEL`,
   `MEETING_RECORDER_LLM_ENDPOINT`, `MEETING_RECORDER_LLM_API_KEY`,
   `MEETING_RECORDER_WHISPER_MODEL`, `MEETING_RECORDER_WHISPER_DEVICE`,
   `MEETING_RECORDER_WHISPER_COMPUTE_TYPE`, `MEETING_RECORDER_WHISPER_LANGUAGE`,
   `MEETING_RECORDER_DATA_DIR`, `MEETING_RECORDER_SAMPLE_RATE`,
   `MEETING_RECORDER_CLEANUP_RAW_AUDIO_DAYS`,
   `MEETING_RECORDER_CLEANUP_MIXED_AUDIO_DAYS`)
4. CLI flags (`--whisper-model`, `--llm-endpoint`, etc.)

LLM API keys are never written to the active-session state file. If a remote
LLM needs a key, provide it through the config file, `MEETING_RECORDER_LLM_API_KEY`,
or `meeting-recorder stop --llm-api-key ...`. A key supplied only to `start`
must be supplied again when processing at `stop` time.

The non-secret whisper/LLM settings you pass to `start` are captured into that
session's state so `stop` (which may run minutes or hours later, in a
different shell) uses the same settings automatically -- but you can also
override them again at `stop` time (e.g. to re-summarize with a different
model without re-recording).

Configure audio retention independently of processing settings:

```yaml
cleanup:
  raw_audio_days: 7
  mixed_audio_days: 14
```

Both values are rolling 24-hour periods measured from the session end time.
`mixed_audio_days` must be at least `raw_audio_days`. Command-line overrides
are available as `cleanup --raw-audio-days N --mixed-audio-days N`.

### Configuring the summarization backend

Copy the example config and edit the `llm:` section:

```bash
mkdir -p ~/.config/meeting-recorder
cp config.example.yaml ~/.config/meeting-recorder/config.yaml
```

```yaml
llm:
  model: ollama/llama3.1          # LiteLLM model string
  endpoint: http://localhost:11434
  api_key: null
  reasoning_effort: null          # optional, forwarded to supporting providers
```

* **Local Ollama (default)**: `model: ollama/<name>`, `endpoint:
  http://localhost:11434`, no `api_key` needed. LiteLLM translates this into
  the appropriate Ollama `/api/generate`/`/api/chat` call.
* **A LiteLLM proxy or any other LiteLLM-supported provider**: just change
  `model` to whatever LiteLLM expects for that provider (e.g.
  `openai/gpt-4o-mini`, `litellm_proxy/my-model`), point `endpoint` at that
  server, and set `api_key`. No code changes needed.
  For the supplied Owlbear proxy configuration, use
  `model: REASONING-gpt56luna-c1` and `reasoning_effort: high`; set `endpoint`
  to the proxy's actual base URL. The model list does not itself specify that
  URL or the proxy authentication policy.
* Summarization sends the complete transcript and mode prompt in one LLM call.
  Configure a model whose context window is large enough for the full transcript,
  prompt, and response. The recorder fails clearly and remains retryable if the
  model rejects the input; it does not fall back to lossy partial summaries.
* Modes with multiple generated documents, such as `game`, request all documents
  in that one response and split them at their exact Markdown headings.

### Smart device selectors and aliases

`list-devices` gives each currently connected microphone an `mN` selector and
each output monitor an `oN` selector. The default device of each type is first,
then the rest are alphabetized. These selectors are intentionally live: use a
personal alias when a durable name matters.

Create an editable alias file once:

```bash
meeting-recorder devices init
# edit ~/.config/meeting-recorder/devices.yaml
```

```yaml
version: 1
aliases:
  desk-mic: alsa_input.usb-Blue_Microphones_Yeti-00
  headset-output: alsa_output.usb-Headset-00.analog-stereo.monitor
```

Then use `--mic desk-mic` or `--system-source headset-output`. Exact PipeWire
source IDs remain valid, and a unique close match such as `--mic yeti` is also
resolved locally. An ambiguous name fails with the available choices instead
of guessing.

For an otherwise unmatched natural-language hint, explicitly permit the
configured LLM to select from the *live* candidate list:

```bash
meeting-recorder start --allow-llm-device-selection --mic "the USB headset"
```

This sends the hint and device names—not audio or transcripts—to the configured
LLM endpoint. Its response must name one current `mN`/`oN` selector and is
validated before recording starts. Without the flag, no device-selection LLM
call is made. Use `--device-config PATH` to use another alias file; `devices
init` never overwrites an existing one unless given `--force`.

## Project layout

```
meeting_recorder/
  cli.py         argparse entry point (start/stop/status/list-devices)
  config.py      layered config (defaults -> file -> env -> CLI)
  cleanup.py     retention planning, reporting, and guarded deletion
  audio.py       PipeWire device discovery + FFmpeg recording (start/stop)
  device_selection.py  live mN/oN selectors, aliases, local matching, optional LLM validation
  state.py       session persistence between the start and stop invocations
  session_archive.py  durable per-session metadata and prompt snapshots
  modes.py       validated mode registry and packaged prompt loading
  modes/         modes.yaml plus one prompt asset per recording mode
  transcribe.py  faster-whisper wrapper
  summarize.py   Full-context LiteLLM summarization and artifact parsing
  errors.py      shared exception types
```

## Troubleshooting

* **`Missing required tool(s): ffmpeg, pactl`** -- install them with
  `sudo apt install ffmpeg pulseaudio-utils`.
* **`ffmpeg exited immediately`** -- check the printed log tail (also saved
  to `ffmpeg.log` in the session directory); this usually means a device
  name from `--mic`/`--system-source` doesn't exist or is already exclusively
  in use. Re-run `meeting-recorder list-devices` to confirm exact names.
* **Capture validation failed** -- the recorder will not send incomplete or
  corrupt audio to Whisper. Inspect `capture-validation.json` and `ffmpeg.log`
  in the session directory; they identify the affected track and any relevant
  FFmpeg error. The FFmpeg log also records the exact command, selected
  sources, startup-header validation, and stop/exit events. A valid silent
  track is not treated as a failure.
* **Session state is `UNVERIFIED OR STALE`** -- for safety, the recorder will
  never signal a PID unless it still matches the FFmpeg process it launched.
  Confirm no recording is active, then remove the state file path printed by
  the command before starting another session.
* **Ollama connection errors during `stop`** -- make sure `ollama serve` is
  running and the model in your config has been pulled
  (`ollama pull llama3.1`).
* **The model could not accept the complete transcript** -- configure a model
  with a larger context window, then run `meeting-recorder retry`. The saved
  transcript is reused without repeating transcription.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

## Privacy

Meeting Recorder processes audio and transcripts according to the configured
transcription and LLM providers. Depending on your configuration, transcript
content may be sent to third-party AI services.

Users are responsible for complying with applicable recording-consent and
privacy laws when recording conversations.
