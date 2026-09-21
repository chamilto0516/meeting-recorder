# Meeting Recorder Panel

The PySide6 control panel is a tray-first companion for `meeting-recorder`.
It recreates the supplied Industry design reference and uses the recorder's
locking, device validation, and session-state safeguards directly.

## Install and run

```bash
pip install -e '.[gui]'
meeting-recorder-panel
```

Install `desktop/meeting-recorder-panel.desktop` into
`~/.local/share/applications/` to make it available from the desktop menu.

The panel uses a generated dot tray icon: click it to toggle the control
panel, right-click it for Stop (while recording), Open, and Exit. The tooltip
carries the textual status because Linux desktop shells do not provide a
portable text area beside Qt tray icons.

## Status indicator

`meeting-recorder start` launches a lightweight, tray-only
`meeting-recorder-indicator` process by default, so a recording started from
a terminal still shows a status-bar icon: a slow gray-to-red pulse while
recording, right-click Stop to end it. It exits on its own a few seconds
after the session finishes. Disable it per-invocation with `--no-indicator`,
or globally with `MEETING_RECORDER_NO_INDICATOR=1`.

Only one process ever shows the tray icon at a time (panel, a CLI-spawned
indicator, or an autostart indicator all race for the same lock; whoever
loses runs without a visible tray). To keep the indicator resident across
login instead of only while a CLI recording is active, install
`desktop/meeting-recorder-indicator.desktop` into `~/.config/autostart/`.

## Layout

- `meeting_recorder_panel/app.py` contains the PySide6 widgets and worker
  bridge.
- `meeting_recorder_panel/tray.py` contains the shared tray icon drawing,
  the single-instance lock, and the state-polling `TrayController` used by
  both the panel and the indicator.
- `meeting_recorder_panel/indicator.py` is the standalone tray-only process.
- `../design_tokens/` and `Meeting Recorder Panel.dc.html` remain the visual
  source of truth.
- `tests/` contains GUI-independent checks; core bridge tests remain with the
  recorder package where its safety behavior is tested.
