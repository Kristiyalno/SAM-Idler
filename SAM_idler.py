"""
SAM Idler
Idles games using SAM.Game.exe (same mechanism SAM uses internally).

Phase 1: Run all games with < 2h playtime simultaneously until each hits 2h.
Phase 2: Run each game one at a time until cards are confirmed dropped.

Automatic detection:
- Library + playtime: Steam Web API (requires API key + Steam ID)
- Card drops remaining: steamcommunity.com/my/gamecards/<appid> per game
  (requires session cookies). The aggregate badges list is only used as a
  quick best-effort pre-fill for the import dialog and bulk refresh; the
  per-app gamecards page is what actually decides when a game is done.

Requirements:
- SAM.Game.exe and SAM.API.dll in the same folder as this script
- Steam running and logged in
- Python 3.8+, no extra packages
"""

import json
import os
import re
import subprocess
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

DATA_FILE    = Path(__file__).parent / "idler_games.json"
CONFIG_FILE  = Path(__file__).parent / "idler_config.json"
SAM_GAME_EXE = Path(__file__).parent / "SAM.Game.exe"

# If drop-count parsing ever fails to find a count on a page that should have
# one (Steam changed the markup again), set SAM_IDLER_DEBUG_HTML=1 in the
# environment before launching to save the raw page HTML here for inspection
# instead of just guessing at the fix blind next time.
DEBUG_HTML_DUMPS = os.environ.get("SAM_IDLER_DEBUG_HTML") == "1"
DEBUG_DUMP_DIR   = Path(__file__).parent / "debug_html"


def _maybe_dump_debug_html(label: str, html: str) -> None:
    if not DEBUG_HTML_DUMPS:
        return
    try:
        DEBUG_DUMP_DIR.mkdir(exist_ok=True)
        path = DEBUG_DUMP_DIR / f"{label}_{int(time.time())}.html"
        path.write_text(html, encoding="utf-8", errors="replace")
    except Exception:
        pass   # debug aid only, never let this break the actual check


PHASE1_POLL_INTERVAL = 30   # seconds between phase 1 timer checks
PHASE2_CARD_POLL_MIN = 5    # default minutes between automatic card-drop checks (configurable in Settings)
CRASH_CHECK_INTERVAL = 5    # seconds between liveness checks on the idling process
CRASH_MAX_RETRIES    = 3    # consecutive quick restart attempts before backing off
CRASH_RETRY_BACKOFF  = 10   # seconds to wait before each quick restart attempt
CRASH_GIVEUP_RETRY_INTERVAL = 300  # seconds between retries once quick attempts are exhausted

# Playtime unit helpers
UNITS = ["minutes", "hours", "seconds", "days"]
UNIT_TO_HOURS = {"minutes": 1/60, "hours": 1.0, "seconds": 1/3600, "days": 24.0}
UNIT_FROM_HOURS = {"minutes": 60.0, "hours": 1.0, "seconds": 3600.0, "days": 1/24}


def parse_playtime(raw: str, unit: str) -> float:
    """
    Parse a user-typed playtime string in the given unit and return hours.
    Accepts: '1.5', '1,5', '.5', ',5', '1,500' (treated as 1.5 not 1500).
    """
    if raw is None:
        return 0.0
    s = raw.strip().replace(" ", "")
    # Replace comma used as decimal separator (e.g. '1,5' -> '1.5')
    # But not thousand separators: if there are digits on both sides and
    # more than 2 digits after the comma, treat as thousands sep; otherwise decimal.
    def _fix_comma(t: str) -> str:
        # Replace a lone leading comma: ',5' -> '0.5'
        if t.startswith(","):
            t = "0." + t[1:]
        # Replace comma used as decimal if pattern is digits,1-2digits
        t = re.sub(r"(\d),(\d{1,2})$", r"\1.\2", t)
        # Any remaining commas are thousand separators - remove them
        t = t.replace(",", "")
        return t
    s = _fix_comma(s)
    try:
        value = float(s)
    except ValueError:
        return 0.0
    return value * UNIT_TO_HOURS.get(unit, 1.0)


def hours_to_unit(hours: float, unit: str) -> float:
    return hours * UNIT_FROM_HOURS.get(unit, 1.0)


# ---------------------------------------------------------------------------
# Config / game persistence
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    "api_key": "", "steam_id": "",
    "session_id": "", "login_secure": "",
    "playtime_unit": "minutes",
    "hide_api_key": True,
    "hide_login_secure": True,
    "phase1_threshold_hours": 2.0,
    "merge_refresh_buttons": False,
    "auto_remove_completed": False,
    "phase2_poll_minutes": 5.0,
}


def _backup_corrupt_file(path: Path) -> str | None:
    """Copy an unreadable file aside so it isn't silently lost, return the backup path as a string."""
    try:
        backup = path.with_name(path.name + ".corrupt.bak")
        backup.write_bytes(path.read_bytes())
        return str(backup)
    except Exception:
        return None


def load_config() -> tuple[dict, str | None]:
    """Returns (config, warning). warning is set if the file existed but couldn't be used."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("config file did not contain a JSON object")
            cfg = dict(_DEFAULT_CONFIG)
            cfg.update(loaded)
            return cfg, None
        except Exception as exc:
            backup = _backup_corrupt_file(CONFIG_FILE)
            warning = (
                f"idler_config.json couldn't be read ({exc}) and was reset to defaults.\n"
                + (f"Your old file was saved as {backup}." if backup else "")
            )
            return dict(_DEFAULT_CONFIG), warning
    return dict(_DEFAULT_CONFIG), None


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _sanitize_game(entry) -> dict | None:
    """Coerce a possibly-malformed game entry into a valid one, or None if unusable."""
    if not isinstance(entry, dict):
        return None
    app_id = str(entry.get("app_id", "")).strip()
    if not app_id or not re.search(r"\d", app_id):
        return None
    app_id = re.sub(r"[^\d]", "", app_id) or app_id
    name = str(entry.get("name") or f"App {app_id}").strip() or f"App {app_id}"
    try:
        playtime = float(entry.get("playtime_hours", 0.0))
    except (TypeError, ValueError):
        playtime = 0.0
    try:
        cards_remaining = int(entry.get("cards_remaining", -1))
    except (TypeError, ValueError):
        cards_remaining = -1
    return {
        "app_id": app_id,
        "name": name,
        "playtime_hours": playtime,
        "cards_remaining": cards_remaining,
        "phase1_done": bool(entry.get("phase1_done", playtime >= 2.0)),
        "cards_done": bool(entry.get("cards_done", False)),
    }


def load_games() -> tuple[list, str | None]:
    """Returns (games, warning). warning is set if the file existed but needed repair/reset."""
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, list):
                raise ValueError("games file did not contain a JSON list")
            games = []
            dropped = 0
            for entry in loaded:
                sanitized = _sanitize_game(entry)
                if sanitized is not None:
                    games.append(sanitized)
                else:
                    dropped += 1
            warning = None
            if dropped:
                backup = _backup_corrupt_file(DATA_FILE)
                warning = (
                    f"{dropped} entr{'y' if dropped == 1 else 'ies'} in idler_games.json "
                    "were malformed and skipped.\n"
                    + (f"Your original file was saved as {backup}." if backup else "")
                )
            return games, warning
        except Exception as exc:
            backup = _backup_corrupt_file(DATA_FILE)
            warning = (
                f"idler_games.json couldn't be read ({exc}) and your game list was reset.\n"
                + (f"Your old file was saved as {backup}." if backup else "")
            )
            return [], warning
    return [], None


def save_games(games: list) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(games, f, indent=2)


def default_game(app_id: str, name: str = "", playtime_h: float = 0.0, cards_remaining: int = -1) -> dict:
    return {
        "app_id": str(app_id).strip(),
        "name": name.strip() or f"App {app_id}",
        "playtime_hours": playtime_h,
        "cards_remaining": cards_remaining,
        "phase1_done": playtime_h >= 2.0,
        "cards_done": (cards_remaining == 0),
    }


# ---------------------------------------------------------------------------
# Steam API helpers
# ---------------------------------------------------------------------------

def _http_get(url: str, cookies: dict | None = None, timeout: int = 15) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def resolve_steam_id(api_key: str, text: str) -> str:
    """
    Turn a vanity name, profile URL, or raw 64-bit Steam ID into a 64-bit
    Steam ID using the Web API. Raises ValueError with a human-readable
    message if it can't be resolved.
    """
    text = text.strip()
    if not text:
        raise ValueError("Enter a Steam ID, vanity name, or profile URL first.")

    # Pull a vanity name out of a full profile URL if one was pasted.
    m = re.search(r"steamcommunity\.com/id/([^/\s]+)", text)
    if m:
        text = m.group(1)
    else:
        m = re.search(r"steamcommunity\.com/profiles/(\d+)", text)
        if m:
            text = m.group(1)

    # Already a 64-bit numeric Steam ID (17 digits, starts with 7656119...).
    if text.isdigit() and len(text) >= 15:
        return text

    # Otherwise treat it as a vanity name and resolve via the Web API.
    url = (
        "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
        f"?key={api_key}&vanityurl={urllib.parse.quote(text)}"
    )
    data = json.loads(_http_get(url))
    resp = data.get("response", {})
    if resp.get("success") == 1 and resp.get("steamid"):
        return resp["steamid"]
    raise ValueError(
        f"Couldn't resolve '{text}' to a Steam ID. "
        "Double check the vanity name/URL, or paste the raw 64-bit ID instead."
    )


def fetch_owned_games(api_key: str, steam_id: str) -> list[dict]:
    url = (
        "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
        f"?key={api_key}&steamid={steam_id}"
        "&include_appinfo=1&include_played_free_games=1&format=json"
    )
    data = json.loads(_http_get(url))
    result = []
    for g in data.get("response", {}).get("games", []):
        result.append({
            "app_id": str(g["appid"]),
            "name": g.get("name", f"App {g['appid']}"),
            "playtime_hours": round(g.get("playtime_forever", 0) / 60, 2),
        })
    return result


# ---------------------------------------------------------------------------
# Card drop parsing
# ---------------------------------------------------------------------------
#
# The per-game "gamecards" page is the authoritative source for a single
# app's drop count: it always exists for any card-eligible game the account
# owns (regardless of whether that game happens to be listed on the paginated
# aggregate badges page, which Steam only populates with a subset of games).
# On that page Steam renders one <span class="progress_info_bold"> containing
# either "No card drops remaining" or "N card drops remaining". This mirrors
# the parsing approach used by di72nn/steam_idle_master (a working, real-world
# Python Steam idler) rather than a blind text search over the whole page,
# since scanning raw HTML for that phrase risks matching help text or other
# chrome that happens to contain similar wording.

class _ProgressInfoParser(HTMLParser):
    """Grabs the text of the first <span class="progress_info_bold"> on the
    page, and separately notes whether a logged-in user's avatar link is
    present (Steam always renders <a class="user_avatar"> in the page header
    when the request cookies are valid; its absence means the cookies were
    rejected and we got a login/error page instead)."""

    def __init__(self):
        super().__init__()
        self.progress_text: str | None = None
        self.is_authorized = False
        self._capture = False
        self._done = False

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        cls = attr.get("class", "")
        if tag == "span" and "progress_info_bold" in cls and not self._done:
            self._capture = True
        if tag == "a" and "user_avatar" in cls:
            self.is_authorized = True

    def handle_endtag(self, tag):
        if tag == "span" and self._capture:
            self._capture = False
            self._done = True

    def handle_data(self, data):
        if self._capture:
            self.progress_text = (self.progress_text or "") + data


_NO_DROPS_TEXT_RE   = re.compile(r"no card drops remaining", re.IGNORECASE)
_DROPS_LEFT_TEXT_RE = re.compile(r"(\d+)\s+card drops?\s+remaining", re.IGNORECASE)


def fetch_app_card_drops(session_id: str, login_secure: str, app_id: str, steam_id: str = "") -> int:
    cookies = {"sessionid": session_id, "steamLoginSecure": login_secure}
    url = (
        f"https://steamcommunity.com/profiles/{steam_id}/gamecards/{app_id}"
        if steam_id else
        f"https://steamcommunity.com/my/gamecards/{app_id}"
    )
    html = _http_get(url, cookies=cookies)

    # The gamecards page does NOT include <a class="user_avatar"> — that element
    # only appears on other Steam page types. Check the data-userinfo JSON blob
    # instead, which Steam always embeds when the session is valid.
    is_logged_in = '"logged_in":true' in html or '"logged_in": true' in html
    if not is_logged_in:
        _maybe_dump_debug_html(f"unauthorized_gamecards_{app_id}", html)
        raise ValueError(
            "Steam didn't recognize the session (not logged in on the gamecards page). "
            "Your session cookies have likely expired — re-enter them in Settings."
        )

    parser = _ProgressInfoParser()
    parser.feed(html)

    if parser.progress_text is not None:
        text = parser.progress_text.strip()
        if "no card drops remaining" in text.lower():
            return 0
        first_word = text.split(" ", 1)[0].strip()
        if first_word.isdigit():
            return int(first_word)

    if _NO_DROPS_TEXT_RE.search(html):
        return 0
    m = _DROPS_LEFT_TEXT_RE.search(html)
    if m:
        return int(m.group(1))

    # Page loaded and we're logged in, but no drop count found.
    # Normal for games without trading cards — return 0.
    if "gamecards" not in html.lower() and "badge" not in html.lower():
        _maybe_dump_debug_html(f"gamecards_{app_id}", html)
        raise ValueError(
            f"App {app_id}: gamecards page didn't look like a Steam page. "
            "Cookies may have expired or Steam returned an error."
        )
    return 0


class _BadgeParser(HTMLParser):
    """Best-effort bulk parser for the aggregate badges list, used only to
    pre-fill counts quickly for many games at once. Not authoritative — see
    module note above. Any app_id missing here should fall back to
    fetch_app_card_drops rather than being assumed to have 0 drops left.

    Steam renders one <a class="badge_row_overlay" href=".../gamecards/N/">
    per game, and — when there are drops left to earn, or explicitly none
    left — one <span class="progress_info_bold">...</span> somewhere after
    it. Rather than hand-tracking div nesting depth to know exactly where one
    game's block "ends" (fragile: any unexpected tag Steam adds throws off
    manual depth counting silently), this just attributes each
    progress_info_bold span to whichever badge_row_overlay anchor appeared
    most recently — which is equivalent in practice since Steam always emits
    them in that order. A game whose row has no such span at all is left out
    of the result entirely (unknown) rather than assumed to be zero, since
    that's not something this scrape can actually confirm."""

    def __init__(self):
        super().__init__()
        self.drops: dict[str, int] = {}
        self.is_authorized = False
        self.seen_ids_in_order: list[str] = []
        self._current_appid: str | None = None
        self._capture_next = False

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        cls  = attr.get("class", "")
        href = attr.get("href", "")

        if tag == "a" and "user_avatar" in cls:
            self.is_authorized = True

        if tag == "a" and "badge_row_overlay" in cls:
            m = re.search(r"/gamecards/(\d+)", href)
            if m:
                app_id = m.group(1)
                self._current_appid = app_id
                self.seen_ids_in_order.append(app_id)
                # Deliberately NOT setting a default here — see class docstring.

        if "progress_info_bold" in cls:
            self._capture_next = True

    def handle_data(self, data):
        if self._capture_next:
            self._capture_next = False
            text = data.strip()
            if "no card drops remaining" in text.lower():
                if self._current_appid:
                    self.drops[self._current_appid] = 0
                return
            first_word = text.split(" ", 1)[0] if text else ""
            if first_word.isdigit() and self._current_appid:
                self.drops[self._current_appid] = int(first_word)


def fetch_card_drops_bulk(session_id: str, login_secure: str, steam_id: str = "") -> dict[str, int]:
    """
    Best-effort scrape of the paginated badges list for many games at once.
    Only includes app_ids Steam actually chose to list there — callers should
    treat a missing app_id as "unknown", not "zero", and fall back to
    fetch_app_card_drops for anything that matters (see module note above).
    """
    cookies = {"sessionid": session_id, "steamLoginSecure": login_secure}
    base = (
        f"https://steamcommunity.com/profiles/{steam_id}/badges/?l=english"
        if steam_id else
        "https://steamcommunity.com/my/badges/?l=english"
    )
    all_drops: dict[str, int] = {}
    page = 1
    while True:
        html = _http_get(base + f"&p={page}", cookies=cookies)
        parser = _BadgeParser()
        parser.feed(html)
        if page == 1 and not parser.is_authorized:
            raise ValueError(
                "Steam didn't recognize the session on the badges page (no logged-in "
                "user found). Your session cookies have likely expired, re-enter them in Settings."
            )
        all_drops.update(parser.drops)
        if not parser.seen_ids_in_order and page > 1:
            break
        if f"p={page + 1}" not in html:
            break
        page += 1
    return all_drops


def _hidden_window_kwargs() -> dict:
    """On Windows, hide the child process window via STARTUPINFO SW_HIDE."""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0   # SW_HIDE
    return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}


# ---------------------------------------------------------------------------
# Idle process wrapper
# ---------------------------------------------------------------------------

class IdleProcess:
    def __init__(self, app_id: str):
        self.app_id = app_id
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        if self.is_running():
            return
        if not SAM_GAME_EXE.exists():
            raise FileNotFoundError(
                f"SAM.Game.exe not found at:\n{SAM_GAME_EXE}\n\n"
                "Place SAM.Game.exe and SAM.API.dll next to this script."
            )
        self._proc = subprocess.Popen(
            [str(SAM_GAME_EXE), self.app_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_hidden_window_kwargs(),
        )

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


# ---------------------------------------------------------------------------
# Status object (controller -> UI each tick)
# ---------------------------------------------------------------------------

class IdleStatus:
    def __init__(self):
        self.phase: str = ""
        self.active_game: str = ""
        self.active_app_id: str = ""
        self.phase1_running: list[str] = []
        self.elapsed_sec: float = 0.0
        self.next_check_sec: float = 0.0
        self.drops_checked: bool = False
        self.crash_notice: str = ""
        self.paused: bool = False


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class IdleController:
    def __init__(self, games: list, config: dict, on_update, on_status, on_log, on_done, on_auto_remove):
        self.games         = games
        self.config        = config
        self.on_update     = on_update
        self.on_status     = on_status
        self.on_log        = on_log
        self.on_done       = on_done
        self.on_auto_remove = on_auto_remove
        self._stop  = threading.Event()
        self._next  = threading.Event()
        self._procs: dict[str, IdleProcess] = {}
        self._status = IdleStatus()

    def stop(self):
        self._stop.set()
        self._next.set()
        self._kill_all()

    def advance_phase2(self):
        self._next.set()

    def _kill_all(self):
        for p in self._procs.values():
            p.stop()
        self._procs.clear()

    def _emit(self):
        self.on_status(self._status)

    def _log(self, msg: str):
        self.on_log(msg)

    def _start_idle(self, app_id: str):
        if app_id not in self._procs:
            self._procs[app_id] = IdleProcess(app_id)
        p = self._procs[app_id]
        if not p.is_running():
            p.start()

    def _stop_idle(self, app_id: str):
        p = self._procs.pop(app_id, None)
        if p:
            p.stop()

    def _is_idle_alive(self, app_id: str) -> bool:
        p = self._procs.get(app_id)
        return bool(p and p.is_running())

    def _restart_after_crash(self, app_id: str, name: str, retry_count: int) -> bool:
        """
        Try to bring a crashed idle process back up. Returns True if it's
        running again, False if retries are exhausted (caller should stop
        counting time for this game until the user intervenes).
        """
        if retry_count >= CRASH_MAX_RETRIES:
            return False
        self._log(
            f"{name} ({app_id}): SAM.Game.exe isn't running anymore. "
            f"Restart attempt {retry_count + 1}/{CRASH_MAX_RETRIES} in {CRASH_RETRY_BACKOFF}s..."
        )
        self._stop.wait(CRASH_RETRY_BACKOFF)
        if self._stop.is_set():
            return False
        try:
            self._procs.pop(app_id, None)
            self._start_idle(app_id)
        except Exception as exc:
            self._log(f"ERROR restarting {app_id}: {exc}")
            return False
        # Give the process a moment to actually come up before trusting it.
        self._stop.wait(2)
        alive = self._is_idle_alive(app_id)
        if alive:
            self._log(f"{name}: back up and idling again.")
        return alive

    def _has_cookies(self) -> bool:
        return bool(self.config.get("session_id") and self.config.get("login_secure"))

    def _check_drops(self, app_id: str) -> int:
        try:
            return fetch_app_card_drops(
                self.config["session_id"],
                self.config["login_secure"],
                app_id,
                self.config.get("steam_id", ""),
            )
        except Exception as exc:
            self._log(f"Drop check failed for {app_id}: {exc}")
            return -1

    # Phase 1 ---------------------------------------------------------------

    def _run_phase1(self):
        threshold_h = float(self.config.get("phase1_threshold_hours", 2.0))
        infinite    = threshold_h <= 0.0

        if infinite:
            targets = list(self.games)   # all games, never stop by time
        else:
            targets = [g for g in self.games if not g["phase1_done"]]

        if not targets:
            self._status.phase = "Phase 1 skipped (all games at threshold+)"
            self._emit()
            self._log("All games already past the threshold, skipping Phase 1.")
            return

        self._status.phase = "Phase 1" + (" (infinite)" if infinite else "")
        self._status.crash_notice = ""
        self._log(
            f"Phase 1: {len(targets)} game(s), "
            + ("running until manually stopped." if infinite else f"threshold = {threshold_h}h, running simultaneously.")
        )

        for g in targets:
            if self._stop.is_set():
                return
            try:
                self._start_idle(g["app_id"])
                self._log(f"Started: {g['name']} ({g['app_id']})")
            except Exception as exc:
                self._log(f"ERROR starting {g['app_id']}: {exc}")

        start_times    = {g["app_id"]: time.time() for g in targets}
        paused_secs    = {g["app_id"]: 0.0 for g in targets}
        crash_since    = {g["app_id"]: None for g in targets}
        retry_counts   = {g["app_id"]: 0 for g in targets}
        gave_up        = set()
        last_giveup_retry = {}
        last_crash_check  = time.time()

        while not self._stop.is_set():
            now = time.time()

            if now - last_crash_check >= CRASH_CHECK_INTERVAL:
                last_crash_check = now
                for g in targets:
                    app_id = g["app_id"]
                    if g["phase1_done"] and not infinite:
                        continue
                    alive = self._is_idle_alive(app_id)
                    if not alive and crash_since[app_id] is None:
                        crash_since[app_id] = now
                        self._status.crash_notice = f"{g['name']} stopped unexpectedly, attempting to restart..."
                        self._emit()
                    if not alive:
                        if app_id in gave_up:
                            if now - last_giveup_retry.get(app_id, 0) < CRASH_GIVEUP_RETRY_INTERVAL:
                                continue
                            last_giveup_retry[app_id] = now
                        recovered = self._restart_after_crash(app_id, g["name"], retry_counts[app_id])
                        retry_counts[app_id] += 1
                        if recovered:
                            paused_secs[app_id] += time.time() - crash_since[app_id]
                            crash_since[app_id] = None
                            retry_counts[app_id] = 0
                            gave_up.discard(app_id)
                            self._status.crash_notice = ""
                        elif retry_counts[app_id] >= CRASH_MAX_RETRIES and app_id not in gave_up:
                            gave_up.add(app_id)
                            last_giveup_retry[app_id] = now
                            self._status.crash_notice = (
                                f"{g['name']} isn't starting after {CRASH_MAX_RETRIES} tries. "
                                f"Still paused, will keep retrying every {CRASH_GIVEUP_RETRY_INTERVAL // 60} min."
                            )
                            self._log(
                                f"{g['name']}: giving up on quick retries. "
                                f"Will retry every {CRASH_GIVEUP_RETRY_INTERVAL // 60} min. Clock paused."
                            )
                            self._emit()

            still_going = []
            for g in targets:
                app_id = g["app_id"]
                if g["phase1_done"] and not infinite:
                    self._stop_idle(app_id)
                    continue
                if app_id in gave_up or crash_since[app_id] is not None:
                    still_going.append((g, None, None))
                    continue
                elapsed_h = (time.time() - start_times[app_id] - paused_secs[app_id]) / 3600

                if not infinite:
                    needed_h = max(0.0, threshold_h - g["playtime_hours"])
                    if elapsed_h >= needed_h:
                        # This game hit the threshold — stop it individually and wait for the rest
                        g["phase1_done"] = True
                        self._stop_idle(app_id)
                        self._log(f"{g['name']} reached {threshold_h}h mark, stopping.")
                        save_games(self.games)
                        self.on_update()
                        continue
                    still_going.append((g, elapsed_h, needed_h))
                else:
                    still_going.append((g, elapsed_h, None))

            self._status.phase1_running = [g["name"] for g, _, _ in still_going]

            if not still_going and not infinite:
                break

            # Time remaining = the LONGEST individual wait (bottleneck game)
            # since all games run simultaneously — sum is wrong.
            timed = [(eh, nh) for _, eh, nh in still_going if eh is not None and nh is not None]
            if timed:
                max_secs = max((nh - eh) * 3600 for eh, nh in timed)
                self._status.next_check_sec = max_secs
            else:
                self._status.next_check_sec = 0.0

            self._emit()
            self.on_update()
            self._stop.wait(1)   # 1-second tick so summary bar counts down live

        self._status.phase1_running = []
        if not infinite:
            self._log("Phase 1 complete.")
        save_games(self.games)
        self.on_update()

    # Phase 2 ---------------------------------------------------------------

    def _run_phase2(self):
        targets = [g for g in self.games if not g["cards_done"]]
        if not targets:
            self._status.phase = "Phase 2 skipped (all cards done)"
            self._emit()
            self._log("No games need Phase 2.")
            return

        self._log(f"Phase 2: {len(targets)} game(s) to card-idle.")

        for g in targets:
            if self._stop.is_set():
                return

            self._next.clear()
            app_id = g["app_id"]
            self._status.phase         = "Phase 2"
            self._status.active_game   = g["name"]
            self._status.active_app_id = app_id
            self._status.elapsed_sec   = 0.0
            self._status.drops_checked = False
            self._status.crash_notice  = ""

            if self._has_cookies():
                drops = self._check_drops(app_id)
                g["cards_remaining"] = drops
                save_games(self.games)
                self.on_update()
                if drops == 0:
                    self._log(f"{g['name']}: 0 drops remaining, skipping.")
                    g["cards_done"] = True
                    save_games(self.games)
                    self.on_update()
                    continue
                self._log(
                    f"Idling {g['name']}: "
                    f"{drops if drops >= 0 else '?'} drop(s) remaining."
                )
            else:
                self._log(f"Idling {g['name']} (no cookies, drop count unknown).")

            self._emit()

            try:
                self._start_idle(app_id)
            except Exception as exc:
                self._log(f"ERROR starting {app_id}: {exc}")
                continue

            game_start        = time.time()
            last_poll         = time.time()
            poll_sec          = max(1.0, float(self.config.get("phase2_poll_minutes", PHASE2_CARD_POLL_MIN))) * 60
            paused_secs       = 0.0   # total crash time since game_start, for elapsed_sec
            paused_since_poll = 0.0   # crash time since last_poll, for the poll countdown
            crash_since  = None
            retry_count  = 0
            gave_up      = False
            last_giveup_retry = 0.0
            last_crash_check  = time.time()

            while not self._stop.is_set() and not self._next.is_set():
                self._stop.wait(1)
                now = time.time()

                if now - last_crash_check >= CRASH_CHECK_INTERVAL:
                    last_crash_check = now
                    alive = self._is_idle_alive(app_id)
                    if not alive and crash_since is None:
                        crash_since = now
                        self._status.crash_notice = f"{g['name']} stopped unexpectedly, attempting to restart..."
                        self._emit()
                    if not alive:
                        should_try = True
                        if gave_up:
                            if now - last_giveup_retry < CRASH_GIVEUP_RETRY_INTERVAL:
                                should_try = False
                            else:
                                last_giveup_retry = now
                        if should_try:
                            recovered = self._restart_after_crash(app_id, g["name"], retry_count)
                            retry_count += 1
                            if recovered:
                                gap = time.time() - crash_since
                                paused_secs       += gap
                                paused_since_poll += gap
                                crash_since = None
                                retry_count = 0
                                gave_up = False
                                self._status.crash_notice = ""
                            elif retry_count >= CRASH_MAX_RETRIES and not gave_up:
                                gave_up = True
                                last_giveup_retry = now
                                self._status.crash_notice = (
                                    f"{g['name']} isn't starting after {CRASH_MAX_RETRIES} tries. "
                                    f"Still retrying every {CRASH_GIVEUP_RETRY_INTERVAL // 60} min, "
                                    "time isn't counting meanwhile."
                                )
                                self._log(
                                    f"{g['name']}: giving up on quick retries after {CRASH_MAX_RETRIES} attempts. "
                                    f"Will keep trying every {CRASH_GIVEUP_RETRY_INTERVAL // 60} min. "
                                    "Not counted as failed, its timer is just paused."
                                )
                                self._emit()

                if crash_since is not None:
                    # Paused: don't advance the displayed timers or the drop-check clock.
                    self._emit()
                    continue

                self._status.elapsed_sec    = now - game_start - paused_secs
                self._status.next_check_sec = max(0.0, poll_sec - (now - last_poll - paused_since_poll))
                self._emit()

                if self._has_cookies() and (now - last_poll - paused_since_poll) >= poll_sec:
                    last_poll = now
                    paused_since_poll = 0.0
                    drops = self._check_drops(app_id)
                    g["cards_remaining"] = drops
                    self._status.drops_checked = True
                    save_games(self.games)
                    self.on_update()
                    if drops == 0:
                        self._log(f"{g['name']}: 0 drops remaining, moving on.")
                        self._next.set()
                        break
                    elif drops > 0:
                        self._log(f"{g['name']}: {drops} drop(s) still remaining.")
                    self._status.drops_checked = False

            self._stop_idle(app_id)

            if self._next.is_set() and not self._stop.is_set():
                g["cards_done"]      = True
                g["cards_remaining"] = 0
                self._log(f"Cards done: {g['name']}.")
                save_games(self.games)
                self.on_update()
                # Auto-remove if configured
                if self.config.get("auto_remove_completed", False):
                    self.on_auto_remove(g["app_id"])

        self._status.active_game   = ""
        self._status.active_app_id = ""
        self._emit()
        self._log("Phase 2 complete.")
        save_games(self.games)
        self.on_update()

    # Entry -----------------------------------------------------------------

    def run(self):
        try:
            self._run_phase1()
            if not self._stop.is_set():
                self._run_phase2()
            if not self._stop.is_set():
                self._status.phase       = "All done"
                self._status.active_game = ""
                self._emit()
                self._log("All phases complete.")
                self.on_done()
        except Exception as exc:
            self._status.phase = f"Error: {exc}"
            self._emit()
            self._log(f"FATAL: {exc}")
        finally:
            self._kill_all()


# ---------------------------------------------------------------------------
# Colours / fonts
# ---------------------------------------------------------------------------

BG       = "#1e1e1e"
FG       = "#e0e0e0"
ACCENT   = "#3a7ebf"
ROW_ODD  = "#252525"
ROW_EVEN = "#2b2b2b"
ENTRY_BG = "#2d2d2d"
BTN_BG   = "#333333"
GREEN    = "#4caf50"
ORANGE   = "#ff9800"
RED      = "#f44336"
GREY     = "#888888"
PANEL_BG = "#242424"
WARN     = "#ffb74d"

FONT  = ("Segoe UI", 10)
BOLD  = ("Segoe UI", 10, "bold")
MONO  = ("Consolas", 9)
TITLE = ("Segoe UI", 13, "bold")
SMALL = ("Segoe UI", 8)
BIG   = ("Segoe UI", 11, "bold")


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


_WORD_BOUNDARY_RE = re.compile(r"\s*\S+\s*$")   # trailing run of non-space + its leading whitespace
_WORD_FORWARD_RE  = re.compile(r"^\s*\S+\s*")   # leading run of non-space + its trailing whitespace


def bind_word_delete(entry: tk.Entry) -> None:
    """
    Add Ctrl+Backspace (delete previous word) and Ctrl+Delete (delete next
    word) to a Tk Entry widget. Stock Tk's default Entry bindings don't
    include this (only plain Backspace/Delete and Control-h/Control-d for
    single characters), so every text field needs it added explicitly to get
    the word-delete behaviour people expect from other apps.
    """
    def _delete_word_back(event):
        if entry.selection_present():
            entry.delete("sel.first", "sel.last")
            return "break"
        pos = entry.index("insert")
        text_before = entry.get()[:pos]
        m = _WORD_BOUNDARY_RE.search(text_before)
        start = m.start() if m else 0
        entry.delete(start, pos)
        return "break"

    def _delete_word_forward(event):
        if entry.selection_present():
            entry.delete("sel.first", "sel.last")
            return "break"
        pos = entry.index("insert")
        text_after = entry.get()[pos:]
        m = _WORD_FORWARD_RE.match(text_after)
        end = pos + (m.end() if m else 0)
        entry.delete(pos, end)
        return "break"

    entry.bind("<Control-BackSpace>", _delete_word_back)
    entry.bind("<Control-Delete>",    _delete_word_forward)


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, config: dict, unit_var: tk.StringVar):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.result: dict | None = None
        self._cfg = config.copy()
        self._unit_var = unit_var
        self._hide_vars: dict[str, tk.BooleanVar] = {}
        self._build()
        self.transient(parent)
        self.wait_window()

    def _field(self, label, key, row, hideable: str | None = None, extra_btn=None):
        """
        hideable: config key for the hide boolean (e.g. 'hide_api_key').
                  If given, a Hide checkbox is appended and the entry toggles show='*'.
        """
        tk.Label(self, text=label, bg=BG, fg=FG, font=FONT, anchor="w").grid(
            row=row, column=0, sticky="w", padx=(16, 8), pady=6
        )
        var = tk.StringVar(value=self._cfg.get(key, ""))
        entry_frame = tk.Frame(self, bg=BG)
        entry_frame.grid(row=row, column=1, padx=(0, 16), pady=6, sticky="w")

        initial_show = "*" if (hideable and self._cfg.get(hideable, True)) else ""
        entry = tk.Entry(
            entry_frame, textvariable=var, bg=ENTRY_BG, fg=FG, font=FONT,
            relief="flat", insertbackground=FG, width=30,
            show=initial_show,
            # Allow normal clipboard shortcuts
            exportselection=True,
        )
        entry.pack(side="left")

        # Right-click context menu for copy/cut/paste/select all
        def _make_menu(e):
            m = tk.Menu(entry, tearoff=0, bg=BTN_BG, fg=FG,
                        activebackground=ACCENT, activeforeground="#fff",
                        relief="flat", bd=0)
            m.add_command(label="Cut",        command=lambda: entry.event_generate("<<Cut>>"))
            m.add_command(label="Copy",       command=lambda: entry.event_generate("<<Copy>>"))
            m.add_command(label="Paste",      command=lambda: entry.event_generate("<<Paste>>"))
            m.add_separator()
            m.add_command(label="Select All", command=lambda: (entry.select_range(0, "end"), entry.focus_set()))
            m.post(e.x_root, e.y_root)
        entry.bind("<Button-3>", _make_menu)
        # Ctrl+A to select all
        entry.bind("<Control-a>", lambda e: (entry.select_range(0, "end"), "break"))
        entry.bind("<Control-A>", lambda e: (entry.select_range(0, "end"), "break"))
        # Unfocus on Escape
        entry.bind("<Escape>", lambda e: self.focus_set())
        bind_word_delete(entry)

        if hideable:
            hide_var = tk.BooleanVar(value=self._cfg.get(hideable, True))
            self._hide_vars[hideable] = hide_var

            def _toggle_show(*_):
                entry.config(show="*" if hide_var.get() else "")

            hide_var.trace_add("write", _toggle_show)
            tk.Checkbutton(
                entry_frame, text="Hide", variable=hide_var,
                bg=BG, fg=GREY, selectcolor=BTN_BG,
                activebackground=BG, font=SMALL,
            ).pack(side="left", padx=(8, 0))

        if extra_btn:
            text, cmd = extra_btn
            tk.Button(entry_frame, text=text, bg=BTN_BG, fg=FG, font=SMALL,
                      relief="flat", padx=6, pady=3, cursor="hand2", bd=0,
                      command=cmd).pack(side="left", padx=(6, 0))
        return var

    def _link(self, parent, text, url, **grid_kw):
        lbl = tk.Label(parent, text=text, bg=BG, fg=ACCENT, font=SMALL,
                        cursor="hand2", anchor="w")
        lbl.grid(**grid_kw)
        lbl.bind("<Button-1>", lambda e: webbrowser.open(url))
        return lbl

    def _build(self):
        tk.Label(self, text="Settings", font=TITLE, bg=BG, fg=ACCENT).grid(
            row=0, column=0, columnspan=2, padx=16, pady=(14, 6), sticky="w"
        )
        tk.Label(self, text="Steam Web API  (library import, playtime)",
                 bg=BG, fg=GREY, font=SMALL).grid(
            row=1, column=0, columnspan=2, padx=16, sticky="w")
        self._api_key_var = self._field("API Key", "api_key", 2, hideable="hide_api_key")
        self._link(self, "Get an API key",
                    "https://steamcommunity.com/dev/apikey",
                    row=3, column=0, columnspan=2, padx=16, sticky="w")

        self._steam_id_var = self._field(
            "Steam ID / vanity name", "steam_id", 4,
            extra_btn=("Look up", self._lookup_steam_id),
        )
        tk.Label(self, text="Paste your 64-bit ID, your profile URL, or just your vanity name, then click Look up.",
                 bg=BG, fg=GREY, font=SMALL, wraplength=420, justify="left").grid(
            row=5, column=0, columnspan=2, padx=16, sticky="w")

        tk.Label(self, text="Session cookies  (automatic card-drop detection, optional)",
                 bg=BG, fg=GREY, font=SMALL).grid(
            row=6, column=0, columnspan=2, padx=16, pady=(10, 0), sticky="w")
        tk.Label(self,
                 text="Log into steamcommunity.com in your browser first, then open the cookies page below,\n"
                      "find 'sessionid' and 'steamLoginSecure', and copy each Value into the boxes here.",
                 bg=BG, fg=GREY, font=SMALL, justify="left").grid(
            row=7, column=0, columnspan=2, padx=16, sticky="w")
        self._link(self, "Open steamcommunity.com cookies page",
                    "https://steamcommunity.com/",
                    row=8, column=0, columnspan=2, padx=16, pady=(0, 4), sticky="w")
        self._session_var = self._field("sessionid",         "session_id",   9)
        self._login_var   = self._field("steamLoginSecure",  "login_secure", 10, hideable="hide_login_secure")
        tk.Label(self, text="Don't have these? Leave them blank — use \"Cards Dropped (manual)\" instead.",
                 bg=BG, fg=GREY, font=SMALL).grid(
            row=11, column=0, columnspan=2, padx=16, pady=(0, 4), sticky="w")

        # Display / behaviour options
        tk.Label(self, text="Display & behaviour",
                 bg=BG, fg=GREY, font=SMALL).grid(
            row=12, column=0, columnspan=2, padx=16, pady=(12, 2), sticky="w")

        # Playtime unit
        unit_row = tk.Frame(self, bg=BG)
        unit_row.grid(row=13, column=0, columnspan=2, padx=16, pady=(0, 4), sticky="w")
        tk.Label(unit_row, text="Playtime unit:", bg=BG, fg=FG, font=FONT).pack(side="left", padx=(0, 8))
        self._unit_cb = ttk.Combobox(
            unit_row, textvariable=self._unit_var,
            values=UNITS, state="readonly", width=10, font=FONT,
        )
        self._unit_cb.pack(side="left")
        tk.Label(unit_row, text="(affects table, input fields, and summary bar)",
                 bg=BG, fg=GREY, font=SMALL).pack(side="left", padx=(10, 0))

        # Phase 1 threshold
        thresh_row = tk.Frame(self, bg=BG)
        thresh_row.grid(row=14, column=0, columnspan=2, padx=16, pady=(0, 4), sticky="w")
        tk.Label(thresh_row, text="Phase 1 stops each game at:", bg=BG, fg=FG, font=FONT).pack(side="left", padx=(0, 8))
        self._thresh_var = tk.StringVar(
            value=str(self._cfg.get("phase1_threshold_hours", 2.0))
        )
        thresh_entry = tk.Entry(thresh_row, textvariable=self._thresh_var, bg=ENTRY_BG, fg=FG,
                                 font=FONT, relief="flat", insertbackground=FG, width=6)
        thresh_entry.pack(side="left")
        bind_word_delete(thresh_entry)
        tk.Label(thresh_row, text="hours  (set to 0 for infinite — Phase 1 never auto-stops)",
                 bg=BG, fg=GREY, font=SMALL).pack(side="left", padx=(6, 0))

        # Phase 2 drop-check interval
        poll_row = tk.Frame(self, bg=BG)
        poll_row.grid(row=15, column=0, columnspan=2, padx=16, pady=(0, 4), sticky="w")
        tk.Label(poll_row, text="Check for drops every:", bg=BG, fg=FG, font=FONT).pack(side="left", padx=(0, 8))
        self._poll_var = tk.StringVar(
            value=str(self._cfg.get("phase2_poll_minutes", PHASE2_CARD_POLL_MIN))
        )
        poll_entry = tk.Entry(poll_row, textvariable=self._poll_var, bg=ENTRY_BG, fg=FG,
                               font=FONT, relief="flat", insertbackground=FG, width=6)
        poll_entry.pack(side="left")
        bind_word_delete(poll_entry)
        tk.Label(poll_row, text="minutes  (Phase 2, requires session cookies)",
                 bg=BG, fg=GREY, font=SMALL).pack(side="left", padx=(6, 0))

        # Merge refresh buttons
        self._merge_refresh_var = tk.BooleanVar(value=self._cfg.get("merge_refresh_buttons", False))
        tk.Checkbutton(
            self,
            text='Merge "Refresh Drops" and "Refresh Playtimes" into a single "Refresh" button',
            variable=self._merge_refresh_var,
            bg=BG, fg=FG, selectcolor=BTN_BG, activebackground=BG, font=FONT,
        ).grid(row=16, column=0, columnspan=2, padx=16, pady=(2, 2), sticky="w")

        # Auto-remove completed
        self._auto_remove_var = tk.BooleanVar(value=self._cfg.get("auto_remove_completed", False))
        tk.Checkbutton(
            self,
            text="Automatically remove a game from the list once all its cards are dropped",
            variable=self._auto_remove_var,
            bg=BG, fg=FG, selectcolor=BTN_BG, activebackground=BG, font=FONT,
        ).grid(row=17, column=0, columnspan=2, padx=16, pady=(2, 4), sticky="w")

        bf = tk.Frame(self, bg=BG)
        bf.grid(row=18, column=0, columnspan=2, pady=(12, 16), padx=16, sticky="e")
        tk.Button(bf, text="Save",   bg=ACCENT, fg="#fff", font=FONT, relief="flat",
                  padx=10, pady=5, cursor="hand2", bd=0, command=self._save
                  ).pack(side="right", padx=(6, 0))
        tk.Button(bf, text="Cancel", bg=BTN_BG, fg=FG,    font=FONT, relief="flat",
                  padx=10, pady=5, cursor="hand2", bd=0, command=self.destroy
                  ).pack(side="right")

    def _lookup_steam_id(self):
        key = self._api_key_var.get().strip()
        text = self._steam_id_var.get().strip()
        if not key:
            messagebox.showinfo("API key needed", "Enter your Steam Web API key first, then click Look up.")
            return
        if not text:
            messagebox.showinfo("Nothing to look up", "Paste your profile URL or vanity name into the Steam ID box first.")
            return
        try:
            resolved = resolve_steam_id(key, text)
        except Exception as exc:
            messagebox.showerror("Lookup failed", str(exc))
            return
        self._steam_id_var.set(resolved)
        messagebox.showinfo("Found it", f"Resolved to Steam ID: {resolved}")

    def _save(self):
        try:
            thresh = float(self._thresh_var.get().strip().replace(",", "."))
        except ValueError:
            thresh = 2.0
        try:
            poll_minutes = float(self._poll_var.get().strip().replace(",", "."))
            if poll_minutes <= 0:
                raise ValueError
        except ValueError:
            poll_minutes = PHASE2_CARD_POLL_MIN
        self.result = {
            "api_key":                 self._api_key_var.get().strip(),
            "steam_id":                self._steam_id_var.get().strip(),
            "session_id":              self._session_var.get().strip(),
            "login_secure":            self._login_var.get().strip(),
            "playtime_unit":           self._unit_var.get(),
            "phase1_threshold_hours":  thresh,
            "phase2_poll_minutes":     poll_minutes,
            "merge_refresh_buttons":   self._merge_refresh_var.get(),
            "auto_remove_completed":   self._auto_remove_var.get(),
            "hide_api_key":            self._hide_vars.get("hide_api_key",      tk.BooleanVar(value=True)).get(),
            "hide_login_secure":       self._hide_vars.get("hide_login_secure", tk.BooleanVar(value=True)).get(),
        }
        self.destroy()


# ---------------------------------------------------------------------------
# Import dialog
# ---------------------------------------------------------------------------

class ImportDialog(tk.Toplevel):
    def __init__(self, parent, games: list, existing_ids: set, unit: str = "minutes"):
        super().__init__(parent)
        self.title("Import Games")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.geometry("780x580")
        self.grab_set()
        self.selected: list[dict] = []
        self._games      = games
        self._existing   = existing_ids
        self._unit       = unit
        # One persistent widget row per game, built once. Filtering/sorting
        # only shows/hides and re-orders these instead of destroying and
        # rebuilding them, so it stays fast and never risks losing selection
        # state to a teardown mid-edit.
        self._row_widgets: dict[str, dict] = {}   # app_id -> {"frame":..., "var":..., "drop_lbl":...}
        self._check_state: dict[str, tk.BooleanVar] = {}
        self._sort_key  = "default"   # "default" | "name" | "playtime" | "drops"
        self._sort_desc = False       # False = increasing, True = decreasing
        self._build()
        self.transient(parent)
        self.wait_window()

    def _build(self):
        tk.Label(self, text="Select games to add", font=BOLD, bg=BG, fg=FG
                 ).pack(padx=16, pady=(12, 0), anchor="w")
        tk.Label(self, text="Grey = already in list.", font=SMALL, bg=BG, fg=GREY
                 ).pack(padx=16, anchor="w")

        # Filter / sort row
        ff = tk.Frame(self, bg=BG)
        ff.pack(fill="x", padx=16, pady=(8, 4))
        tk.Label(ff, text="Filter:", bg=BG, fg=FG, font=FONT).pack(side="left")
        self._filter_var = tk.StringVar()
        self._filter_after_id = None
        self._filter_var.trace_add("write", self._on_filter_changed)
        filter_entry = tk.Entry(ff, textvariable=self._filter_var, bg=ENTRY_BG, fg=FG, font=FONT,
                                 relief="flat", insertbackground=FG, width=22)
        filter_entry.pack(side="left", padx=(6, 0))
        filter_entry.focus_set()
        bind_word_delete(filter_entry)
        self._filter_count_lbl = tk.Label(ff, text="", bg=BG, fg=GREY, font=SMALL)
        self._filter_count_lbl.pack(side="left", padx=(6, 0))

        self._sub2h_var = tk.BooleanVar(value=False)
        tk.Checkbutton(ff, text="Only under 2h", variable=self._sub2h_var,
                       bg=BG, fg=FG, selectcolor=BTN_BG, font=FONT, activebackground=BG,
                       command=self._apply_filter_sort).pack(side="left", padx=(10, 0))

        tk.Label(ff, text="Sort:", bg=BG, fg=GREY, font=SMALL).pack(side="left", padx=(12, 4))
        self._sort_var = tk.StringVar(value="default")
        for val, label in (("default", "App ID"), ("name", "Name"),
                            ("playtime", "Playtime"), ("drops", "Drops")):
            tk.Radiobutton(
                ff, text=label, variable=self._sort_var, value=val,
                bg=BG, fg=FG, selectcolor=BTN_BG, activebackground=BG, font=SMALL,
                command=self._apply_filter_sort,
            ).pack(side="left", padx=(0, 4))

        self._sort_dir_btn = tk.Button(
            ff, text="↑ Increasing", bg=BTN_BG, fg=FG, font=SMALL, relief="flat",
            padx=6, pady=2, cursor="hand2", bd=0, command=self._toggle_sort_dir,
        )
        self._sort_dir_btn.pack(side="left", padx=(8, 0))

        # Scrollable list
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        self._canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._inner = tk.Frame(self._canvas, bg=BG)
        self._win_id = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(self._win_id, width=e.width))

        # Mouse wheel scrolling: bind at the toplevel level via bind_all so it
        # fires no matter which child widget (checkbox, label, row frame) the
        # cursor happens to be over, then unbind when this window closes so it
        # doesn't leak onto the rest of the app. Per-widget binding (the old
        # approach) silently misses whichever widgets the row actually
        # contains, which is most of the row's clickable area.
        def _on_wheel(event):
            if event.num == 4 or getattr(event, "delta", 0) > 0:
                self._canvas.yview_scroll(-1, "units")
            elif event.num == 5 or getattr(event, "delta", 0) < 0:
                self._canvas.yview_scroll(1, "units")
        self.bind_all("<MouseWheel>", _on_wheel)
        self.bind_all("<Button-4>",   _on_wheel)
        self.bind_all("<Button-5>",   _on_wheel)
        self.bind("<Destroy>", self._cleanup_wheel_bindings)

        self._build_all_rows()
        self._apply_filter_sort()

        # Buttons row
        bf = tk.Frame(self, bg=BG)
        bf.pack(fill="x", padx=16, pady=(0, 14))
        tk.Button(bf, text="Select All",        bg=BTN_BG, fg=FG, font=FONT, relief="flat",
                  padx=8, pady=4, cursor="hand2", bd=0,
                  command=self._select_all).pack(side="left", padx=(0, 4))
        tk.Button(bf, text="Select None",       bg=BTN_BG, fg=FG, font=FONT, relief="flat",
                  padx=8, pady=4, cursor="hand2", bd=0,
                  command=self._select_none).pack(side="left", padx=(0, 4))
        tk.Button(bf, text="Invert",            bg=BTN_BG, fg=FG, font=FONT, relief="flat",
                  padx=8, pady=4, cursor="hand2", bd=0,
                  command=self._invert).pack(side="left", padx=(0, 4))
        tk.Button(bf, text="Select with drops", bg=BTN_BG, fg=FG, font=FONT, relief="flat",
                  padx=8, pady=4, cursor="hand2", bd=0,
                  command=self._select_with_drops).pack(side="left")
        tk.Label(bf, text="(applies to all games, not just visible)", bg=BG, fg=GREY, font=SMALL
                 ).pack(side="left", padx=(8, 0))
        tk.Button(bf, text="Add Selected",      bg=ACCENT,  fg="#fff", font=FONT, relief="flat",
                  padx=10, pady=5, cursor="hand2", bd=0,
                  command=self._confirm).pack(side="right")

    def _cleanup_wheel_bindings(self, event=None):
        if event is not None and event.widget is not self:
            return
        try:
            self.unbind_all("<MouseWheel>")
            self.unbind_all("<Button-4>")
            self.unbind_all("<Button-5>")
        except tk.TclError:
            pass

    def _on_filter_changed(self, *_):
        # Debounce: typing fires this on every keystroke, but re-filtering
        # (show/hide only, no widget rebuild) is cheap enough that a short
        # debounce is just to avoid redundant work while typing fast.
        if self._filter_after_id is not None:
            self.after_cancel(self._filter_after_id)
        self._filter_after_id = self.after(120, self._apply_filter_sort)

    def _toggle_sort_dir(self):
        self._sort_desc = not self._sort_desc
        self._sort_dir_btn.config(text="↓ Decreasing" if self._sort_desc else "↑ Increasing")
        self._apply_filter_sort()

    def _build_all_rows(self):
        """Create one persistent widget row per game, hidden until _apply_filter_sort places them."""
        for g in self._games:
            app_id  = g["app_id"]
            already = app_id in self._existing
            var = tk.BooleanVar(value=already)
            self._check_state[app_id] = var

            row_bg   = ROW_ODD
            fg_color = GREY if already else FG
            f = tk.Frame(self._inner, bg=row_bg)

            cb = tk.Checkbutton(f, variable=var, bg=row_bg, fg=fg_color,
                                 selectcolor=BTN_BG, activebackground=row_bg)
            cb.pack(side="left", padx=(6, 0))
            tk.Label(f, text=g["name"], bg=row_bg, fg=fg_color,
                     font=FONT, anchor="w", width=34).pack(side="left", padx=4)

            pt_display = hours_to_unit(g["playtime_hours"], self._unit)
            tk.Label(f, text=f"{pt_display:.1f} {self._unit}",
                     bg=row_bg, fg=GREY, font=FONT, width=13, anchor="e").pack(side="left")

            drops = g.get("cards_remaining", -1)
            drops_str = str(drops) if drops >= 0 else "?"
            drops_color = GREEN if drops == 0 else (ORANGE if drops > 0 else GREY)
            drop_lbl = tk.Label(f, text=f"{drops_str} drops left",
                                 bg=row_bg, fg=drops_color, font=FONT, width=13, anchor="e")
            drop_lbl.pack(side="left", padx=(4, 6))

            self._row_widgets[app_id] = {
                "frame": f, "var": var, "game": g, "row_bg_even": ROW_ODD, "row_bg_odd": ROW_EVEN,
            }

    def _sorted_games(self) -> list[dict]:
        key = self._sort_var.get()
        if key == "name":
            games = sorted(self._games, key=lambda g: g["name"].lower())
        elif key == "playtime":
            games = sorted(self._games, key=lambda g: g["playtime_hours"])
        elif key == "drops":
            # Unknown (-1) isn't a real quantity to rank by, so keep it
            # pinned to the end regardless of direction: sort known values
            # normally, then reverse only that portion, then append unknowns.
            known   = [g for g in self._games if g.get("cards_remaining", -1) >= 0]
            unknown = [g for g in self._games if g.get("cards_remaining", -1) < 0]
            known.sort(key=lambda g: g["cards_remaining"])
            if self._sort_desc:
                known.reverse()
            return known + unknown
        else:
            games = list(self._games)   # "default" = original order (by app_id from API)
        if self._sort_desc:
            games.reverse()
        return games

    def _apply_filter_sort(self):
        ftext = self._filter_var.get().lower().strip()
        sub2h = self._sub2h_var.get()

        # Un-pack everything first so pack order can be rebuilt cleanly.
        for w in self._row_widgets.values():
            w["frame"].pack_forget()

        visible_count = 0
        for g in self._sorted_games():
            app_id = g["app_id"]
            if ftext and ftext not in g["name"].lower() and ftext not in app_id:
                continue
            if sub2h and g["playtime_hours"] >= 2.0:
                continue
            row = self._row_widgets[app_id]
            row_bg = row["row_bg_even"] if visible_count % 2 == 0 else row["row_bg_odd"]
            row["frame"].configure(bg=row_bg)
            for child in row["frame"].winfo_children():
                try:
                    child.configure(bg=row_bg)
                except tk.TclError:
                    pass
            row["frame"].pack(fill="x")
            visible_count += 1

        total = len(self._row_widgets)
        if ftext or sub2h:
            self._filter_count_lbl.config(text=f"{visible_count}/{total}")
        else:
            self._filter_count_lbl.config(text="")

    # Selection helpers — these always act on every game, not just what the
    # current filter happens to be showing, since "select all" silently only
    # selecting the visible subset is exactly the kind of surprise that made
    # this menu confusing to use in the first place.
    def _select_all(self):
        for var in self._check_state.values():
            var.set(True)

    def _select_none(self):
        for var in self._check_state.values():
            var.set(False)

    def _invert(self):
        for var in self._check_state.values():
            var.set(not var.get())

    def _select_with_drops(self):
        for row in self._row_widgets.values():
            if row["game"].get("cards_remaining", -1) > 0:
                row["var"].set(True)

    def _confirm(self):
        self.selected = [row["game"] for row in self._row_widgets.values() if row["var"].get()]
        self.destroy()


# ---------------------------------------------------------------------------
# Inline cell editor for the Treeview
# ---------------------------------------------------------------------------

class _CellEditor(tk.Entry):
    """
    A temporary Entry widget that pops up over a Treeview cell to allow
    inline editing. Commits on Return or focus-out, cancels on Escape.
    """
    def __init__(self, tree: ttk.Treeview, iid: str, col: str, current_val: str, on_commit):
        self._tree     = tree
        self._iid      = iid
        self._col      = col
        self._on_commit = on_commit

        # Position over the cell
        bbox = tree.bbox(iid, col)
        if not bbox:
            return
        x, y, w, h = bbox

        super().__init__(tree, font=FONT, bg=ENTRY_BG, fg=FG,
                         insertbackground=FG, relief="flat", bd=1)
        self.place(x=x, y=y, width=w, height=h)
        self.insert(0, current_val)
        self.select_range(0, "end")
        self.focus_set()

        self.bind("<Return>",    self._commit)
        self.bind("<KP_Enter>",  self._commit)
        self.bind("<Escape>",    lambda e: self.destroy())
        self.bind("<FocusOut>",  self._commit)
        bind_word_delete(self)

    def _commit(self, event=None):
        val = self.get()
        self.destroy()
        self._on_commit(self._iid, self._col, val)


# ---------------------------------------------------------------------------
# Status panel
# ---------------------------------------------------------------------------

class StatusPanel(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=PANEL_BG, pady=8, padx=14)
        self._build()

    def _lbl(self, row, col, text="", fg=FG, font=FONT, columnspan=1):
        l = tk.Label(self, bg=PANEL_BG, fg=fg, font=font, anchor="w", text=text)
        l.grid(row=row, column=col, sticky="w", padx=(0, 16), pady=1,
               columnspan=columnspan)
        return l

    def _build(self):
        self.columnconfigure(1, weight=1)
        self.columnconfigure(3, weight=1)

        self._lbl(0, 0, "Phase",           fg=GREY, font=SMALL)
        self._phase_val   = self._lbl(0, 1, font=BOLD)
        self._lbl(0, 2, "Currently idling", fg=GREY, font=SMALL)
        self._game_val    = self._lbl(0, 3, fg=ORANGE, font=BOLD)

        self._lbl(1, 0, "Time on game",    fg=GREY, font=SMALL)
        self._elapsed_val = self._lbl(1, 1, font=MONO)
        self._lbl(1, 2, "Next drop check", fg=GREY, font=SMALL)
        self._check_val   = self._lbl(1, 3, font=MONO)

        self._lbl(2, 0, "Running (Phase 1)", fg=GREY, font=SMALL)
        self._p1list_val  = self._lbl(2, 1, font=SMALL, columnspan=3)

        self._crash_val = self._lbl(3, 0, fg=WARN, font=SMALL, columnspan=4)

    def update_status(self, st: IdleStatus, running: bool):
        if not running:
            self._phase_val.config(text="Idle", fg=GREY)
            self._game_val.config(text="")
            self._elapsed_val.config(text="")
            self._check_val.config(text="")
            self._p1list_val.config(text="")
            self._crash_val.config(text="")
            return

        self._phase_val.config(text=st.phase or "", fg=FG)

        if st.active_game:
            self._game_val.config(text=st.active_game, fg=ORANGE)
            self._elapsed_val.config(text=_fmt_time(st.elapsed_sec))
            if st.drops_checked:
                self._check_val.config(text="checking...")
            elif st.next_check_sec > 0:
                self._check_val.config(text=_fmt_time(st.next_check_sec))
            else:
                self._check_val.config(text="n/a (no cookies)")
        else:
            self._game_val.config(text="")
            self._elapsed_val.config(text="")
            self._check_val.config(text="")

        if st.phase1_running:
            names = ", ".join(st.phase1_running[:5])
            if len(st.phase1_running) > 5:
                names += f" +{len(st.phase1_running) - 5} more"
            self._p1list_val.config(text=names)
        else:
            self._p1list_val.config(text="")

        self._crash_val.config(text=st.crash_notice or "")


# ---------------------------------------------------------------------------
# Summary bar
# ---------------------------------------------------------------------------

class SummaryBar(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=PANEL_BG, pady=6, padx=14)
        self._build()

    def _stat(self, col, label):
        tk.Label(self, text=label, bg=PANEL_BG, fg=GREY, font=SMALL).grid(
            row=0, column=col, sticky="w", padx=(0, 4))
        val = tk.Label(self, text="", bg=PANEL_BG, fg=FG, font=BIG)
        val.grid(row=1, column=col, sticky="w", padx=(0, 28))
        return val

    def _build(self):
        self._total_drops = self._stat(0, "Total drops left")
        self._total_pt    = self._stat(1, "Phase 1 time left")
        self._games_p1    = self._stat(2, "In phase 1")
        self._games_p2    = self._stat(3, "In phase 2")
        self._games_done  = self._stat(4, "Done")

    def refresh(self, games: list, unit: str = "minutes", threshold_h: float = 2.0, phase1_remaining_sec: float | None = None):
        total_drops = sum(g["cards_remaining"] for g in games if g["cards_remaining"] > 0)
        p1   = sum(1 for g in games if not g["phase1_done"])
        p2   = sum(1 for g in games if g["phase1_done"] and not g["cards_done"])
        done = sum(1 for g in games if g["cards_done"])

        # Phase 1 time left = LONGEST individual wait (bottleneck), not sum.
        # Games run simultaneously so the total wait is max(), not sum().
        if phase1_remaining_sec is not None:
            disp_val = hours_to_unit(phase1_remaining_sec / 3600, unit)
        else:
            max_h = max(
                (max(0.0, threshold_h - g["playtime_hours"]) for g in games if not g["phase1_done"]),
                default=0.0,
            )
            disp_val = hours_to_unit(max_h, unit)

        self._total_drops.config(text=str(total_drops) if total_drops else (
            "?" if any(g["cards_remaining"] < 0 for g in games) else "0"))
        self._total_pt.config(text=f"{disp_val:.0f} {unit}")
        self._games_p1.config(text=str(p1))
        self._games_p2.config(text=str(p2))
        self._games_done.config(text=str(done))


# ---------------------------------------------------------------------------
# Wrapping two-block row (used by the toolbar)
# ---------------------------------------------------------------------------

class _WrapRow(tk.Frame):
    """
    A container that lays out two child frames (set via set_children): the
    left one anchored to the left edge, the right one anchored to the far
    right edge -- like a toolbar with actions on the left and Refresh/
    Settings pinned to the top-right corner. When the window gets too
    narrow for both to fit on one line, the right block drops to its own
    row below the left block (still right-aligned) instead of clipping.
    """

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._left = None
        self._right = None
        self._stacked = None  # tri-state: None = not laid out yet
        self.bind("<Configure>", self._on_configure)

    def set_children(self, left: tk.Widget, right: tk.Widget):
        self._left = left
        self._right = right
        self._left.pack(in_=self, side="left", anchor="w")
        self._right.pack(in_=self, side="right", anchor="e")
        self._stacked = False
        # Widths aren't known until the widgets are drawn; re-check shortly
        # after and on every resize from then on.
        self.after(0, self._reflow)

    def _on_configure(self, event=None):
        self._reflow()

    def _reflow(self):
        if self._left is None or self._right is None:
            return
        available = self.winfo_width()
        if available <= 1:
            return
        needed = self._left.winfo_reqwidth() + 24 + self._right.winfo_reqwidth()
        should_stack = needed > available
        if should_stack == self._stacked:
            return
        self._stacked = should_stack
        self._left.pack_forget()
        self._right.pack_forget()
        if should_stack:
            self._left.pack(in_=self, side="top", anchor="w", fill="x")
            self._right.pack(in_=self, side="top", anchor="e", fill="x", pady=(6, 0))
        else:
            self._left.pack(in_=self, side="left", anchor="w")
            self._right.pack(in_=self, side="right", anchor="e")


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SAM Idler")
        self.geometry("960x900")
        self.minsize(740, 680)
        self.configure(bg=BG)

        self.games, games_warning   = load_games()
        self.config, config_warning = load_config()
        self._controller: IdleController | None = None
        self._thread: threading.Thread | None   = None
        self._running = False
        self._drag_item: str | None = None
        self._last_removed: tuple | None = None
        self._resumed_before = False
        self._sort_col: str = "order"   # column currently sorted by
        self._sort_desc: bool = False   # False = ascending
        self._undo_stack: list[list[dict]] = []   # snapshots of self.games before each mutation
        self._redo_stack: list[list[dict]] = []   # snapshots popped by undo, replayable with Ctrl+Y
        self._undo_limit = 50

        # Playtime display unit (kept in sync with a StringVar)
        self._unit_var = tk.StringVar(value=self.config.get("playtime_unit", "minutes"))
        self._unit_var.trace_add("write", self._on_unit_change)

        self._build_ui()
        self._refresh_table()
        self._summary.refresh(self.games, self._unit_var.get(), threshold_h=float(self.config.get("phase1_threshold_hours", 2.0)))

        # Clicking on empty space unfocuses any active entry/cell editor
        self.bind("<Button-1>", self._maybe_unfocus_on_click)
        self.bind("<Return>",   self._maybe_unfocus_on_key)
        self.bind("<Escape>",   self._maybe_unfocus_on_key)

        # Global keybinds: undo/redo, and delete/backspace to remove selected
        # games. Bound on the root so they work regardless of which widget
        # has focus, but the handlers themselves check focus so they don't
        # fire while someone is typing in a text entry or editing a cell.
        # Note: <Control-z> and <Control-Z> are DIFFERENT bindings in Tk --
        # the capital variant requires Shift too (i.e. Ctrl+Shift+Z). Redo
        # is bound to Ctrl+Y instead, which is unambiguous and matches the
        # convention used by most Windows apps.
        self.bind_all("<Control-z>", self._on_ctrl_z)
        self.bind_all("<Control-y>", self._on_ctrl_y)
        self.bind_all("<Delete>",    self._on_delete_key)
        self.bind_all("<BackSpace>", self._on_delete_key)

        for warning in (games_warning, config_warning):
            if warning:
                self._append_log(f"WARNING: {warning}")
                self.after(200, lambda w=warning: messagebox.showwarning("Data file issue", w))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -----------------------------------------------------------------------
    # Properties
    # -----------------------------------------------------------------------

    @property
    def _unit(self) -> str:
        return self._unit_var.get()

    # -----------------------------------------------------------------------
    # UI build
    # -----------------------------------------------------------------------

    def _build_ui(self):
        # Title row
        tf = tk.Frame(self, bg=BG)
        tf.pack(fill="x", padx=16, pady=(14, 0))
        tk.Label(tf, text="SAM Idler", font=TITLE, bg=BG, fg=ACCENT).pack(side="left")
        sam_ok = SAM_GAME_EXE.exists()
        tk.Label(tf,
                 text="SAM.Game.exe found" if sam_ok else "SAM.Game.exe NOT FOUND",
                 font=FONT, bg=BG, fg=GREEN if sam_ok else RED).pack(side="right")

        # Toolbar: a left block (game management, two rows) and a right
        # block (refresh + settings), inside a wrapping container so the
        # right block drops below the left block instead of clipping when
        # the window gets narrow.
        tb_wrap = _WrapRow(self, bg=BG)
        tb_wrap.pack(fill="x", padx=16, pady=(10, 0))

        tb_left = tk.Frame(tb_wrap, bg=BG)
        tb_left_row1 = tk.Frame(tb_left, bg=BG)
        tb_left_row1.pack(fill="x")
        tb_left_row2 = tk.Frame(tb_left, bg=BG)
        tb_left_row2.pack(fill="x", pady=(6, 0))

        # Row 1: import, add, remove, undo remove
        self._mk_btn(tb_left_row1, "Import from Steam", self._import_library, accent=True).pack(side="left", padx=(0, 6))
        self._mk_btn(tb_left_row1, "Add via App ID",    self._add_by_id).pack(side="left", padx=(0, 6))
        self._mk_btn(tb_left_row1, "Remove",            self._remove_game).pack(side="left", padx=(0, 6))
        self._undo_btn = self._mk_btn(tb_left_row1, "Undo Remove", self._undo_remove)
        self._undo_btn.pack(side="left")
        self._undo_btn.config(state="disabled")

        # Row 2: remove completed, remove all, full reset, force kill all sam
        self._mk_btn(tb_left_row2, "Remove Completed",  self._remove_completed, danger=True).pack(side="left", padx=(0, 6))
        self._mk_btn(tb_left_row2, "Remove All",        self._remove_all,  danger=True).pack(side="left", padx=(0, 6))
        self._mk_btn(tb_left_row2, "Full Reset",        self._full_reset,  danger=True).pack(side="left", padx=(0, 6))
        self._mk_btn(tb_left_row2, "Force Kill All SAM", self._force_kill_all, danger=True).pack(side="left")

        tb_right = tk.Frame(tb_wrap, bg=BG)
        self._refresh_drops_btn = self._mk_btn(tb_right, "Refresh Drops", self._refresh_drops)
        self._refresh_drops_btn.pack(side="left", padx=(0, 6))
        self._refresh_pt_btn = self._mk_btn(tb_right, "Refresh Playtimes", self._refresh_playtimes)
        self._refresh_pt_btn.pack(side="left", padx=(0, 6))
        # Keep _refresh_btn pointing to drops button for compat with existing state changes
        self._refresh_btn = self._refresh_drops_btn
        self._mk_btn(tb_right, "Settings", self._open_settings).pack(side="left")

        tb_wrap.set_children(tb_left, tb_right)
        # Apply merge/split mode from config
        self.after(0, self._apply_refresh_button_mode)

        # Summary bar
        self._summary = SummaryBar(self)
        self._summary.pack(fill="x", padx=16, pady=(10, 0))

        # Search bar
        search_frame = tk.Frame(self, bg=BG)
        search_frame.pack(fill="x", padx=16, pady=(8, 0))
        tk.Label(search_frame, text="Search:", bg=BG, fg=GREY, font=FONT).pack(side="left")
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._refresh_table())
        search_entry = tk.Entry(
            search_frame, textvariable=self._search_var,
            bg=ENTRY_BG, fg=FG, font=FONT, relief="flat",
            insertbackground=FG, width=28,
        )
        search_entry.pack(side="left", padx=(6, 0))
        bind_word_delete(search_entry)
        self._search_count_lbl = tk.Label(search_frame, text="", bg=BG, fg=GREY, font=SMALL)
        self._search_count_lbl.pack(side="left", padx=(6, 0))
        self._mk_btn(search_frame, "Clear", lambda: self._search_var.set("")).pack(side="left", padx=(4, 0))

        # Game table
        list_frame = tk.Frame(self, bg=BG)
        list_frame.pack(fill="both", expand=True, padx=16, pady=(8, 0))

        tk.Label(list_frame,
                 text="Drag rows to reorder. Double-click a cell to edit. Phase 2 idles in list order.",
                 font=SMALL, bg=BG, fg=GREY, anchor="w").pack(anchor="w", pady=(0, 4))

        cols = ("order", "app_id", "name", "playtime", "drops", "phase1", "cards")
        self._tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="extended")
        self._style_tree()

        self._tree.heading("order",    text="#",         command=lambda: self._sort_by("order"))
        self._tree.heading("app_id",   text="App ID",    command=lambda: self._sort_by("app_id"))
        self._tree.heading("name",     text="Name",      command=lambda: self._sort_by("name"))
        self._tree.heading("playtime", text="Playtime",  command=lambda: self._sort_by("playtime"))
        self._tree.heading("drops",    text="Drops left",command=lambda: self._sort_by("drops"))
        self._tree.heading("phase1",   text="Phase 2",   command=lambda: self._sort_by("phase1"))
        self._tree.heading("cards",    text="Cards done",command=lambda: self._sort_by("cards"))

        self._tree.column("order",    width=38,  anchor="center", stretch=False)
        self._tree.column("app_id",   width=82,  anchor="center", stretch=False)
        self._tree.column("name",     width=270)
        self._tree.column("playtime", width=110, anchor="center", stretch=False)
        self._tree.column("drops",    width=78,  anchor="center", stretch=False)
        self._tree.column("phase1",   width=70,  anchor="center", stretch=False)
        self._tree.column("cards",    width=84,  anchor="center", stretch=False)

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Bindings
        self._tree.bind("<ButtonPress-1>",   self._drag_start)
        self._tree.bind("<B1-Motion>",        self._drag_motion)
        self._tree.bind("<ButtonRelease-1>",  self._drag_end)
        self._tree.bind("<Double-Button-1>",  self._on_double_click)
        self._tree.bind("<Button-3>",         self._on_right_click)

        # Move buttons
        of = tk.Frame(self, bg=BG)
        of.pack(fill="x", padx=16, pady=(4, 0))
        self._mk_btn(of, "Move Up",   self._move_up).pack(side="left", padx=(0, 4))
        self._mk_btn(of, "Move Down", self._move_down).pack(side="left", padx=(0, 4))
        self._mk_btn(of, "Reorder",   self._reorder).pack(side="left")

        # Status panel
        self._status_panel = StatusPanel(self)
        self._status_panel.pack(fill="x", padx=16, pady=(10, 0))

        # Control row
        ctrl = tk.Frame(self, bg=BG)
        ctrl.pack(fill="x", padx=16, pady=(8, 0))

        self._start_btn = self._mk_btn(ctrl, "Start Idling", self._start_idling, accent=True)
        self._start_btn.pack(side="left", padx=(0, 6))

        self._stop_btn = self._mk_btn(ctrl, "Pause", self._stop_idling, danger=True)
        self._stop_btn.pack(side="left", padx=(0, 6))
        self._stop_btn.config(state="disabled")

        self._cards_btn = self._mk_btn(ctrl, "Cards Dropped (manual)", self._mark_cards_dropped, success=True)
        self._cards_btn.pack(side="left", padx=(0, 6))
        self._cards_btn.config(state="disabled")

        self._cards_hint = tk.Label(ctrl, text="", font=SMALL, bg=BG, fg=GREY)
        self._cards_hint.pack(side="left", padx=4)
        self._update_cards_hint()

        # Log
        log_frame = tk.Frame(self, bg=BG)
        log_frame.pack(fill="both", padx=16, pady=(10, 14))

        log_hdr = tk.Frame(log_frame, bg=BG)
        log_hdr.pack(fill="x", anchor="w")
        tk.Label(log_hdr, text="Log", font=BOLD, bg=BG, fg=FG).pack(side="left")
        self._mk_btn(log_hdr, "Copy Log",   self._copy_log,   ).pack(side="left", padx=(8, 0))
        self._mk_btn(log_hdr, "Export Log", self._export_log, ).pack(side="left", padx=(4, 0))

        self._log_text = tk.Text(
            log_frame, height=10,
            bg=ENTRY_BG, fg=FG, font=MONO, relief="flat", wrap="word", bd=0,
            state="normal",   # keep normal so user can select/copy
        )
        # Make it read-only to typing but still selectable
        self._log_text.bind("<Key>", lambda e: "break" if e.keysym not in (
            "c", "C", "a", "A") and e.state & 0x4 == 0 else None)
        log_vsb = ttk.Scrollbar(log_frame, orient="vertical", command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_vsb.set)
        self._log_text.pack(side="left", fill="both", expand=True)
        log_vsb.pack(side="right", fill="y")

    def _style_tree(self):
        s = ttk.Style(self)
        s.theme_use("default")
        s.configure("Treeview",
            background=ROW_EVEN, fieldbackground=ROW_EVEN, foreground=FG,
            rowheight=26, font=FONT, borderwidth=0)
        s.configure("Treeview.Heading",
            background=BTN_BG, foreground=FG, font=BOLD, relief="flat")
        s.map("Treeview",
            background=[("selected", ACCENT)],
            foreground=[("selected", "#fff")])
        s.configure("Vertical.TScrollbar", background=BTN_BG, troughcolor=BG)
        self._tree.tag_configure("odd",    background=ROW_ODD)
        self._tree.tag_configure("even",   background=ROW_EVEN)
        self._tree.tag_configure("done",   foreground=GREEN)
        self._tree.tag_configure("active", foreground=ORANGE)
        self._tree.tag_configure("drag",   background="#4a4a00")

    def _mk_btn(self, parent, text, command, accent=False, danger=False, success=False):
        bg = ACCENT if accent else ("#7a2020" if danger else ("#2a5c2a" if success else BTN_BG))
        fg = "#fff" if accent else FG
        return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                         activebackground=ACCENT, activeforeground="#fff",
                         font=FONT, relief="flat", padx=10, pady=5, cursor="hand2", bd=0)

    def _maybe_unfocus_on_click(self, event=None):
        """
        Shift keyboard focus to the main window when the user clicks on
        empty space (not on an input widget). Dismisses any active inline
        cell editor (which commits on FocusOut) and clears the cursor from
        any entry widget when clicking elsewhere.

        This used to be bound as one handler shared with <Return>/<Escape>
        and fired on EVERY click anywhere in the window -- including a click
        INTO the search box or any other entry -- immediately stealing focus
        back with focus_set() right after the widget had just set it. That
        meant an entry could never actually keep keyboard focus after being
        clicked. We now check what was actually clicked: if it's an
        entry-like input widget (or something inside one), we leave focus
        alone and let the click's own default behaviour stand.
        """
        widget = event.widget if event is not None else None
        w = widget
        while w is not None:
            if isinstance(w, (tk.Entry, tk.Spinbox, ttk.Entry, ttk.Combobox, ttk.Treeview)):
                return
            try:
                parent_name = w.winfo_parent()
            except tk.TclError:
                break
            w = w.nametowidget(parent_name) if parent_name else None

        focused = self.focus_get()
        if focused and focused is not self:
            self.focus_set()

    def _maybe_unfocus_on_key(self, event=None):
        """Return/Escape always dismiss focus, regardless of what's focused."""
        focused = self.focus_get()
        if focused and focused is not self:
            self.focus_set()

    # -----------------------------------------------------------------------
    # Undo / redo stack (Ctrl+Z / Ctrl+Y) -- covers edits, bulk edits,
    # toggles, reorders, and removals with a single mechanism: snapshot
    # self.games before any mutating action, restore the most recent
    # snapshot on undo, and step back forward through undone snapshots
    # with redo.
    # -----------------------------------------------------------------------

    def _push_undo(self):
        """Snapshot the current game list before a mutating action."""
        snapshot = [dict(g) for g in self.games]
        self._undo_stack.append(snapshot)
        if len(self._undo_stack) > self._undo_limit:
            self._undo_stack.pop(0)
        # A fresh action invalidates whatever redo history existed --
        # otherwise redoing after a new edit could jump to a state that no
        # longer makes sense next to what was just done.
        self._redo_stack.clear()

    def _on_ctrl_z(self, event=None):
        # Don't hijack Ctrl+Z while the user is editing text somewhere
        # (an open cell editor, the search box, a settings field, etc.) --
        # let the widget's own native undo/typing behaviour happen instead.
        focused = self.focus_get()
        if isinstance(focused, (tk.Entry, tk.Spinbox, ttk.Entry, ttk.Combobox, tk.Text)):
            return
        self._undo()

    def _on_ctrl_y(self, event=None):
        # Same guard as Ctrl+Z: don't hijack Ctrl+Y while typing.
        focused = self.focus_get()
        if isinstance(focused, (tk.Entry, tk.Spinbox, ttk.Entry, ttk.Combobox, tk.Text)):
            return
        self._redo()

    def _undo(self):
        if not self._undo_stack:
            self._append_log("Nothing to undo.")
            return
        self._redo_stack.append([dict(g) for g in self.games])
        self.games[:] = self._undo_stack.pop()
        save_games(self.games)
        self._refresh_table()
        self._append_log("Undid last change.")

    def _redo(self):
        if not self._redo_stack:
            self._append_log("Nothing to redo.")
            return
        self._undo_stack.append([dict(g) for g in self.games])
        self.games[:] = self._redo_stack.pop()
        save_games(self.games)
        self._refresh_table()
        self._append_log("Redid last undone change.")

    def _on_delete_key(self, event=None):
        # Don't hijack Delete/Backspace while typing in a text entry --
        # only treat it as "remove selected game(s)" when the table itself
        # (or nothing in particular) has focus.
        focused = self.focus_get()
        if isinstance(focused, (tk.Entry, tk.Spinbox, ttk.Entry, ttk.Combobox, tk.Text)):
            return
        selected = self._selected_indices()
        if not selected:
            return
        self._remove_selected(selected)

    # -----------------------------------------------------------------------
    # Unit change
    # -----------------------------------------------------------------------

    def _update_cards_hint(self):
        has_cookies = bool(self.config.get("session_id") and self.config.get("login_secure"))
        if has_cookies:
            self._cards_hint.config(
                text="(cookies are set, so drops are checked automatically; only click this if you're sure it's done)"
            )
        else:
            self._cards_hint.config(
                text="(no cookies set, so drop count is unknown; click this once you see the drops in Steam)"
            )

    def _on_unit_change(self, *_):
        self.config["playtime_unit"] = self._unit
        save_config(self.config)
        self._refresh_table()
        self._summary.refresh(self.games, self._unit, threshold_h=float(self.config.get("phase1_threshold_hours", 2.0)))

    # -----------------------------------------------------------------------
    # Table
    # -----------------------------------------------------------------------

    def _playtime_display(self, hours: float) -> str:
        val = hours_to_unit(hours, self._unit)
        # Show enough precision based on unit
        if self._unit == "seconds":
            return f"{val:.0f}s"
        if self._unit == "minutes":
            return f"{val:.1f}m"
        if self._unit == "hours":
            return f"{val:.2f}h"
        if self._unit == "days":
            return f"{val:.3f}d"
        return str(val)

    _FILTER_STRIP_RE = re.compile(r"[':()\u2122]")
    _FILTER_DASH_RE  = re.compile(r"[-_]")

    def _filter_normalize(self, s: str) -> str:
        s = self._FILTER_STRIP_RE.sub("", s)
        s = self._FILTER_DASH_RE.sub(" ", s)
        s = re.sub(r"\s+", " ", s).strip().lower()
        return s

    def _sort_by(self, col: str):
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = False
        self._refresh_table()

    def _sorted_games_for_display(self) -> list[tuple[int, dict]]:
        """
        Returns [(original_index, game), ...] sorted by the current sort column.
        The original index is needed so the '#' column always reflects list position,
        and so drag/reorder/move operations still reference the right slot.
        """
        indexed = list(enumerate(self.games))
        col = self._sort_col

        if col == "order":
            # Default: list order. Ascending = normal, descending = reversed.
            if self._sort_desc:
                indexed = list(reversed(indexed))
            return indexed

        def _key(pair):
            _, g = pair
            if col == "app_id":
                return int(g["app_id"]) if g["app_id"].isdigit() else 0
            if col == "name":
                return g["name"].lower()
            if col == "playtime":
                return g["playtime_hours"]
            if col == "drops":
                v = g["cards_remaining"]
                # Sort unknowns (-1) to the end regardless of direction
                return (1, v) if v >= 0 else (2, 0)
            if col == "phase1":
                return 0 if g["phase1_done"] else 1
            if col == "cards":
                return 0 if g["cards_done"] else 1
            return 0

        indexed.sort(key=_key, reverse=self._sort_desc)
        return indexed

    def _update_heading_arrows(self):
        labels = {
            "order":    "#",
            "app_id":   "App ID",
            "name":     "Name",
            "playtime": "Playtime",
            "drops":    "Drops left",
            "phase1":   "Phase 2",
            "cards":    "Cards done",
        }
        arrow = " ↓" if self._sort_desc else " ↑"
        for col, base in labels.items():
            text = base + arrow if col == self._sort_col else base
            self._tree.heading(col, text=text)

    def _refresh_table(self):
        sel     = self._tree.selection()
        sel_iids = set(sel)
        self._tree.delete(*self._tree.get_children())

        self._update_heading_arrows()

        search_raw = self._search_var.get() if hasattr(self, "_search_var") else ""
        search_norm = self._filter_normalize(search_raw)

        shown = 0
        for display_pos, (orig_idx, g) in enumerate(self._sorted_games_for_display()):
            if search_norm:
                name_norm = self._filter_normalize(g["name"])
                if search_norm not in name_norm and search_norm not in g["app_id"]:
                    continue
            shown += 1
            if g["cards_done"]:
                tag = "done"
            elif g["phase1_done"]:
                tag = "active"
            elif shown % 2 == 0:
                tag = "even"
            else:
                tag = "odd"
            drops_str = str(g["cards_remaining"]) if g["cards_remaining"] >= 0 else "?"
            self._tree.insert("", "end", iid=str(orig_idx),
                values=(
                    orig_idx + 1,
                    g["app_id"],
                    g["name"],
                    self._playtime_display(g["playtime_hours"]),
                    drops_str,
                    "yes" if g["phase1_done"] else "no",
                    "yes" if g["cards_done"]  else "no",
                ),
                tags=(tag,))

        # Restore selection
        for iid in sel_iids:
            if self._tree.exists(iid):
                self._tree.selection_add(iid)

        # Update search count label
        if hasattr(self, "_search_count_lbl"):
            total = len(self.games)
            if search_norm:
                self._search_count_lbl.config(text=f"{shown}/{total}")
            else:
                self._search_count_lbl.config(text="")

        self._summary.refresh(self.games, self._unit, threshold_h=float(self.config.get("phase1_threshold_hours", 2.0)))

    # -----------------------------------------------------------------------
    # Inline cell editing
    # -----------------------------------------------------------------------

    _EDITABLE = {
        "order":    "order",
        "app_id":   "app_id",
        "name":     "text",
        "playtime": "playtime",
        "drops":    "drops",
        "phase1":   "toggle",
        "cards":    "toggle",
    }

    def _selected_indices(self) -> list[int]:
        return [int(iid) for iid in self._tree.selection()]

    def _on_double_click(self, event):
        region = self._tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        iid = self._tree.identify_row(event.y)
        col = self._tree.identify_column(event.x)
        if not iid or not col:
            return

        col_idx  = int(col[1:]) - 1
        col_name = self._tree["columns"][col_idx]
        edit_type = self._EDITABLE.get(col_name)
        if not edit_type:
            return

        selected = self._selected_indices()
        # If multiple rows selected and the clicked row is one of them,
        # apply the edit to all selected. Otherwise edit just the clicked row.
        multi = len(selected) > 1 and int(iid) in selected
        indices = selected if multi else [int(iid)]

        if edit_type == "toggle":
            # For toggle, flip based on the clicked row's current value
            g0 = self.games[int(iid)]
            new_val = not g0[col_name if col_name != "phase1" else "phase1_done"]
            field = "phase1_done" if col_name == "phase1" else "cards_done"
            self._push_undo()
            for idx in indices:
                self.games[idx][field] = new_val
            save_games(self.games)
            self._refresh_table()
            return

        if multi and edit_type in ("playtime", "drops", "text"):
            # Ask once, apply to all selected
            self._bulk_edit(indices, col_name, edit_type)
            return

        # Single edit via inline cell editor
        idx = int(iid)
        g   = self.games[idx]
        if edit_type == "order":
            current_val = str(idx + 1)
        elif edit_type == "playtime":
            current_val = f"{hours_to_unit(g['playtime_hours'], self._unit):.4g}"
        elif edit_type == "app_id":
            current_val = g["app_id"]
        elif edit_type == "drops":
            current_val = str(g["cards_remaining"]) if g["cards_remaining"] >= 0 else "0"
        else:
            current_val = g["name"]

        _CellEditor(self._tree, iid, col_name, current_val, self._commit_edit)

    def _bulk_edit(self, indices: list[int], col_name: str, edit_type: str):
        """Apply the same value to all selected rows for a given column."""
        if edit_type == "playtime":
            prompt = f"Set playtime ({self._unit}) for {len(indices)} game(s):"
            raw = simpledialog.askstring("Bulk Edit", prompt, parent=self)
            if raw is None:
                return
            self._push_undo()
            hours = parse_playtime(raw, self._unit)
            for idx in indices:
                self.games[idx]["playtime_hours"] = hours
                self.games[idx]["phase1_done"]    = hours >= float(self.config.get("phase1_threshold_hours", 2.0))
        elif edit_type == "drops":
            raw = simpledialog.askstring(
                "Bulk Edit", f"Set drops remaining for {len(indices)} game(s):", parent=self
            )
            if raw is None:
                return
            self._push_undo()
            try:
                drops = int(raw.strip())
            except ValueError:
                drops = -1
            for idx in indices:
                self.games[idx]["cards_remaining"] = drops
                if drops == 0:
                    self.games[idx]["cards_done"] = True
        elif edit_type == "text":
            raw = simpledialog.askstring(
                "Bulk Edit", f"Set name for {len(indices)} game(s):", parent=self
            )
            if raw is None:
                return
            self._push_undo()
            for idx in indices:
                self.games[idx]["name"] = raw.strip() or self.games[idx]["name"]
        save_games(self.games)
        self._refresh_table()

    def _commit_edit(self, iid: str, col_name: str, raw_val: str):
        idx = int(iid)
        if idx >= len(self.games):
            return
        g = self.games[idx]

        if col_name == "order":
            try:
                new_pos = int(raw_val.strip()) - 1
            except ValueError:
                return
            new_pos = max(0, min(new_pos, len(self.games) - 1))
            if new_pos != idx:
                self._push_undo()
                item = self.games.pop(idx)
                self.games.insert(new_pos, item)
                save_games(self.games)
                self._refresh_table()
                new_iid = str(new_pos)
                if self._tree.exists(new_iid):
                    self._tree.selection_set(new_iid)
            return

        if col_name == "name":
            stripped = raw_val.strip()
            if stripped:
                self._push_undo()
                g["name"] = stripped
            save_games(self.games)
            self._refresh_table()
            return

        if col_name == "app_id":
            digits = re.sub(r"[^\d]", "", raw_val)
            if not digits:
                self._append_log(f"App ID edit: '{raw_val}' has no digits, App ID left unchanged.")
                return
            self._push_undo()
            if digits != g["app_id"] and any(other["app_id"] == digits for other in self.games if other is not g):
                self._append_log(f"App ID {digits} is already used elsewhere in the list, but changing it anyway.")
            g["app_id"] = digits
            save_games(self.games)
            self._refresh_table()
            return

        if col_name == "playtime":
            self._push_undo()
            hours = parse_playtime(raw_val, self._unit)
            g["playtime_hours"] = hours
            g["phase1_done"]    = hours >= float(self.config.get("phase1_threshold_hours", 2.0))
            save_games(self.games)
            self._refresh_table()
            return

        if col_name == "drops":
            try:
                drops = int(raw_val.strip())
            except ValueError:
                return
            self._push_undo()
            g["cards_remaining"] = drops
            if drops == 0:
                g["cards_done"] = True
            save_games(self.games)
            self._refresh_table()
            return

    # -----------------------------------------------------------------------
    # Drag reorder
    # -----------------------------------------------------------------------

    def _on_right_click(self, event):
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        # If the right-clicked row isn't in the current selection, select just it
        if iid not in self._tree.selection():
            self._tree.selection_set(iid)
        selected = self._selected_indices()
        idx = int(iid)
        g = self.games[idx]
        multi = len(selected) > 1

        menu = tk.Menu(self, tearoff=0, bg=BTN_BG, fg=FG,
                       activebackground=ACCENT, activeforeground="#fff",
                       relief="flat", bd=0)

        header = f"{g['name']}" if not multi else f"{len(selected)} games selected"
        menu.add_command(label=header, state="disabled", foreground=GREY, background=BTN_BG)
        menu.add_separator()

        if not multi:
            menu.add_command(
                label="Move to top",
                state="normal" if idx > 0 else "disabled",
                command=lambda: self._move_to(idx, 0),
            )
            menu.add_command(
                label="Move up",
                state="normal" if idx > 0 else "disabled",
                command=self._move_up,
            )
            menu.add_command(
                label="Move down",
                state="normal" if idx < len(self.games) - 1 else "disabled",
                command=self._move_down,
            )
            menu.add_command(
                label="Move to bottom",
                state="normal" if idx < len(self.games) - 1 else "disabled",
                command=lambda: self._move_to(idx, len(self.games) - 1),
            )
            menu.add_separator()

        # Toggle flags (works for single and multi)
        thresh = float(self.config.get("phase1_threshold_hours", 2.0))
        label_2h = f"Mark {len(selected)} game(s) Phase 2 ready" if multi else (
            "Mark Phase 2 ready" if not g["phase1_done"] else "Mark Phase 2 NOT ready"
        )
        menu.add_command(label=label_2h, command=lambda: self._set_field_all(selected, "phase1_done", True if multi else not g["phase1_done"]))

        label_cards = f"Mark {len(selected)} game(s) cards done" if multi else (
            "Mark cards done" if not g["cards_done"] else "Mark cards NOT done"
        )
        menu.add_command(label=label_cards, command=lambda: self._set_field_all(selected, "cards_done", True if multi else not g["cards_done"]))

        menu.add_separator()

        if multi:
            menu.add_command(
                label=f"Bulk edit playtime for {len(selected)} game(s)",
                command=lambda: self._bulk_edit(selected, "playtime", "playtime"),
            )
            menu.add_command(
                label=f"Bulk edit drops for {len(selected)} game(s)",
                command=lambda: self._bulk_edit(selected, "drops", "drops"),
            )
            menu.add_separator()
            menu.add_command(
                label=f"Remove {len(selected)} game(s)",
                command=lambda: self._remove_selected(selected),
            )
        else:
            menu.add_command(label="Refresh playtime & drops", command=lambda: self._refresh_single(idx))
            menu.add_separator()
            menu.add_command(label="Remove", command=self._remove_game)

        menu.tk_popup(event.x_root, event.y_root)

    def _set_field_all(self, indices: list[int], field: str, value):
        self._push_undo()
        for idx in indices:
            self.games[idx][field] = value
        save_games(self.games)
        self._refresh_table()

    def _remove_selected(self, indices: list[int]):
        self._push_undo()
        indices_sorted = sorted(indices, reverse=True)
        for idx in indices_sorted:
            self.games.pop(idx)
        self._last_removed = None
        self._undo_btn.config(state="disabled")
        save_games(self.games)
        self._refresh_table()
        self._append_log(f"Removed {len(indices)} game(s).")

    def _move_to(self, src: int, dst: int):
        if src == dst:
            return
        self._push_undo()
        game = self.games.pop(src)
        self.games.insert(dst, game)
        save_games(self.games)
        self._refresh_table()
        if self._tree.exists(str(dst)):
            self._tree.selection_set(str(dst))

    def _toggle_field(self, idx: int, field: str):
        self._push_undo()
        g = self.games[idx]
        g[field] = not g[field]
        save_games(self.games)
        self._refresh_table()

    def _drag_start(self, event):
        # Don't start a drag on a double-click
        item = self._tree.identify_row(event.y)
        if item:
            self._drag_item   = item
            self._drag_moved  = False

    def _drag_motion(self, event):
        if not self._drag_item:
            return
        target = self._tree.identify_row(event.y)
        if target and target != self._drag_item:
            self._drag_moved = True
            for iid in self._tree.get_children():
                tags = [t for t in self._tree.item(iid, "tags") if t != "drag"]
                self._tree.item(iid, tags=tags)
            cur = list(self._tree.item(target, "tags"))
            cur.append("drag")
            self._tree.item(target, tags=cur)

    def _drag_end(self, event):
        if not self._drag_item:
            return
        target = self._tree.identify_row(event.y)
        if target and target != self._drag_item and getattr(self, "_drag_moved", False):
            src, dst = int(self._drag_item), int(target)
            self._push_undo()
            item = self.games.pop(src)
            self.games.insert(dst, item)
            save_games(self.games)
            self._refresh_table()
            if self._tree.exists(str(dst)):
                self._tree.selection_set(str(dst))
        self._drag_item  = None
        self._drag_moved = False

    def _selected_index(self) -> int | None:
        sel = self._tree.selection()
        return int(sel[0]) if sel else None

    def _move_up(self):
        idx = self._selected_index()
        if idx is None or idx == 0:
            return
        self._push_undo()
        self.games[idx - 1], self.games[idx] = self.games[idx], self.games[idx - 1]
        save_games(self.games)
        self._refresh_table()
        if self._tree.exists(str(idx - 1)):
            self._tree.selection_set(str(idx - 1))

    def _move_down(self):
        idx = self._selected_index()
        if idx is None or idx >= len(self.games) - 1:
            return
        self._push_undo()
        self.games[idx], self.games[idx + 1] = self.games[idx + 1], self.games[idx]
        save_games(self.games)
        self._refresh_table()
        if self._tree.exists(str(idx + 1)):
            self._tree.selection_set(str(idx + 1))

    def _reorder(self):
        """
        Commit whatever order the table is currently sorted/displayed in as
        the new Phase 2 list order (the '#' column). E.g. sort by Drops
        descending, then click Reorder, to prioritize games with the most
        drops left without having to drag everything by hand.
        """
        if not self.games:
            return
        if self._sort_col == "order":
            messagebox.showinfo(
                "Reorder",
                "The table is already showing list order (# column).\n"
                "Sort by a different column first (e.g. Drops left), then click Reorder.",
            )
            return
        self._push_undo()
        new_order = [g for _, g in self._sorted_games_for_display()]
        self.games[:] = new_order
        self._sort_col = "order"
        self._sort_desc = False
        save_games(self.games)
        self._refresh_table()
        self._append_log(f"Reordered list to match current sort ({len(new_order)} game(s)).")

    # -----------------------------------------------------------------------
    # Log
    # -----------------------------------------------------------------------

    def _append_log(self, msg: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        self._log_text.insert("end", line + "\n")
        self._log_text.see("end")

    def _log_from_thread(self, msg: str):
        self.after(0, self._append_log, msg)

    def _copy_log(self):
        content = self._log_text.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(content)

    def _export_log(self):
        import datetime
        logs_dir = Path(__file__).parent / "logs"
        logs_dir.mkdir(exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]
        path = logs_dir / f"log-{ts}.txt"
        content = self._log_text.get("1.0", "end").strip()
        path.write_text(content, encoding="utf-8")
        self._append_log(f"Log exported to logs/log-{ts}.txt")

    # -----------------------------------------------------------------------
    # Thread callbacks
    # -----------------------------------------------------------------------

    def _auto_remove_from_thread(self, app_id: str):
        def _apply():
            before = len(self.games)
            # Mutate in place: self.games is the same list object the
            # IdleController thread holds a reference to. Reassigning
            # self.games here would leave the controller iterating and
            # re-saving its own stale copy (with this game still in it)
            # forever, which is why auto-remove used to silently do nothing.
            #
            # Deliberately not pushed onto the Ctrl+Z undo stack: this fires
            # unattended in the background while idling, possibly many times
            # over a long session, and would otherwise bury the manual edit
            # the user actually meant to undo under automatic removals.
            self.games[:] = [g for g in self.games if g["app_id"] != app_id]
            if len(self.games) < before:
                save_games(self.games)
                self._refresh_table()
                self._append_log(f"Auto-removed {app_id} (cards done).")
        self.after(0, _apply)

    def _update_from_thread(self):
        self.after(0, self._refresh_table)

    def _status_from_thread(self, st: IdleStatus):
        def _apply():
            self._status_panel.update_status(st, self._running)
            if self._running and st.phase1_running:
                self._summary.refresh(
                    self.games, self._unit,
                    threshold_h=float(self.config.get("phase1_threshold_hours", 2.0)),
                    phase1_remaining_sec=st.next_check_sec,
                )
        self.after(0, _apply)

    def _on_all_done(self):
        self.after(0, self._handle_all_done)

    def _handle_all_done(self):
        self._running = False
        self._start_btn.config(text="Start Idling", state="normal")
        self._stop_btn.config(state="disabled")
        self._cards_btn.config(state="disabled")
        self._status_panel.update_status(IdleStatus(), False)
        self._resumed_before = False
        messagebox.showinfo("Done", "All games idled through both phases.")

    # -----------------------------------------------------------------------
    # Settings
    # -----------------------------------------------------------------------

    def _apply_refresh_button_mode(self):
        if self.config.get("merge_refresh_buttons", False):
            self._refresh_drops_btn.config(text="Refresh", command=self._refresh_all)
            self._refresh_pt_btn.pack_forget()
        else:
            self._refresh_drops_btn.config(text="Refresh Drops", command=self._refresh_drops)
            if not self._refresh_pt_btn.winfo_ismapped():
                self._refresh_pt_btn.pack(side="left", padx=(0, 6))

    def _refresh_all(self, silent: bool = False):
        self._refresh_drops(silent=silent)
        self._refresh_playtimes(silent=silent)

    def _force_kill_all(self):
        import subprocess as sp
        try:
            sp.run(["taskkill", "/F", "/IM", "SAM.Game.exe"],
                   stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            self._append_log("Force killed all SAM.Game.exe processes.")
        except Exception as exc:
            self._append_log(f"Force kill failed: {exc}")

    def _remove_completed(self):
        completed = [g for g in self.games if g["cards_done"]]
        if not completed:
            messagebox.showinfo("Nothing to remove", "No games are marked as cards done.")
            return
        if not messagebox.askyesno(
            "Remove Completed",
            f"Remove {len(completed)} game(s) with all cards dropped?",
        ):
            return
        self._push_undo()
        # Mutate the list in place (not a reassignment) so any running
        # IdleController, which holds a reference to this same list object,
        # sees the removal too instead of keeping a stale copy around.
        self.games[:] = [g for g in self.games if not g["cards_done"]]
        self._last_removed = None
        self._undo_btn.config(state="disabled")
        save_games(self.games)
        self._refresh_table()
        self._append_log(f"Removed {len(completed)} completed game(s).")

    # -----------------------------------------------------------------------
    # Settings
    # -----------------------------------------------------------------------

    def _open_settings(self):
        dlg = SettingsDialog(self, self.config, self._unit_var)
        if dlg.result:
            self.config.update(dlg.result)
            save_config(self.config)
            self._update_cards_hint()
            self._apply_refresh_button_mode()

    # -----------------------------------------------------------------------
    # Import
    # -----------------------------------------------------------------------

    def _import_library(self):
        if not self.config.get("api_key") or not self.config.get("steam_id"):
            messagebox.showinfo("Settings required",
                "Open Settings and fill in your Steam API key and Steam ID.\n\n"
                "API keys: https://steamcommunity.com/dev/apikey\n"
                "(The domain name field on that page can be anything, e.g. localhost)")
            return

        self._append_log("Fetching library from Steam...")
        session_id   = self.config.get("session_id", "")
        login_secure = self.config.get("login_secure", "")
        steam_id     = self.config.get("steam_id", "")

        def _fetch():
            try:
                games = fetch_owned_games(self.config["api_key"], steam_id)
            except Exception as exc:
                self.after(0, messagebox.showerror, "Error", f"Library fetch failed:\n{exc}")
                return
            # Best-effort: fill in drop counts from the badges list if cookies
            # are set, so the import dialog isn't showing "?" for everything.
            # Not authoritative (see fetch_card_drops_bulk docstring) but good
            # enough to sort/filter by at import time; a per-game check runs
            # anyway once a game actually starts idling.
            if session_id and login_secure:
                try:
                    drops = fetch_card_drops_bulk(session_id, login_secure, steam_id)
                    for g in games:
                        if g["app_id"] in drops:
                            g["cards_remaining"] = drops[g["app_id"]]
                except Exception as exc:
                    self.after(0, self._append_log, f"Drop counts unavailable for import: {exc}")
            self.after(0, self._show_import_dialog, games)

        threading.Thread(target=_fetch, daemon=True).start()

    def _show_import_dialog(self, fetched: list):
        self._append_log(f"Fetched {len(fetched)} games.")
        existing = {g["app_id"] for g in self.games}
        dlg = ImportDialog(self, fetched, existing, unit=self._unit)
        added = 0
        skipped = 0
        to_add = [g for g in dlg.selected if g["app_id"] not in existing]
        if to_add:
            self._push_undo()
        for g in to_add:
            self.games.append(default_game(
                g["app_id"], g["name"], g["playtime_hours"],
                g.get("cards_remaining", -1),
            ))
            added += 1
        skipped = len(dlg.selected) - added
        if added:
            save_games(self.games)
            self._refresh_table()
        if added or skipped:
            msg = f"Added {added} game(s)."
            if skipped:
                msg += f" Skipped {skipped} already in the list."
            self._append_log(msg)
        else:
            self._append_log("No games selected, nothing added.")

    # -----------------------------------------------------------------------
    # Add by ID
    # -----------------------------------------------------------------------

    def _add_by_id(self):
        raw = simpledialog.askstring("Add via App ID", "Steam App ID:", parent=self)
        if raw is None:
            return
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            self._append_log(f"Add via App ID: '{raw}' has no digits in it, nothing added.")
            messagebox.showinfo(
                "No number found",
                f"'{raw}' doesn't contain any digits, so there's no App ID to use.\n"
                "Try again with the numeric Steam App ID (e.g. 440).",
            )
            return
        if digits != raw.strip():
            self._append_log(f"Add via App ID: interpreted '{raw}' as {digits}.")
        app_id = digits

        is_dupe = any(g["app_id"] == app_id for g in self.games)
        if is_dupe:
            self._append_log(f"App {app_id} is already in the list, adding another entry won't be blocked.")

        name = simpledialog.askstring("Add via App ID", "Game name (optional):", parent=self)
        if name is None:
            return   # user cancelled

        pt = simpledialog.askstring(
            "Add via App ID",
            f"Current playtime ({self._unit}):",
            initialvalue="0", parent=self,
        )
        if pt is None:
            return   # user cancelled

        hours = parse_playtime(pt, self._unit)
        thresh = float(self.config.get("phase1_threshold_hours", 2.0))
        self._push_undo()
        game = default_game(app_id, name or "", hours)
        game["phase1_done"] = hours >= thresh
        self.games.append(game)
        save_games(self.games)
        self._refresh_table()
        self._append_log(f"Added App {app_id}" + (f" ({name})" if name else "") + ".")

    # -----------------------------------------------------------------------
    # Remove
    # -----------------------------------------------------------------------

    def _remove_game(self):
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo("Select a game", "Select a game first.")
            return
        self._push_undo()
        g = self.games.pop(idx)
        self._last_removed = (idx, g)
        save_games(self.games)
        self._refresh_table()
        self._append_log(f"Removed {g['name']} ({g['app_id']}). Click Undo Remove to bring it back.")
        self._undo_btn.config(state="normal")

    def _undo_remove(self):
        if not self._last_removed:
            return
        idx, g = self._last_removed
        idx = max(0, min(idx, len(self.games)))
        self.games.insert(idx, g)
        self._last_removed = None
        save_games(self.games)
        self._refresh_table()
        self._append_log(f"Restored {g['name']} ({g['app_id']}).")
        self._undo_btn.config(state="disabled")

    def _remove_all(self):
        if not self.games:
            messagebox.showinfo("Nothing to remove", "The game list is already empty.")
            return
        if not messagebox.askyesno(
            "Remove All",
            f"Remove all {len(self.games)} game(s) from the list?\n\nThis can be undone with Ctrl+Z.",
        ):
            return
        self._push_undo()
        count = len(self.games)
        self.games.clear()
        self._last_removed = None
        self._undo_btn.config(state="disabled")
        save_games(self.games)
        self._refresh_table()
        self._append_log(f"Removed all {count} game(s).")

    def _full_reset(self):
        if not messagebox.askyesno(
            "Full Reset",
            "This will remove all games AND clear all phase/card progress.\n\n"
            "The list will be completely empty. This can be undone with Ctrl+Z. Are you sure?",
        ):
            return
        self._push_undo()
        count = len(self.games)
        self.games.clear()
        self._last_removed = None
        self._undo_btn.config(state="disabled")
        save_games(self.games)
        self._refresh_table()
        self._append_log(f"Full reset: removed {count} game(s).")

    # -----------------------------------------------------------------------
    # Refresh drops
    # -----------------------------------------------------------------------

    def _refresh_drops(self, silent: bool = False):
        if not self.config.get("session_id") or not self.config.get("login_secure"):
            if silent:
                self._append_log("Skipped drop refresh: no session cookies set in Settings.")
            else:
                messagebox.showinfo("Cookies required",
                    "Enter your sessionid and steamLoginSecure in Settings first.")
            return
        self._append_log(f"Refreshing card drop counts for {len(self.games)} game(s)...")
        self._refresh_btn.config(state="disabled")

        session_id   = self.config["session_id"]
        login_secure = self.config["login_secure"]
        steam_id     = self.config.get("steam_id", "")
        games_snapshot = list(self.games)

        def _fetch():
            confirmed: dict[str, int] = {}
            try:
                confirmed.update(fetch_card_drops_bulk(session_id, login_secure, steam_id))
            except Exception as exc:
                self.after(0, self._append_log, f"Bulk drop scrape skipped: {exc}")

            unresolved = [g for g in games_snapshot if g["app_id"] not in confirmed]
            for i, g in enumerate(unresolved):
                try:
                    confirmed[g["app_id"]] = fetch_app_card_drops(
                        session_id, login_secure, g["app_id"], steam_id
                    )
                except Exception as exc:
                    self.after(0, self._append_log, f"{g['name']}: {exc}")
                if (i + 1) % 5 == 0:
                    self.after(0, self._append_log,
                               f"Checked {i + 1}/{len(unresolved)} remaining game(s)...")

            def _apply():
                updated = 0
                for g in self.games:
                    if g["app_id"] in confirmed:
                        g["cards_remaining"] = confirmed[g["app_id"]]
                        g["cards_done"] = (confirmed[g["app_id"]] == 0)
                        updated += 1
                save_games(self.games)
                self._refresh_table()
                still_with_drops = sum(1 for g in self.games if g["cards_remaining"] > 0)
                unknown = sum(1 for g in self.games if g["cards_remaining"] < 0)
                msg = f"Drop counts refreshed for {updated}/{len(self.games)} game(s). {still_with_drops} still have drops remaining."
                if unknown:
                    msg += f" {unknown} still unknown."
                self._append_log(msg)
                self._refresh_btn.config(state="normal")
            self.after(0, _apply)

        threading.Thread(target=_fetch, daemon=True).start()

    def _refresh_single(self, idx: int):
        g = self.games[idx]
        has_cookies = bool(self.config.get("session_id") and self.config.get("login_secure"))
        has_api     = bool(self.config.get("api_key") and self.config.get("steam_id"))
        if not has_cookies and not has_api:
            messagebox.showinfo("Nothing configured", "Set your API key or cookies in Settings first.")
            return
        self._append_log(f"Refreshing {g['name']}...")
        session_id   = self.config.get("session_id", "")
        login_secure = self.config.get("login_secure", "")
        steam_id     = self.config.get("steam_id", "")
        api_key      = self.config.get("api_key", "")
        app_id       = g["app_id"]

        def _fetch():
            new_pt    = None
            new_drops = None
            if has_api:
                try:
                    fetched = fetch_owned_games(api_key, steam_id)
                    pt_map  = {f["app_id"]: f["playtime_hours"] for f in fetched}
                    if app_id in pt_map:
                        new_pt = pt_map[app_id]
                except Exception as exc:
                    self.after(0, self._append_log, f"Playtime fetch failed: {exc}")
            if has_cookies:
                try:
                    new_drops = fetch_app_card_drops(session_id, login_secure, app_id, steam_id)
                except Exception as exc:
                    self.after(0, self._append_log, f"Drop check failed: {exc}")

            def _apply():
                targets = [x for x in self.games if x["app_id"] == app_id]
                if not targets:
                    return
                t = targets[0]
                thresh = float(self.config.get("phase1_threshold_hours", 2.0))
                msgs = []
                if new_pt is not None:
                    t["playtime_hours"] = new_pt
                    t["phase1_done"]    = new_pt >= thresh
                    msgs.append(f"playtime = {new_pt:.1f}h")
                if new_drops is not None:
                    t["cards_remaining"] = new_drops
                    t["cards_done"]      = new_drops == 0
                    msgs.append(f"drops = {new_drops}")
                save_games(self.games)
                self._refresh_table()
                self._append_log(f"{g['name']}: " + (", ".join(msgs) if msgs else "nothing changed") + ".")
            self.after(0, _apply)

        threading.Thread(target=_fetch, daemon=True).start()

    # -----------------------------------------------------------------------
    # Idling control
    # -----------------------------------------------------------------------

    def _refresh_playtimes(self, silent: bool = False):
        if not self.config.get("api_key") or not self.config.get("steam_id"):
            if silent:
                self._append_log("Skipped playtime refresh: no API key/Steam ID set in Settings.")
            else:
                messagebox.showinfo("API key required",
                    "Enter your Steam API key and Steam ID in Settings to refresh playtimes.")
            return
        self._append_log(f"Refreshing playtimes for {len(self.games)} game(s)...")
        api_key  = self.config["api_key"]
        steam_id = self.config["steam_id"]
        thresh   = float(self.config.get("phase1_threshold_hours", 2.0))

        def _fetch():
            try:
                fetched  = fetch_owned_games(api_key, steam_id)
                pt_map   = {g["app_id"]: g["playtime_hours"] for g in fetched}
            except Exception as exc:
                self.after(0, self._append_log, f"Playtime fetch failed: {exc}")
                return

            def _apply():
                updated = 0
                for g in self.games:
                    if g["app_id"] in pt_map:
                        g["playtime_hours"] = pt_map[g["app_id"]]
                        g["phase1_done"]    = g["playtime_hours"] >= thresh
                        updated += 1
                save_games(self.games)
                self._refresh_table()
                self._append_log(f"Playtimes updated for {updated}/{len(self.games)} game(s).")
            self.after(0, _apply)

        threading.Thread(target=_fetch, daemon=True).start()

    def _start_idling(self):
        if self._running:
            return
        if not self.games:
            messagebox.showinfo("No games", "Add at least one game first.")
            return
        if not SAM_GAME_EXE.exists():
            messagebox.showerror("SAM.Game.exe missing",
                f"SAM.Game.exe was not found at:\n{SAM_GAME_EXE}\n\n"
                "Place SAM.Game.exe and SAM.API.dll in the same directory as this script.")
            return

        self._running = True
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._cards_btn.config(state="normal")

        # Refresh drop counts and playtimes first so Phase 1/2 decisions use
        # current data. Silent: if cookies/API key aren't set this just logs
        # instead of popping a dialog in the way of starting the session.
        self._refresh_all(silent=True)

        self._controller = IdleController(
            games=self.games,
            config=self.config,
            on_update=self._update_from_thread,
            on_status=self._status_from_thread,
            on_log=self._log_from_thread,
            on_done=self._on_all_done,
            on_auto_remove=self._auto_remove_from_thread,
        )
        self._thread = threading.Thread(target=self._controller.run, daemon=True)
        self._thread.start()
        self._append_log(
            "Idler started" + (" (resuming from where it left off)." if self._resumed_before else ".")
        )
        self._resumed_before = True

    def _stop_idling(self):
        if not self._running:
            return
        if self._controller:
            self._controller.stop()
        self._running = False
        self._start_btn.config(text="Resume Idling", state="normal")
        self._stop_btn.config(state="disabled")
        self._cards_btn.config(state="disabled")
        self._status_panel.update_status(IdleStatus(), False)
        self._append_log(
            "Idler paused. Nothing was reset, hit Resume Idling to continue where you left off."
        )

    def _mark_cards_dropped(self):
        if self._controller:
            self._controller.advance_phase2()
            self._append_log("Cards dropped confirmed manually.")

    # -----------------------------------------------------------------------
    # Close
    # -----------------------------------------------------------------------

    def _on_close(self):
        if self._running:
            if not messagebox.askyesno(
                "Quit",
                "Idler is running. Pause it and quit?\n"
                "(Your progress is saved either way, you can resume next time you open the app.)"
            ):
                return
            self._stop_idling()
        self.destroy()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    App().mainloop()