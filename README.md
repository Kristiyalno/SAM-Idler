# SAM Idler

A lightweight Python/Tkinter GUI for farming Steam trading cards using `SAM.Game.exe` as the idle engine. Automates the standard two-phase workflow: get everything past the 2-hour refund window first, then farm cards one game at a time.

## How it works

SAM Idler launches `SAM.Game.exe` as a subprocess with the target App ID as an argument. Steam registers the process as the game running, which is the same mechanism SAM uses internally.

**Phase 1** runs all games with under 2 hours of playtime simultaneously. Steam withholds card drops until a game is outside the refund window, so this phase burns through the wait in parallel. Each game is stopped once it hits the 2-hour mark.

**Phase 2** idles each remaining game one at a time in list order. Drop counts are checked every 5 minutes via that game's own gamecards page (`steamcommunity.com/my/gamecards/<appid>`), which explicitly says "No card drops remaining" or "N card drops remaining". When a game hits 0 drops the idler moves on automatically.

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
```

If either JSON file gets corrupted it is automatically backed up and reset to a clean state on next launch, with a warning in the log.

## Configuration (Settings window)

Open **Settings** in the top-right corner.

**Steam Web API** (library import, playtime)
- API Key: get one at https://steamcommunity.com/dev/apikey — the domain field can be anything, e.g. `localhost`
- Steam ID: paste your 64-bit ID, your full profile URL, or your vanity name, then click **Look up** to resolve it automatically

**Session cookies** (automatic card-drop detection, optional)
- `sessionid` and `steamLoginSecure` from your browser while logged into steamcommunity.com
- Open DevTools (F12) -> Application -> Cookies -> `https://steamcommunity.com`
- These expire periodically; re-enter them when automatic detection stops working

The API key and `steamLoginSecure` fields have a **Hide** checkbox next to them (enabled by default). Uncheck to reveal the value. The checkbox state persists between sessions. All text fields support Ctrl+A, Ctrl+C, Ctrl+X, Ctrl+Backspace/Ctrl+Delete to delete a word at a time, and right-click for a cut/copy/paste menu.

## Adding games

### Import from Steam (recommended)

Click **Import from Steam** to fetch your entire library with playtime already filled in. If session cookies are set, drop counts are pre-filled too (best-effort — see note below); otherwise they show `?` until you refresh them later.

Filter and sort options:
- Text filter by name or App ID; shows a `x/total` count when a filter is active
- "Only under 2h" checkbox to narrow to games that still need Phase 1
- Sort by App ID (default), Name, Playtime, or Drops
- Direction toggle button next to Sort switches between increasing and decreasing

Selection buttons:
- **Select All** / **Select None** / **Invert** — act on every game in your library, not just whatever the current filter is showing
- **Select with drops** — ticks every game with at least 1 card drop remaining

Filtering, sorting, and toggling "Only under 2h" never touch your checkbox selections. Mouse wheel scrolling works anywhere over the list.

Note on drop counts: the initial fill comes from Steam's aggregate badges page, which only lists a subset of games and can occasionally miss one. It's a sorting aid at import time, not the final word — the idler always re-checks each game's actual gamecards page once it starts idling it.

### Add via App ID

For games not in your library or without an API key, click **Add via App ID**. Cancelling any step cancels the whole thing. App IDs are in the Steam store URL: `store.steampowered.com/app/1091500/` -> `1091500`

## Playtime unit

The dropdown in the toolbar controls how playtime is displayed and entered everywhere. Options are minutes (default), hours, seconds, and days. Switching live re-renders the table. Persists between restarts.

Input is flexible: `1.5`, `1,5`, `,5`, `.5`, `0,25` all work.

## The game table

### Sorting

Click any column header to sort by it. Click again to flip direction. The active column shows `↑` or `↓`. Defaults to `#` (list order) ascending. Unknown drop counts (`?`) always sort to the end regardless of direction.

### Inline editing

Double-click any cell to edit it inline. Click elsewhere, press Enter, or press Escape to commit/dismiss.

| Column | Behaviour |
|---|---|
| `#` | Type a new position number to move the row |
| App ID | Edit the App ID directly |
| Name | Edit the game name |
| Playtime | Type a new value in the current unit; phase 1 status updates automatically |
| 2h done | Double-click to toggle yes/no |
| Cards done | Double-click to toggle yes/no |

### Right-click menu

Right-clicking any row opens a context menu with:
- Move to top / Move up / Move down / Move to bottom
- Toggle 2h done / Toggle cards done
- Remove

## Reordering

Phase 2 idles games in list order (`#` column). To change priority:
- Drag rows up or down in the table
- Use **Move Up / Move Down** below the table
- Double-click the `#` cell and type a position number
- Right-click and use the move options

Sorting the table by a column other than `#` is a view — it does not change the underlying list order that Phase 2 uses. To permanently reorder, use drag, the buttons, or the `#` cell edit.

## Status panel and log

The status panel (above the control buttons) shows the current phase, which game is being idled, how long it has been running, and when the next drop check fires. Crash notices appear here too.

The log at the bottom records every state change, drop count update, and error with timestamps.

## Pausing and resuming

**Pause** stops all idle processes but saves progress. Hit **Resume Idling** to continue exactly where it left off. Closing the window while running prompts you to pause first; progress is always saved either way.

## Other buttons

| Button | What it does |
|---|---|
| **Refresh Drops** | Updates card drop counts for every game — bulk pass first, then per-game for anything the bulk pass missed (requires cookies) |
| **Cards Dropped (manual)** | Advance Phase 2 to the next game without waiting for auto-detection |
| **Undo Remove** | Restores the last removed game (one level of undo) |
| **Pause** | Kill all idle processes and pause the session |

## Notes

- Games already at 2h+ playtime are skipped in Phase 1 automatically
- If cookies are not set, drop detection falls back to manual
- This tool does not touch achievements; it only keeps a process alive that Steam sees as in-game
- The app is always dark mode; it does not follow the system theme
- Running 10-20 games simultaneously in Phase 1 is fine. Beyond ~30 you may hit Steam's internal rate limits on "in-game" status updates, though there are no known hard bans for this

## If drop counts show `?`

Drop checking reads each game's own page at `steamcommunity.com/my/gamecards/<appid>`. If a check fails, the log will say why — most commonly:

- **"Steam didn't recognize the session"** — your cookies have expired. Re-enter `sessionid` and `steamLoginSecure` in Settings.
- **"Couldn't find a drop count... Steam may have changed the page layout"** — set the environment variable `SAM_IDLER_DEBUG_HTML=1` before launching and try again; the raw page HTML gets saved to a `debug_html` folder next to the script for inspection.