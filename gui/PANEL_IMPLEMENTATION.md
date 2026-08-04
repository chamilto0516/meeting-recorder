# Panel implementation notes

The implementation lives in `meeting_recorder_panel/` and is intentionally
separate from the supplied design handoff and HTML prototype.

## Run it

```bash
pip install -e '.[gui]'
meeting-recorder-panel
```

Install `desktop/meeting-recorder-panel.desktop` into
`~/.local/share/applications/` to add an application-menu entry.

The generated tray dot reports the state through color and pulse. Its tooltip
carries the status word because Qt cannot ask Linux desktop shells to draw a
portable dynamic label adjacent to the tray icon.

## Boundaries

- The panel's source, tests, launcher, and design artifacts remain in `gui/`.
- `meeting_recorder.gui_api` is the only core bridge; it reuses CLI locking and
  process-identity checks, emits processing stages, and stores non-secret UI
  history metadata next to session artifacts.
- The PySide6 dependency is opt-in through the `gui` installation extra, so
  console-only installations retain their existing dependency set.
