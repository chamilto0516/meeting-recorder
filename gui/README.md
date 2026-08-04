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
panel, right-click it for Open and Exit. The tooltip carries the textual
status because Linux desktop shells do not provide a portable text area beside
Qt tray icons.

## Layout

- `meeting_recorder_panel/app.py` contains the PySide6 widgets and worker
  bridge.
- `../design_tokens/` and `Meeting Recorder Panel.dc.html` remain the visual
  source of truth.
- `tests/` contains GUI-independent checks; core bridge tests remain with the
  recorder package where its safety behavior is tested.
