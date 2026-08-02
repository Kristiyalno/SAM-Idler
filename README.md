# SAM Idler

A Python/Tkinter GUI for farming Steam trading cards using `SAM.Game.exe` as the idle engine.

## How card drops work

Cards drop while a game is running. Steam tracks how long a game has been open and delivers drops on a timer, roughly every 30 minutes per card by default (developers can set a different interval). The drop lands in your inventory while the session is still active - you do not need to close the game to receive it.

**Running multiple games at once slows the drop rate per game.** Steam intentionally reduces drop frequency when more than one game is running. The exact amount varies but is significant - two games running simultaneously does not mean two cards arriving twice as fast, it means each card takes considerably longer to arrive.

**Some accounts have a drop delay.** If your account is new or has made a refund request recently, Steam adds a delay before drops start - typically around 2 hours of playtime per game. This is a refund-abuse prevention measure. On an established account with no recent refunds, drops can start within the first session with no specific playtime requirement.

## Idle modes

Pick one in Settings depending on what you want.

### Multi-idle (default)

Starts all games simultaneously and keeps them running indefinitely. Because everything is running at once, drops per game are slower, but all games are accumulating time in parallel. Good for large libraries where you just want to leave it running overnight and not think about it.

### Solo

Idles one game at a time in list order. Each game gets the full drop rate since nothing else is competing. Requires session cookies for automatic detection - the app checks drop counts every few minutes and moves to the next game automatically when a game hits 0. Without cookies, use the "Cards Dropped (manual)" button to advance manually.

### Multi then solo

Two-phase workflow: first runs all games simultaneously until each one hits a playtime threshold you set, then switches to solo mode for the actual drop farming. This is how Idle Master works. The idea is to get every game past the 2-hour drop delay gate simultaneously (since they all run at once, you only wait 2 hours total instead of 2 hours per game), then solo-idle each one at full drop rate. Only worth using if your account has the 2-hour delay. If it does not, skip straight to solo.

### Fast cycle

Runs all games simultaneously for a set interval, then rapidly stops and restarts each one in sequence to trigger drop delivery, then repeats. Stopping a game session causes Steam to immediately deliver any drop that has already been earned server-side but not yet pushed to your inventory. This is the "fast mode" approach from Idle Master Extended. Can be faster than solo in practice on some accounts, but results vary.

## Requirements

- Python 3.10+
- `SAM.Game.exe` and `SAM.API.dll` from a SAM release (see setup)
- Steam running and logged in

Optional — install both together for system tray support and the window icon:

```
pip install pystray pillow
```

The Python console window is automatically hidden on Windows when the app starts. If you need to see debug output, run from a terminal directly and it will stay visible.

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

**Session cookies** (automatic drop detection, optional)
- `sessionid` and `steamLoginSecure` from your browser while logged into steamcommunity.com
- Open DevTools (F12) -> Application -> Cookies -> `https://steamcommunity.com`
- These expire periodically. When drop counts stop updating, re-enter them.
- Without cookies the app cannot detect drops automatically. Use "Cards Dropped (manual)" instead.
- You do not need to keep the browser tab open. Cookies persist independently of open tabs.

All text fields support Ctrl+A, Ctrl+C, Ctrl+X, Ctrl+Backspace/Delete, and right-click for a cut/copy/paste menu. The API key and `steamLoginSecure` fields have a **Hide** checkbox (on by default, state persists).

**Display and behaviour**
- **Playtime unit** - minutes (default), hours, seconds, or days. Takes effect immediately everywhere.
- **Idle mode** - see Idle modes above. Each option has a description in the Settings window.
- **Switch to solo after** - shown for "multi then solo" only. Hours of playtime per game before switching to solo. Set to 0 to wait indefinitely.
- **Check for drops every** - shown for "solo" and "multi then solo". Time between automatic drop checks. Default 5 minutes. Requires session cookies.
- **Multi-idle for** - shown for "fast cycle" only. How long to run all games simultaneously before cycling through each one to collect drops.
- **Pause after stopping each game for** - shown for "fast cycle" only. How long to wait after stopping each game before checking drops and restarting it. Default 5 seconds. Lower values may cause Steam to not register the session end in time.
- **Merge Refresh buttons** - combine Refresh Drops and Refresh Playtimes into a single Refresh button.
- **Auto-remove completed** - automatically remove a game from the list once all its cards are dropped.
- **Start idling automatically on launch** - begin idling immediately when the app opens, without needing to click Start Idling.
- **Minimize to system tray instead of closing** - clicking the window's close button hides it to the system tray and keeps the idler running. Right-click the tray icon to restore or quit. Requires `pystray` and `Pillow` (see Requirements).

**Legacy**
- **Show "Cards Dropped (manual)" button** - hidden by default. Only useful if you never set session cookies and need to manually advance solo mode. If you have cookies set, the app detects drops automatically and this button does nothing useful.

## Adding games

### Import from Steam (recommended)

Click **Import from Steam** to fetch your entire library with playtime already filled in. If session cookies are set, drop counts are pre-filled too; otherwise they show `?` until you refresh.

Filter and sort options in the import dialog:
- Text filter by name or App ID; shows a `x/total` count when active
- "Only not in list" hides games you have already added
- Sort by App ID, Name, Playtime, or Drops with direction toggle
- **Select All** / **Select None** / **Invert** / **Select with drops**
- Filtering and sorting never affect checkbox selections
- Mouse wheel scrolling works anywhere over the list

### Add via App ID

Click **Add via App ID**. Cancelling any step cancels the whole add. App IDs are in the Steam store URL: `store.steampowered.com/app/1091500/` -> `1091500`

## The game table

### Search

The search bar above the table filters by name or App ID. Punctuation is ignored and dashes/underscores are treated as spaces, so "half life" matches "Half-Life". Shows a `x/total` count when active. Has a Clear button.

### Sorting

Click any column header to sort, click again to flip direction. Defaults to `#` (list order). Unknown drop counts (`?`) sort to the end.

### Inline editing

Double-click any cell to edit inline. Click elsewhere, Enter, or Escape to commit/dismiss.

| Column | Behaviour |
|---|---|
| `#` | Type a new position number to move the row |
| App ID | Edit the App ID directly |
| Name | Edit the game name |
| Playtime | Type a value in the current unit, or use a suffix to override it: `3h`, `90m`, `45s`, `2d` |
| Drops left | Type a number; setting it to 0 also marks cards done |
| Solo ready | Double-click to toggle yes/no - only relevant in "multi then solo" mode, ignored in all other modes |
| Cards done | Double-click to toggle yes/no |

### Multi-select editing

Ctrl+click or Shift+click to select multiple rows. Double-clicking Name, Playtime, or Drops left when multiple rows are selected opens a bulk-edit dialog that applies the value to all selected games at once.

### Right-click menu

Single row: Move to top / up / down / bottom, toggle solo ready / cards done, refresh playtime and drops for that game, remove.

Multiple rows: mark all solo ready, mark all cards done, bulk edit playtime, bulk edit drops, remove all selected.

### Keyboard shortcuts

- **Ctrl+Z** - undo the last change: edits, bulk edits, toggles, reordering, removals. Does not fire while typing in a text field.
- **Ctrl+Y** - redo. Does not fire while typing in a text field.
- **Delete** / **Backspace** - remove the selected game(s). Does not fire while typing in a text field.

## Reordering

Solo mode idles in list order (`#` column). To change priority:
- Drag rows up or down
- Use **Move Up / Move Down** below the table
- Double-click `#` and type a position number
- Right-click and use the move options
- Sort by another column (e.g. Drops left) and click **Reorder** to lock that order as the new list order

Sorting by any column other than `#` is view-only and does not affect idle order until you click **Reorder**.

## Status panel and log

The status panel shows the current mode and game being idled, how long it has been running, the estimated time remaining for the current game (solo mode only, requires at least two drop confirmations to calculate), and when the next drop check fires.

The summary bar above the table shows stats relevant to the current idle mode. In multi then solo mode it shows total drops left, multi-idle time remaining, how many games are not yet solo-ready, solo queue size, and done count. In multi or fast cycle mode the solo-ready columns are hidden since they don't apply. In solo mode only total drops left and done are shown.

The log records every event with a full timestamp. You can select and copy text in it directly. Buttons next to the Log label:
- **Copy Log** - copies the entire log to the clipboard
- **Export Log** - saves to `logs/log-YYYY-MM-DD_HH-MM-SS-mmm.txt`

## Pausing and resuming

**Pause** stops all idle processes but saves progress. **Resume Idling** picks up where it left off and re-checks drops and playtimes first. Closing while running (without tray mode) prompts you to pause first.

## Toolbar

**Left block, row 1**

| Button | What it does |
|---|---|
| **Import from Steam** | Fetch your full library with playtime and drop counts |
| **Add via App ID** | Manually add a game by Steam App ID |
| **Remove** | Remove the selected game |
| **Undo** | Undo the last change (same as Ctrl+Z) - covers edits, toggles, reordering, and removals |

**Left block, row 2**

| Button | What it does |
|---|---|
| **Remove Completed** | Remove all games marked cards done (asks for confirmation) |
| **Remove All** | Remove every game (asks for confirmation, undoable with Ctrl+Z) |
| **Full Reset** | Remove every game and wipe all settings including API key and cookies. Cannot be undone. |
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
| **Start Idling** / **Resume Idling** | Start or resume - automatically refreshes drops and playtimes first (skipped silently if credentials are not set) |
| **Pause** | Stop all idle processes and save progress |
| **Cards Dropped (manual)** | Advance solo mode without waiting for auto-detection. Hidden by default - enable in Settings under Legacy. Only useful if you have no session cookies set. |

## Notes

- This tool does not touch achievements; it only keeps a process alive that Steam sees as in-game
- The app is always dark mode
- Running 10-20 games simultaneously is fine; beyond ~30 you may hit Steam rate limits

## If drop counts show `?`

Most likely your session cookies have expired. Re-enter `sessionid` and `steamLoginSecure` in Settings. The log will say specifically what failed.

If it still does not work after fresh cookies, set the environment variable `SAM_IDLER_DEBUG_HTML=1` before launching; the raw page HTML is saved to `debug_html/` for inspection.

```
SAM_IDLER_DEBUG_HTML=1 python SAM_idler.py
```

## VAC and anti-cheat

When the app starts or you import games, it checks each game's VAC status via the Steam store API and shows the result in the VAC column (`yes` / `no` / `?`). If any VAC-enabled games are in your list when you click Start Idling, you will be asked to confirm before the session starts.

The risk is not from idling itself. It comes from running the idler while you are actively connected to a VAC-secured server on the same machine. If VAC scans while SAM.Game.exe is open alongside a VAC-protected game session, it could flag it. Pause the idler before launching any VAC-protected multiplayer game.

**Rust specifically:** Rust shows as VAC-enabled on its Steam store page but uses Easy Anti-Cheat (EAC) in practice and issues game bans, not VAC bans. The VAC label on Rust's store page is a legacy listing. EAC is a separate system - idling Rust while actively playing it is still worth avoiding out of caution, but it is not a VAC concern.