# SAM Idler

A lightweight Python/Tkinter GUI for farming Steam trading cards using `SAM.Game.exe` as the idle engine. Automates the standard two-phase workflow: get everything past the playtime threshold first, then farm cards one game at a time.

## How it works

SAM Idler launches `SAM.Game.exe` as a subprocess with the target App ID as an argument. Steam registers the process as the game running, which is the same mechanism SAM uses internally. The windows are hidden automatically so nothing appears on your taskbar.

**Phase 1** runs all games that haven't hit the playtime threshold simultaneously. Each game is stopped individually once it reaches the threshold, and Phase 1 completes once every game is done. The threshold defaults to 2 hours but is configurable in Settings, including an infinite mode that never auto-stops.

**Phase 2** idles each remaining game one at a time in list order. Drop counts are checked every 5 minutes by default (configurable in Settings) via each game's own gamecards page. When a game hits 0 drops the idler moves on automatically.

If `SAM.Game.exe` crashes during a session, the idler detects it and tries to restart it. If it won't restart after several attempts, the game's timer is paused and retries continue every 5 minutes in the background.

## Requirements

- Python 3.8+, no third-party packages
- `SAM.Game.exe` and `SAM.API.dll` from a SAM release (see setup)
- Steam running and logged in

## Setup

1. Download a SAM release from https://github.com/gibbed/SteamAchievementManager/releases
2. Extract the zip and copy the entire contents into the same folder as `SAM_idler.py`
3. Run:

```
python SAM_idler.py
```

Your folder should look like this:

```
SAM_idler.py
SAM.API.dll
SAM.Game.exe
SAM.Picker.exe
idler_config.json   <- auto-created on first save
idler_games.json    <- auto-created on first add
logs/               <- auto-created on first log export
```

If either JSON file gets corrupted it is automatically backed up and reset on next launch.

## Configuration (Settings window)

Open **Settings** on the right side of the toolbar.

**Steam Web API** (library import, playtime refresh)
- API Key: get one at https://steamcommunity.com/dev/apikey — the domain field can be anything, e.g. `localhost`
- Steam ID: paste your 64-bit ID, your full profile URL, or your vanity name, then click **Look up**

**Session cookies** (automatic card-drop detection, optional)
- `sessionid` and `steamLoginSecure` from your browser while logged into steamcommunity.com
- Open DevTools (F12) -> Application -> Cookies -> `https://steamcommunity.com`
- These expire periodically; re-enter them when detection stops working

The API key and `steamLoginSecure` fields have a **Hide** checkbox (enabled by default, state persists). All text fields support Ctrl+A, Ctrl+C, Ctrl+X, Ctrl+Backspace/Delete, and right-click for a cut/copy/paste menu.

**Display & behaviour**
- **Playtime unit** — minutes (default), hours, seconds, or days. Takes effect immediately everywhere.
- **Phase 1 stops each game at** — hours before stopping a game. Default 2. Set to 0 for infinite mode.
- **Check for drops every** — minutes between automatic Phase 2 drop checks. Default 5. Requires session cookies.
- **Merge Refresh buttons** — combine Refresh Drops and Refresh Playtimes into a single Refresh button.
- **Auto-remove completed** — automatically remove a game from the list once all its cards are dropped.

## Adding games

### Import from Steam (recommended)

Click **Import from Steam** to fetch your entire library with playtime already filled in. If session cookies are set, drop counts are pre-filled too; otherwise they show `?` until you refresh.

Filter and sort options in the import dialog:
- Text filter by name or App ID; shows a `x/total` count when active
- "Only under 2h" checkbox
- Sort by App ID, Name, Playtime, or Drops with direction toggle
- **Select All** / **Select None** / **Invert** / **Select with drops**
- Filtering and sorting never touch checkbox selections
- Mouse wheel scrolling works anywhere over the list

### Add via App ID

Click **Add via App ID**. Cancelling any step cancels the whole thing. App IDs are in the Steam store URL: `store.steampowered.com/app/1091500/` -> `1091500`

## The game table

### Search

The search bar above the table filters by name or App ID. Punctuation (`' : ( ) ™`) is ignored and dashes/underscores are treated as spaces, so "half life" matches "Half-Life™". Shows a `x/total` count when active. Has a Clear button.

### Sorting

Click any column header to sort, click again to flip direction. Active column shows `↑` or `↓`. Defaults to `#` (list order). Unknown drop counts (`?`) sort to the end.

### Inline editing

Double-click any cell to edit inline. Click elsewhere, Enter, or Escape to commit/dismiss.

| Column | Behaviour |
|---|---|
| `#` | Type a new position number to move the row |
| App ID | Edit the App ID directly |
| Name | Edit the game name |
| Playtime | Type a new value in the current unit; phase 1 status updates automatically |
| Drops left | Type a number; setting it to 0 also marks cards done |
| Phase 2 | Double-click to toggle yes/no — whether the game has cleared the Phase 1 threshold and is ready for Phase 2 |
| Cards done | Double-click to toggle yes/no |

### Multi-select editing

Ctrl+click or Shift+click to select multiple rows. Double-clicking Name, Playtime, or Drops left when multiple rows are selected opens a bulk-edit dialog that applies the value to all selected games at once.

### Right-click menu

Single row: Move to top / up / down / bottom, toggle Phase 2 ready / cards done, Refresh playtime & drops for that one game, Remove.

Multiple rows: Mark all Phase 2 ready, mark all cards done, bulk edit playtime, bulk edit drops, remove all selected.

### Keyboard shortcuts

- **Ctrl+Z** — undo the last change: edits, bulk edits, toggles, reordering, and removals (including Remove All and Full Reset). Doesn't fire while you're typing in a text field.
- **Ctrl+Y** — redo the last undone change. Doesn't fire while you're typing in a text field.
- **Delete** / **Backspace** — remove the selected game(s), same as the Remove button. Doesn't fire while you're typing in a text field.

## Reordering

Phase 2 idles in list order (`#` column). To change priority:
- Drag rows up or down
- Use **Move Up / Move Down** below the table
- Double-click `#` and type a position number
- Right-click and use the move options
- Sort the table by another column (e.g. Drops left) and click **Reorder** to lock that sorted order in as the new list order

Sorting by any column other than `#` is view-only and does not affect Phase 2 idle order until you click **Reorder**.

## Status panel and log

The status panel shows the current phase, which game is being idled, how long it has been running, when the next drop check fires, and a live countdown of the longest remaining Phase 1 wait.

The log records every event with a full timestamp. You can select and copy text in it directly. Buttons next to the Log label:
- **Copy Log** — copies the entire log to the clipboard
- **Export Log** — saves to `logs/log-YYYY-MM-DD_HH-MM-SS-mmm.txt`

## Pausing and resuming

**Pause** stops all idle processes but saves progress. **Resume Idling** continues exactly where it left off, and re-checks drops and playtimes automatically first (same as Start Idling — see below). Closing while running prompts you to pause first.

## Toolbar

The toolbar has a left block (game management, two rows) and a right block (refresh and settings) pinned to the top-right corner. The right block drops below the left block on narrow windows instead of clipping, staying right-aligned either way.

**Left block, row 1**

| Button | What it does |
|---|---|
| **Import from Steam** | Fetch your full library with playtime and drop counts |
| **Add via App ID** | Manually add a game by Steam App ID |
| **Remove** | Remove the selected game |
| **Undo Remove** | Restore the last removed game (one level of undo) — Ctrl+Z also works and covers edits, toggles, and reordering too, not just removals |

**Left block, row 2**

| Button | What it does |
|---|---|
| **Remove Completed** | Remove all games marked cards done (asks for confirmation) |
| **Remove All** | Remove every game (asks for confirmation, undoable with Ctrl+Z) |
| **Full Reset** | Remove every game and all progress (asks for confirmation, undoable with Ctrl+Z) |
| **Force Kill All SAM** | Kills every SAM.Game.exe process immediately (equivalent to `taskkill /F /IM SAM.Game.exe`) |

**Right block**

| Button | What it does |
|---|---|
| **Refresh Drops** | Update card drop counts for all games (requires cookies) |
| **Refresh Playtimes** | Update playtimes for all games from the Steam API (requires API key) |
| **Refresh** | Both of the above merged — shown when merge mode is enabled in Settings |
| **Settings** | Open the settings window |

**Control row** (below the table)

| Button | What it does |
|---|---|
| **Start Idling** / **Resume Idling** | Start or resume the idle session — automatically refreshes drops and playtimes first (silently skipped if cookies/API key aren't set) |
| **Pause** | Stop all idle processes and save progress |
| **Cards Dropped (manual)** | Advance Phase 2 without waiting for auto-detection (use when cookies aren't set) |

## Notes

- Games already past the threshold are skipped in Phase 1 automatically
- If cookies are not set, drop detection falls back to manual
- This tool does not touch achievements; it only keeps a process alive that Steam sees as in-game
- The app is always dark mode
- Running 10-20 games simultaneously in Phase 1 is fine; beyond ~30 you may hit Steam's internal rate limits

## If drop counts show `?`

Most commonly your session cookies have expired. Re-enter `sessionid` and `steamLoginSecure` in Settings. The log will say specifically what failed.

If it still doesn't work after fresh cookies, set the environment variable `SAM_IDLER_DEBUG_HTML=1` before launching; the raw page HTML is saved to `debug_html/` for inspection.