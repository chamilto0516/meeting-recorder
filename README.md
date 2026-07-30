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
  [0] alsa_input.usb-Blue_Microphones_Yeti-00 (default)
  [1] alsa_input.pci-0000_00_1f.3.analog-stereo

System audio (sink monitors -- captures whatever is playing):
  [2] alsa_output.pci-0000_00_1f.3.analog-stereo.monitor (default sink's monitor)
```

### 2. Start recording

```bash
# simplest: default mic + default system output
meeting-recorder start

# multiple mics (e.g. two people, two headsets)
meeting-recorder start --mic alsa_input.usb-Blue_Microphones_Yeti-00 \
                        --mic alsa_input.usb-Another_Mic-00

# pin a specific system audio source instead of the default sink's monitor
meeting-recorder start --system-source alsa_output.usb-Headset.monitor
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

### Other commands

```bash
meeting-recorder status    # is a recording currently active?
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
transcript.txt    # faster-whisper output
summary.txt       # LLM summary
```

Override the base directory with `--data-dir` or `MEETING_RECORDER_DATA_DIR`.

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
   `MEETING_RECORDER_DATA_DIR`, `MEETING_RECORDER_SAMPLE_RATE`)
4. CLI flags (`--whisper-model`, `--llm-endpoint`, etc.)

The whisper/LLM settings you pass to `start` are captured into that
session's state so `stop` (which may run minutes or hours later, in a
different shell) uses the same settings automatically -- but you can also
override them again at `stop` time (e.g. to re-summarize with a different
model without re-recording).

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
  chunk_char_limit: 6000
```

* **Local Ollama (default)**: `model: ollama/<name>`, `endpoint:
  http://localhost:11434`, no `api_key` needed. LiteLLM translates this into
  the appropriate Ollama `/api/generate`/`/api/chat` call.
* **A LiteLLM proxy or any other LiteLLM-supported provider**: just change
  `model` to whatever LiteLLM expects for that provider (e.g.
  `openai/gpt-4o-mini`, `litellm_proxy/my-model`), point `endpoint` at that
  server, and set `api_key`. No code changes needed.
* Long transcripts are automatically map-reduced: if the transcript exceeds
  `chunk_char_limit` characters, it's split into chunks, each summarized
  individually, and the partial summaries are combined into one final
  summary call.

## Project layout

```
meeting_recorder/
  cli.py         argparse entry point (start/stop/status/list-devices)
  config.py      layered config (defaults -> file -> env -> CLI)
  audio.py       PipeWire device discovery + FFmpeg recording (start/stop)
  state.py       session persistence between the start and stop invocations
  transcribe.py  faster-whisper wrapper
  summarize.py   LiteLLM summarization, with map-reduce chunking
  errors.py      shared exception types
```

## Troubleshooting

* **`Missing required tool(s): ffmpeg, pactl`** -- install them with
  `sudo apt install ffmpeg pulseaudio-utils`.
* **`ffmpeg exited immediately`** -- check the printed log tail (also saved
  to `ffmpeg.log` in the session directory); this usually means a device
  name from `--mic`/`--system-source` doesn't exist or is already exclusively
  in use. Re-run `meeting-recorder list-devices` to confirm exact names.
* **A recording is already in progress** -- run `meeting-recorder stop`
  (or `meeting-recorder status` to check first). If the process actually
  crashed, `start` will detect the stale state and let you proceed.
* **Ollama connection errors during `stop`** -- make sure `ollama serve` is
  running and the model in your config has been pulled
  (`ollama pull llama3.1`).
