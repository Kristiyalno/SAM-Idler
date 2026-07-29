# SAM Idler

A Python/Tkinter GUI for farming Steam trading cards using `SAM.Game.exe` as the idle engine.

## How it works

SAM Idler launches `SAM.Game.exe` as a subprocess with the target App ID as an argument. Steam sees the process as the game running, which is the same mechanism SAM uses internally. The windows are hidden automatically.

**Bulk mode** runs all games simultaneously, indefinitely. This is the default behavior (threshold set to 0). Cards drop after you stop idling a game, so keeping everything launched together and stopping games one by one to check is the most practical workflow.

**Timed mode** is an optional variation where each game auto-stops once it hits a configured playtime threshold. Set the threshold in Settings. With the default of 0 it never auto-stops.

**Focused mode** (what the app calls Phase 2) idles each game one at a time in list order. Drop counts are checked every 5 minutes by default (configurable) via each game's own gamecards page. When a game hits 0 drops the idler moves on automatically. This is useful when you want to concentrate drops on specific games.

**Note on the 2-hour myth:** There is no 2-hour playtime threshold for card drops in Steam. Cards drop based on time idled, not a playtime gate. The old default of 2 hours was wrong and has been removed.

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
- API Key: get one at https://steamcommunity.com/dev/apikey - the domain field can be anything, e.g. `localhost`
- Steam ID: paste your 64-bit ID, your full profile URL, or your vanity name, then click **Look up**

**Session cookies** (automatic card-drop detection, optional)
- `sessionid` and `steamLoginSecure` from your browser while logged into steamcommunity.com
- Open DevTools (F12) -> Application -> Cookies -> `https://steamcommunity.com`
- These expire periodically; re-enter them when detection stops working

The API key and `steamLoginSecure` fields have a **Hide** checkbox (enabled by default, state persists). All text fields support Ctrl+A, Ctrl+C, Ctrl+X, Ctrl+Backspace/Delete, and right-click for a cut/copy/paste menu.

**Display and behaviour**
- **Playtime unit** - minutes (default), hours, seconds, or days. Takes effect immediately everywhere.
- **Phase 1 stops each game at** - hours before auto-stopping in bulk mode. Default 0 (never auto-stop, runs forever).
- **Check for drops every** - minutes between automatic drop checks in focused mode. Default 5. Requires session cookies.
- **Merge Refresh buttons** - combine Refresh Drops and Refresh Playtimes into a single Refresh button.
- **Auto-remove completed** - automatically remove a game from the list once all its cards are dropped.

## Adding games

### Import from Steam (recommended)

Click **Import from Steam** to fetch your entire library with playtime already filled in. If session cookies are set, drop counts are pre-filled too; otherwise they show `?` until you refresh.

Filter and sort options in the import dialog:
- Text filter by name or App ID; shows a `x/total` count when active
- "Only not in list" checkbox to hide games you have already added
- Sort by App ID, Name, Playtime, or Drops with direction toggle
- **Select All** / **Select None** / **Invert** / **Select with drops**
- Filtering and sorting never touch checkbox selections
- Mouse wheel scrolling works anywhere over the list

### Add via App ID

Click **Add via App ID**. Cancelling any step cancels the whole thing. App IDs are in the Steam store URL: `store.steampowered.com/app/1091500/` -> `1091500`

## The game table

### Search

The search bar above the table filters by name or App ID. Punctuation (`' : ( ) TM`) is ignored and dashes/underscores are treated as spaces, so "half life" matches "Half-Life". Shows a `x/total` count when active. Has a Clear button.

### Sorting

Click any column header to sort, click again to flip direction. Active column shows an arrow. Defaults to `#` (list order). Unknown drop counts (`?`) sort to the end.

### Inline editing

Double-click any cell to edit inline. Click elsewhere, Enter, or Escape to commit/dismiss.

| Column | Behaviour |
|---|---|
| `#` | Type a new position number to move the row |
| App ID | Edit the App ID directly |
| Name | Edit the game name |
| Playtime | Type a new value in the current unit; phase status updates if a threshold is set |
| Drops left | Type a number; setting it to 0 also marks cards done |
| Phase 2 | Double-click to toggle yes/no - whether the game is queued for focused mode |
| Cards done | Double-click to toggle yes/no |

### Multi-select editing

Ctrl+click or Shift+click to select multiple rows. Double-clicking Name, Playtime, or Drops left when multiple rows are selected opens a bulk-edit dialog that applies the value to all selected games at once.

### Right-click menu

Single row: Move to top / up / down / bottom, toggle Phase 2 / cards done, Refresh playtime and drops for that game, Remove.

Multiple rows: Mark all Phase 2 ready, mark all cards done, bulk edit playtime, bulk edit drops, remove all selected.

### Keyboard shortcuts

- **Ctrl+Z** - undo the last change: edits, bulk edits, toggles, reordering, removals. Does not fire while typing in a text field.
- **Ctrl+Y** - redo the last undone change. Does not fire while typing in a text field.
- **Delete** / **Backspace** - remove the selected game(s). Does not fire while typing in a text field.

## Reordering

Focused mode (Phase 2) idles in list order (`#` column). To change priority:
- Drag rows up or down
- Use **Move Up / Move Down** below the table
- Double-click `#` and type a position number
- Right-click and use the move options
- Sort by another column (e.g. Drops left) and click **Reorder** to lock that order as the new list order

Sorting by any column other than `#` is view-only and does not affect idle order until you click **Reorder**.

## Status panel and log

The status panel shows the current phase, which game is being idled, how long it has been running, when the next drop check fires, and a live countdown of the longest remaining bulk-mode wait.

The log records every event with a full timestamp. You can select and copy text in it directly. Buttons next to the Log label:
- **Copy Log** - copies the entire log to the clipboard
- **Export Log** - saves to `logs/log-YYYY-MM-DD_HH-MM-SS-mmm.txt`

## Pausing and resuming

**Pause** stops all idle processes but saves progress. **Resume Idling** continues exactly where it left off, and re-checks drops and playtimes first. Closing while running prompts you to pause first.

## Toolbar

The toolbar has a left block (game management, two rows) and a right block (refresh and settings) pinned to the top-right corner. The right block drops below the left block on narrow windows instead of clipping.

**Left block, row 1**

| Button | What it does |
|---|---|
| **Import from Steam** | Fetch your full library with playtime and drop counts |
| **Add via App ID** | Manually add a game by Steam App ID |
| **Remove** | Remove the selected game |
| **Undo Remove** | Restore the last removed game - Ctrl+Z also works and covers edits, toggles, and reordering |

**Left block, row 2**

| Button | What it does |
|---|---|
| **Remove Completed** | Remove all games marked cards done (asks for confirmation) |
| **Remove All** | Remove every game (asks for confirmation, undoable with Ctrl+Z) |
| **Full Reset** | Remove every game and wipe all settings including API key and cookies (asks for confirmation) |
| **Force Kill All SAM** | Kills every SAM.Game.exe process immediately |

**Right block**

| Button | What it does |
|---|---|
| **Refresh Drops** | Update card drop counts for all games (requires cookies) |
| **Refresh Playtimes** | Update playtimes for all games from the Steam API (requires API key) |
| **Refresh** | Both of the above merged - shown when merge mode is enabled in Settings |
| **Settings** | Open the settings window |

**Control row** (below the table)

| Button | What it does |
|---|---|
| **Start Idling** / **Resume Idling** | Start or resume - automatically refreshes drops and playtimes first (silently skipped if credentials are not set) |
| **Pause** | Stop all idle processes and save progress |
| **Cards Dropped (manual)** | Advance focused mode without waiting for auto-detection (use when cookies are not set) |

## Notes

- This tool does not touch achievements; it only keeps a process alive that Steam sees as in-game
- The app is always dark mode
- Running 10-20 games simultaneously in bulk mode is fine; beyond ~30 you may hit Steam's internal rate limits

## If drop counts show `?`

Most commonly your session cookies have expired. Re-enter `sessionid` and `steamLoginSecure` in Settings. The log will say specifically what failed.

If it still does not work after fresh cookies, set the environment variable `SAM_IDLER_DEBUG_HTML=1` before launching; the raw page HTML is saved to `debug_html/` for inspection.