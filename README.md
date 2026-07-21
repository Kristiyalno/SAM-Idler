# SAM Idler

A lightweight Python/Tkinter GUI for farming Steam trading cards using `SAM.Game.exe` as the idle engine. Automates the standard two-phase workflow: get everything past the playtime threshold first, then farm cards one game at a time.

## How it works

SAM Idler launches `SAM.Game.exe` as a subprocess with the target App ID as an argument. Steam registers the process as the game running, which is the same mechanism SAM uses internally. The windows are hidden automatically.

**Phase 1** runs all games that haven't hit the playtime threshold simultaneously. Each game is stopped individually once it reaches the threshold, and Phase 1 completes once every game is done. The threshold defaults to 2 hours (the Steam refund window) but is configurable in Settings, including an infinite mode that never auto-stops.

**Phase 2** idles each remaining game one at a time in list order. Drop counts are checked every 5 minutes via each game's own gamecards page. When a game hits 0 drops the idler moves on automatically.

If `SAM.Game.exe` crashes during a session, the idler detects it and tries to restart it up to 3 times. If it still won't start, the game's timer is paused and retries continue every 5 minutes in the background.

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

If either JSON file gets corrupted it is automatically backed up and reset on next launch, with a warning in the log.

## Configuration (Settings window)

Open **Settings** in the top-right corner.

**Steam Web API** (library import, playtime refresh)
- API Key: get one at https://steamcommunity.com/dev/apikey — the domain field can be anything, e.g. `localhost`
- Steam ID: paste your 64-bit ID, your full profile URL, or your vanity name, then click **Look up** to resolve it automatically

**Session cookies** (automatic card-drop detection, optional)
- `sessionid` and `steamLoginSecure` from your browser while logged into steamcommunity.com
- Open DevTools (F12) -> Application -> Cookies -> `https://steamcommunity.com`
- These expire periodically; re-enter them when automatic detection stops working

The API key and `steamLoginSecure` fields have a **Hide** checkbox (enabled by default). The checkbox state persists between sessions. All text fields support Ctrl+A, Ctrl+C, Ctrl+X, Ctrl+Backspace/Ctrl+Delete, and right-click for a cut/copy/paste menu.

**Display & behaviour**
- **Playtime unit** — controls how playtime is displayed and entered everywhere (minutes by default, also hours/seconds/days). Takes effect immediately.
- **Phase 1 stops each game at** — hours of playtime before stopping a game in Phase 1. Default is 2. Set to 0 for infinite mode, which runs Phase 1 forever until you manually pause.

## Adding games

### Import from Steam (recommended)

Click **Import from Steam** to fetch your entire library with playtime already filled in. If session cookies are set, drop counts are pre-filled too; otherwise they show `?` until you refresh.

Filter and sort options in the import dialog:
- Text filter by name or App ID; shows a `x/total` count when active
- "Only under 2h" checkbox
- Sort by App ID, Name, Playtime, or Drops; direction toggle
- **Select All** / **Select None** / **Invert** / **Select with drops**
- Filtering and sorting never touch checkbox selections
- Mouse wheel scrolling works anywhere over the list

### Add via App ID

Click **Add via App ID**. Cancelling any step cancels the whole thing. App IDs are in the Steam store URL: `store.steampowered.com/app/1091500/` -> `1091500`

## Playtime input

Input is flexible everywhere: `1.5`, `1,5`, `,5`, `.5`, `0,25` all work.

## The game table

### Search

A search bar above the table filters by name or App ID. Punctuation (`' - : ( ) ™`) is ignored in matching so searching "half life" matches "Half-Life™". Shows a `x/total` count when active.

### Sorting

Click any column header to sort by it, click again to flip direction. Active column shows `↑` or `↓`. Defaults to `#` (list order). Unknown drop counts (`?`) sort to the end.

### Inline editing

Double-click any cell to edit inline. Click elsewhere, press Enter, or Escape to commit/dismiss.

| Column | Behaviour |
|---|---|
| `#` | Type a new position number to move the row |
| App ID | Edit the App ID directly |
| Name | Edit the game name |
| Playtime | Type a new value in the current unit; phase 1 status updates automatically |
| Drops left | Type a number; setting it to 0 also marks cards done |
| 2h done | Double-click to toggle yes/no |
| Cards done | Double-click to toggle yes/no |

### Multi-select editing

Ctrl+click or Shift+click to select multiple rows. Double-clicking Name, Playtime, or Drops left when multiple rows are selected opens a single bulk-edit dialog that applies the value to all selected games.

### Right-click menu

Right-clicking any row opens a context menu. When multiple rows are selected the menu shows bulk operations:

Single row: Move to top / Move up / Move down / Move to bottom, toggle 2h done / cards done, Refresh playtime & drops, Remove.

Multiple rows: Mark all 2h done, Mark all cards done, Bulk edit playtime, Bulk edit drops, Remove all selected.

## Reordering

Phase 2 idles games in list order (`#` column). To change priority:
- Drag rows up or down
- Use **Move Up / Move Down** below the table
- Double-click the `#` cell and type a position number
- Right-click and use the move options

Sorting by a column other than `#` is a view only — it does not affect Phase 2 idle order.

## Status panel and log

The status panel shows the current phase, which game is being idled, how long it has been running, and when the next drop check fires. During Phase 1 it shows a live countdown of the longest remaining wait (the bottleneck game). Crash notices appear here too.

The log records every event with a full timestamp (`YYYY-MM-DD HH:MM:SS`). You can select and copy text in it directly. Two buttons sit next to the Log label:
- **Copy Log** — copies the entire log to the clipboard
- **Export Log** — saves the log to `logs/log-YYYY-MM-DD_HH-MM-SS-mmm.txt` next to the script

## Pausing and resuming

**Pause** stops all idle processes but saves progress. **Resume Idling** continues exactly where it left off. Closing while running prompts you to pause first; progress is always saved either way.

## Toolbar buttons

| Button | What it does |
|---|---|
| **Import from Steam** | Fetch your full library with playtime and drop counts |
| **Add via App ID** | Manually add a game by Steam App ID |
| **Remove** | Remove the selected game |
| **Undo Remove** | Restore the last removed game (one level of undo) |
| **Remove All** | Remove every game from the list (asks for confirmation) |
| **Full Reset** | Remove every game and all progress (asks for confirmation) |
| **Refresh Drops** | Update drop counts for all games (requires cookies) |
| **Cards Dropped (manual)** | Advance Phase 2 without waiting for auto-detection |
| **Pause** | Kill all idle processes and pause the session |

## Notes

- Games already past the threshold are skipped in Phase 1 automatically
- If cookies are not set, drop detection falls back to manual
- This tool does not touch achievements; it only keeps a process alive that Steam sees as in-game
- The app is always dark mode
- Running 10-20 games simultaneously in Phase 1 is fine; beyond ~30 you may hit Steam's internal rate limits

## If drop counts show `?`

Most commonly your session cookies have expired. Re-enter `sessionid` and `steamLoginSecure` in Settings. The log will say specifically what failed.

If it still doesn't work after re-entering cookies, set the environment variable `SAM_IDLER_DEBUG_HTML=1` before launching; the raw page HTML is saved to `debug_html/` for inspection.