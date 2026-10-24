# UI work: theme palette wiring (this change set)

## Fixed
- App did not start: `apply_appearance()` referenced an undefined `night` variable (NameError on launch).
- ttk LabelFrame styles used the wrong class name (`TLabelFrame` instead of `TLabelframe`), so frames kept the default light colours.
- Tuning gauge drew its arcs counter-clockwise and was clipped at the bottom; it now sweeps clockwise from the lower left over the top and fits the canvas.
- Nav buttons showed a white focus frame in dark themes.

## Added
- Central palette is now the single source of colours: every ttk style (incl. Treeview, Notebook, Scrollbar, Checkbutton, Spinbox, Tune/Dash styles) and every raw Tk widget (sidebar, top bar, status bar, nav buttons, Text areas, game.log window, gauge Canvas) reads from it.
- `retheme()` re-colours the running window in place (no widget is destroyed or created). A registry (`reg`, `tk_frame`, `tk_label`, `reg_text`) plus redraw hooks (gauge, report tags, nav buttons, theme cards, accent controls, top-bar controls) keep canvases and tags in sync. Combobox pop-down lists are updated too.
- Settings page: 8 theme preview cards (click / Enter / Space), custom accent (hex field, system colour chooser, 6 quick swatches), **Reset to Theme Default**, scrollable page, persistence note.
- Pure helpers (unit-tested): `normalize_hex_color`, `blend_colors`, `contrast_ratio`, `best_text_on`, `readable_on`, `sanitize_appearance`, `ui_palette` (adds nav/brand/dim/soft-accent and a readable `accent_text`).
- `accent_foreground` now picks whichever of dark/white has the better contrast instead of a fixed luminance threshold.
- Theme, mode and custom accent are saved atomically to `settings/ui.json`; legacy `accent` names are migrated.
- Persian names for the themes and new bilingual strings.
- `tests/test_theme_ui.py` (14 tests).

## Not touched
- Save logic, the rolling backup system and restore: all 9 backup-related functions (`make_backup`, `unique_backup_dir`, `backup_history`, `latest_backup`, `restore_save_backup`, `edit_save`, ...) are AST-identical to the input.
- Only `choose_accent` (which rebuilt the whole window) was replaced, by `choose_accent_from_box` (live).
