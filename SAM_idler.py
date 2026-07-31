"""
SAM Idler
Idles Steam games using SAM.Game.exe to farm trading card drops.

Idle modes (configurable in Settings):
  multi          - Run all games simultaneously forever. Cards drop while running,
                   roughly every 30 min per card on a normal account. Running
                   multiple games at once slows the per-game drop rate but lets
                   all games clock time in parallel. Good for large libraries.
  solo           - One game at a time. Checks drops periodically and moves on
                   automatically. Best per-game drop rate.
  multi_then_solo- Multi-idle until a playtime threshold, then switch to solo.
                   Useful for accounts with a drop delay (new accounts or
                   accounts that have made recent refunds).
  fast_cycle     - Multi-idle for an interval, then rapidly stop/restart each
                   game in sequence to flush pending drops, then repeat.

Drop detection:
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
import queue
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
    Parse a user-typed playtime string and return hours.
    Accepts the current unit by default, but also recognises explicit suffixes:
      3h / 3hr / 3hours -> hours
      90m / 90min / 90minutes -> minutes
      45s / 45sec / 45seconds -> seconds
      2d / 2days -> days
    Examples: '1.5', '3h', '90m', '1,5h', '45s'
    If no suffix is given, the current display unit is assumed.
    """
    if raw is None:
        return 0.0
    s = raw.strip().replace(" ", "").lower()

    # Check for explicit unit suffix (letters at the end)
    suffix_match = re.match(r"^([0-9.,]+)(h(?:r|ours?)?|m(?:in(?:utes?)?)?|s(?:ec(?:onds?)?)?|d(?:ays?)?)$", s)
    if suffix_match:
        num_str, sfx = suffix_match.groups()
        if sfx.startswith("h"):
            explicit_unit = "hours"
        elif sfx.startswith("m"):
            explicit_unit = "minutes"
        elif sfx.startswith("s"):
            explicit_unit = "seconds"
        else:
            explicit_unit = "days"
        s = num_str
        unit = explicit_unit

    def _fix_comma(t: str) -> str:
        if t.startswith(","):
            t = "0." + t[1:]
        t = re.sub(r"(\d),(\d{1,2})$", r"\1.\2", t)
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


def phase1_done_for_playtime(hours: float, config: dict) -> bool:
    """
    Whether a game counts as "solo ready" for a given playtime.
    This threshold only means anything in multi_then_solo mode, where it's
    the gate for handing a game off from multi-idle to solo-idle. In every
    other mode there is no handoff, so the flag is left permanently True
    (its harmless default) instead of being recomputed against a threshold
    that doesn't apply to the current mode.
    """
    if config.get("idle_mode", "multi") != "multi_then_solo":
        return True
    thresh_h = float(config.get("phase1_threshold_seconds", 7200.0) / 3600)
    return hours >= thresh_h


# ---------------------------------------------------------------------------
# Config / game persistence
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    "config_version": 2,
    "api_key": "", "steam_id": "",
    "session_id": "", "login_secure": "",
    "playtime_unit": "minutes",
    "hide_api_key": True,
    "hide_login_secure": True,
    "idle_mode": "multi",
    "phase1_threshold_seconds": 7200.0,
    "phase2_poll_seconds": 300.0,
    "fast_cycle_seconds": 300.0,
    "fast_cycle_stop_pause_seconds": 5.0,
    "merge_refresh_buttons": False,
    "auto_remove_completed": False,
    "minimize_to_tray": False,
    "auto_start_idling": False,
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
            _migrate_config(cfg)
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


def _migrate_config(cfg: dict) -> None:
    """Convert old per-unit config keys to the unified *_seconds keys in place."""
    if "phase1_threshold_hours" in cfg and "phase1_threshold_seconds" not in cfg:
        cfg["phase1_threshold_seconds"] = float(cfg.pop("phase1_threshold_hours")) * 3600
    if "phase2_poll_minutes" in cfg and "phase2_poll_seconds" not in cfg:
        cfg["phase2_poll_seconds"] = float(cfg.pop("phase2_poll_minutes")) * 60
    if "fast_cycle_minutes" in cfg and "fast_cycle_seconds" not in cfg:
        cfg["fast_cycle_seconds"] = float(cfg.pop("fast_cycle_minutes")) * 60
    # Stamp current version so future migrations can be keyed on it.
    cfg.setdefault("config_version", 2)


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
    vac_raw = entry.get("vac_enabled")
    vac_enabled = True if vac_raw is True else (False if vac_raw is False else None)
    return {
        "app_id": app_id,
        "name": name,
        "playtime_hours": playtime,
        "cards_remaining": cards_remaining,
        "phase1_done": bool(entry.get("phase1_done", True)),
        "cards_done": bool(entry.get("cards_done", False)),
        "vac_enabled": vac_enabled,
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
        "phase1_done": True,
        "cards_done": (cards_remaining == 0),
    }


# ---------------------------------------------------------------------------
# Steam API helpers
# ---------------------------------------------------------------------------

def _http_get(url: str, cookies: dict | None = None, timeout: int = 15) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
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

    parser = _ProgressInfoParser()
    parser.feed(html)

    # Same session-validity check used by fetch_card_drops_bulk (the badges
    # page): look for <a class="user_avatar">, which Steam renders in the
    # page header whenever the request cookies are valid. Also accept the
    # data-userinfo "logged_in" JSON flag as a fallback in case Steam ever
    # omits the avatar link on this page layout but still marks the session
    # as logged in. Only fail if BOTH signals say we're not authorized.
    is_logged_in = parser.is_authorized or '"logged_in":true' in html or '"logged_in": true' in html
    if not is_logged_in:
        _maybe_dump_debug_html(f"unauthorized_gamecards_{app_id}", html)
        raise ValueError(
            "Steam didn't recognize the session (not logged in on the gamecards page). "
            "Your session cookies have likely expired. Re-enter them in Settings."
        )

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


def is_vac_enabled(app_id: str) -> bool | None:
    """
    Returns True if the app has VAC enabled, False if not, None if the check failed.
    Uses the Steam store appdetails API which is public and requires no auth.
    category id 8 = VAC enabled.
    """
    try:
        url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&filters=categories"
        raw = _http_get(url)
        data = json.loads(raw)
        app_data = data.get(str(app_id), {})
        if not app_data.get("success"):
            return None
        categories = app_data.get("data", {}).get("categories", [])
        return any(c.get("id") == 8 for c in categories)
    except Exception:
        return None


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
        self.eta_sec: float = -1.0   # -1 means unknown; >= 0 is estimated seconds until game is done
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

    def _run_phase1(self, force_infinite: bool = False):
        threshold_h = float(self.config.get("phase1_threshold_seconds", 7200.0) / 3600)
        infinite    = force_infinite or threshold_h <= 0.0

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
            # In infinite mode there is no target time so set -1 as sentinel.
            timed = [(eh, nh) for _, eh, nh in still_going if eh is not None and nh is not None]
            if timed:
                max_secs = max((nh - eh) * 3600 for eh, nh in timed)
                self._status.next_check_sec = max_secs
            elif infinite:
                self._status.next_check_sec = -1.0  # sentinel: running indefinitely
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
            self._log("No games need solo idling.")
            return

        self._log(f"Phase 2: {len(targets)} game(s) to card-idle.")

        for g in targets:
            if self._stop.is_set():
                return

            self._next.clear()
            app_id = g["app_id"]
            self._status.phase         = "Solo"
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
            poll_sec          = max(1.0, float(self.config.get("phase2_poll_seconds", PHASE2_CARD_POLL_MIN * 60)))
            paused_secs       = 0.0   # total crash time since game_start, for elapsed_sec
            paused_since_poll = 0.0   # crash time since last_poll, for the poll countdown
            crash_since  = None
            retry_count  = 0
            gave_up      = False
            last_giveup_retry = 0.0
            last_crash_check  = time.time()
            # ETA tracking: record (elapsed_sec, drops_remaining) each time a
            # drop count is confirmed so we can estimate remaining time.
            _eta_samples: list[tuple[float, int]] = []
            if g["cards_remaining"] > 0:
                _eta_samples.append((0.0, g["cards_remaining"]))

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

                elapsed_sec = now - game_start - paused_secs
                self._status.elapsed_sec    = elapsed_sec
                self._status.next_check_sec = max(0.0, poll_sec - (now - last_poll - paused_since_poll))

                # Recompute ETA from samples: fit a linear drop rate and project.
                if len(_eta_samples) >= 2:
                    t0, d0 = _eta_samples[0]
                    t1, d1 = _eta_samples[-1]
                    dropped = d0 - d1
                    if dropped > 0 and t1 > t0:
                        secs_per_drop = (t1 - t0) / dropped
                        drops_left = g["cards_remaining"]
                        self._status.eta_sec = drops_left * secs_per_drop if drops_left > 0 else 0.0
                    else:
                        self._status.eta_sec = -1.0
                else:
                    self._status.eta_sec = -1.0
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
                        _eta_samples.append((elapsed_sec, drops))
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
        self._log("Solo mode complete.")
        save_games(self.games)
        self.on_update()

    # Fast cycle ------------------------------------------------------------

    def _run_fast_cycle(self):
        """
        Fast cycle mode: start all games simultaneously (same as multi mode),
        then once they have been running long enough, rapidly stop and restart
        each one in sequence. Stopping a game causes Steam to flush any pending
        drop that has already been earned server-side but not yet delivered.

        Drop interval is ~30 min per card on unrestricted accounts. This mode
        starts all games at once to clock that time in parallel, then cycles
        through them to collect the drops, then repeats.
        """
        targets = [g for g in self.games if not g["cards_done"]]
        if not targets:
            self._status.phase = "Nothing to idle (all cards done)"
            self._emit()
            self._log("Fast cycle: no games need cards.")
            return

        cycle_minutes = float(self.config.get("fast_cycle_seconds", 300.0)) / 60
        poll_sec      = max(1.0, float(self.config.get("phase2_poll_seconds", PHASE2_CARD_POLL_MIN * 60)))

        self._log(
            f"Fast cycle: {len(targets)} game(s). "
            f"Running all simultaneously for {cycle_minutes:.0f} min, then cycling through each to collect drops."
        )

        while not self._stop.is_set():
            # Refresh target list each outer loop in case some finished
            targets = [g for g in self.games if not g["cards_done"]]
            if not targets:
                break

            # Start all games
            self._status.phase = f"Fast cycle: multi-idling {len(targets)} game(s)"
            self._status.phase1_running = [g["name"] for g in targets]
            self._emit()

            for g in targets:
                if self._stop.is_set():
                    return
                try:
                    self._start_idle(g["app_id"])
                except Exception as exc:
                    self._log(f"ERROR starting {g['app_id']}: {exc}")

            # Wait for the cycle duration
            wait_start = time.time()
            cycle_sec  = cycle_minutes * 60
            while not self._stop.is_set():
                elapsed = time.time() - wait_start
                self._status.next_check_sec = max(0.0, cycle_sec - elapsed)
                self._emit()
                if elapsed >= cycle_sec:
                    break
                self._stop.wait(1)

            if self._stop.is_set():
                return

            # Stop and restart each game briefly to flush pending drops
            self._status.phase1_running = []
            for g in targets:
                if self._stop.is_set():
                    return
                if g["cards_done"]:
                    continue
                app_id = g["app_id"]
                self._status.phase      = f"Fast cycle: collecting from {g['name']}"
                self._status.active_game   = g["name"]
                self._status.active_app_id = app_id
                self._emit()

                self._stop_idle(app_id)
                stop_pause = float(self.config.get("fast_cycle_stop_pause_seconds", 5.0))
                self._stop.wait(stop_pause)
                if self._stop.is_set():
                    return

                if self._has_cookies():
                    drops = self._check_drops(app_id)
                    g["cards_remaining"] = drops
                    save_games(self.games)
                    self.on_update()
                    if drops == 0:
                        g["cards_done"] = True
                        self._log(f"{g['name']}: 0 drops remaining, done.")
                        save_games(self.games)
                        self.on_update()
                        if self.config.get("auto_remove_completed", False):
                            self.on_auto_remove(app_id)
                        continue
                    elif drops > 0:
                        self._log(f"{g['name']}: {drops} drop(s) remaining.")

                # Restart the game for the next cycle
                try:
                    self._start_idle(app_id)
                except Exception as exc:
                    self._log(f"ERROR restarting {g['app_id']}: {exc}")

            self._status.active_game   = ""
            self._status.active_app_id = ""

        self._status.phase1_running = []
        self._status.phase = "All done"
        self._emit()
        self._log("Fast cycle complete.")
        save_games(self.games)
        self.on_update()

    # Entry -----------------------------------------------------------------

    def run(self):
        mode = self.config.get("idle_mode", "multi")
        try:
            if mode == "solo":
                # Solo mode: skip multi-idle entirely, just do one-at-a-time
                self._run_phase2()
            elif mode == "multi_then_solo":
                # Multi until threshold, then solo for drops
                self._run_phase1()
                if not self._stop.is_set():
                    self._run_phase2()
            elif mode == "fast_cycle":
                self._run_fast_cycle()
            else:
                # Default: "multi" - run everything simultaneously forever
                self._run_phase1(force_infinite=True)

            if not self._stop.is_set():
                self._status.phase       = "All done"
                self._status.active_game = ""
                self._emit()
                self._log("Session complete.")
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


def bind_entry_keys(entry: tk.Entry, on_escape=None, on_enter=None) -> None:
    """
    Attach a complete set of expected keyboard behaviours to a Tk Entry widget:
    - Ctrl+Backspace / Ctrl+Delete: delete previous/next word
    - Ctrl+A: select all
    - Right-click: cut/copy/paste/select-all context menu
    - Escape: clear selection and unfocus (calls on_escape if provided, else focus_set on parent)
    - Return/KP_Enter: unfocus (calls on_enter if provided, same fallback)
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

    def _select_all(event):
        entry.select_range(0, "end")
        entry.icursor("end")
        return "break"

    def _do_unfocus(callback):
        try:
            entry.select_clear()
        except Exception:
            pass
        if callback:
            callback()
        else:
            try:
                entry.winfo_toplevel().focus_set()
            except Exception:
                pass

    def _make_menu(event):
        m = tk.Menu(entry, tearoff=0, bg=BTN_BG, fg=FG,
                    activebackground=ACCENT, activeforeground="#fff",
                    relief="flat", bd=0)
        m.add_command(label="Cut",        command=lambda: entry.event_generate("<<Cut>>"))
        m.add_command(label="Copy",       command=lambda: entry.event_generate("<<Copy>>"))
        m.add_command(label="Paste",      command=lambda: entry.event_generate("<<Paste>>"))
        m.add_separator()
        m.add_command(label="Select All", command=lambda: (_select_all(None),))
        m.tk_popup(event.x_root, event.y_root)

    entry.bind("<Control-BackSpace>", _delete_word_back)
    entry.bind("<Control-Delete>",    _delete_word_forward)
    entry.bind("<Control-a>",         _select_all)
    entry.bind("<Control-A>",         _select_all)
    entry.bind("<Button-3>",          _make_menu)
    entry.bind("<Escape>",            lambda e: (_do_unfocus(on_escape), "break")[1])
    entry.bind("<Return>",            lambda e: (_do_unfocus(on_enter),  "break")[1])
    entry.bind("<KP_Enter>",          lambda e: (_do_unfocus(on_enter),  "break")[1])


# Keep the old name as an alias so call sites that haven't been updated yet still work.
def bind_word_delete(entry: tk.Entry) -> None:
    bind_entry_keys(entry)



# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

def _sec_to_display(seconds: float) -> tuple[str, int | float]:
    """Pick the most readable unit for a seconds value and return (unit, value)."""
    if seconds < 60:
        return "seconds", int(seconds)
    if seconds < 3600:
        val = seconds / 60
        return "minutes", int(val) if val == int(val) else round(val, 1)
    val = seconds / 3600
    return "hours", int(val) if val == int(val) else round(val, 1)


def _display_to_sec(val_str: str, unit: str) -> float:
    multipliers = {"seconds": 1.0, "minutes": 60.0, "hours": 3600.0}
    try:
        return max(0.0, float(val_str.strip().replace(",", ".")) * multipliers.get(unit, 60.0))
    except ValueError:
        return multipliers.get(unit, 60.0)  # default to 1 unit


class VacWarningDialog(tk.Toplevel):
    """
    Shown before starting the idler if any game in the list is VAC-enabled.
    Offers three choices instead of a plain yes/no: start anyway, remove the
    VAC games from the list and start, or cancel. self.result ends up as
    one of "start", "remove_and_start", or None (cancelled / closed).
    """
    def __init__(self, parent, vac_names: list[str]):
        super().__init__(parent)
        self.title("VAC-enabled games in list")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.result: str | None = None
        self._build(vac_names)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.wait_window()

    def _build(self, vac_names: list[str]):
        pad = dict(padx=18)
        tk.Label(self, text="VAC-enabled games in list", font=TITLE, bg=BG, fg=WARN).pack(
            anchor="w", pady=(14, 8), **pad
        )

        shown = vac_names[:10]
        names_text = "\n".join(f"  -  {n}" for n in shown)
        if len(vac_names) > 10:
            names_text += f"\n  ...  and {len(vac_names) - 10} more"
        tk.Label(self, text="The following game(s) have VAC enabled:", bg=BG, fg=FG, font=FONT,
                 anchor="w", justify="left").pack(anchor="w", pady=(0, 4), **pad)
        names_lbl = tk.Label(self, text=names_text, bg=BG, fg=FG, font=MONO,
                              anchor="w", justify="left")
        names_lbl.pack(anchor="w", pady=(0, 10), **pad)

        body = (
            "Running the idler while connected to a VAC-secured server on the same machine "
            "can get you kicked or game-banned from that server. This will not happen just "
            "from idling, only if you also play a VAC game at the same time.\n\n"
            "Pause the idler before launching any VAC-protected multiplayer game."
        )
        tk.Label(self, text=body, bg=BG, fg=GREY, font=SMALL, wraplength=420,
                 anchor="w", justify="left").pack(anchor="w", pady=(0, 14), **pad)

        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(anchor="e", pady=(0, 14), **pad)

        def _mk(text, command, accent=False, danger=False):
            bg = ACCENT if accent else ("#7a2020" if danger else BTN_BG)
            fg = "#fff" if (accent or danger) else FG
            return tk.Button(btn_row, text=text, command=command, bg=bg, fg=fg,
                              activebackground=ACCENT, activeforeground="#fff",
                              font=FONT, relief="flat", padx=10, pady=5, cursor="hand2", bd=0)

        _mk("Cancel", self._cancel).pack(side="left", padx=(0, 6))
        _mk("Remove VAC games and start", self._remove_and_start, danger=True).pack(side="left", padx=(0, 6))
        _mk("Start anyway", self._start, accent=True).pack(side="left")

    def _start(self):
        self.result = "start"
        self.destroy()

    def _remove_and_start(self):
        self.result = "remove_and_start"
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


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
        self.bind("<Button-1>", self._maybe_unfocus_on_click)
        self.bind("<Escape>",   lambda e: self.focus_set())
        self.wait_window()

    def _maybe_unfocus_on_click(self, event):
        if isinstance(event.widget, tk.Entry):
            return
        self.focus_set()

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        # ── Title ────────────────────────────────────────────────────────────
        tk.Label(self, text="Settings", font=TITLE, bg=BG, fg=ACCENT).grid(
            row=0, column=0, columnspan=2, padx=16, pady=(14, 10), sticky="w"
        )

        # ── Left panel: credentials ──────────────────────────────────────────
        left = tk.Frame(self, bg=BG)
        left.grid(row=1, column=0, padx=(16, 8), pady=0, sticky="nwe")

        tk.Label(left, text="Steam Web API", bg=BG, fg=ACCENT, font=FONT).pack(anchor="w", pady=(0, 2))
        tk.Label(left, text="Required for library import and playtime refresh.",
                 bg=BG, fg=GREY, font=SMALL).pack(anchor="w")

        def lfield(parent, label, key, hideable=None, extra_btn=None):
            tk.Label(parent, text=label, bg=BG, fg=FG, font=FONT, anchor="w").pack(anchor="w", pady=(8, 1))
            var = tk.StringVar(value=self._cfg.get(key, ""))
            row_f = tk.Frame(parent, bg=BG)
            row_f.pack(anchor="w", fill="x")
            initial_show = "*" if (hideable and self._cfg.get(hideable, True)) else ""
            entry = tk.Entry(row_f, textvariable=var, bg=ENTRY_BG, fg=FG, font=FONT,
                             relief="flat", insertbackground=FG, width=28, show=initial_show,
                             exportselection=True)
            entry.pack(side="left")
            bind_entry_keys(entry, on_escape=lambda: self.focus_set())
            if hideable:
                hide_var = tk.BooleanVar(value=self._cfg.get(hideable, True))
                self._hide_vars[hideable] = hide_var
                def _toggle_show(*_, hv=hide_var, e=entry):
                    e.config(show="*" if hv.get() else "")
                hide_var.trace_add("write", _toggle_show)
                tk.Checkbutton(row_f, text="Hide", variable=hide_var,
                               bg=BG, fg=GREY, selectcolor=BTN_BG,
                               activebackground=BG, font=SMALL).pack(side="left", padx=(8, 0))
            if extra_btn:
                text_b, cmd = extra_btn
                tk.Button(row_f, text=text_b, bg=BTN_BG, fg=FG, font=SMALL,
                          relief="flat", padx=6, pady=3, cursor="hand2", bd=0,
                          command=cmd).pack(side="left", padx=(6, 0))
            return var

        self._api_key_var = lfield(left, "API Key", "api_key", hideable="hide_api_key")
        lnk = tk.Label(left, text="Get an API key →", bg=BG, fg=ACCENT, font=SMALL, cursor="hand2")
        lnk.pack(anchor="w", pady=(2, 0))
        lnk.bind("<Button-1>", lambda e: webbrowser.open("https://steamcommunity.com/dev/apikey"))

        self._steam_id_var = lfield(left, "Steam ID / vanity name", "steam_id",
                                    extra_btn=("Look up", self._lookup_steam_id))
        tk.Label(left, text="Paste your 64-bit ID, profile URL, or vanity name.",
                 bg=BG, fg=GREY, font=SMALL, wraplength=300, justify="left").pack(anchor="w", pady=(2, 0))

        tk.Frame(left, bg=GREY, height=1).pack(fill="x", pady=(14, 10))

        tk.Label(left, text="Session cookies", bg=BG, fg=ACCENT, font=FONT).pack(anchor="w", pady=(0, 2))
        tk.Label(left,
                 text="Optional. Required for automatic drop detection.\n"
                      "Log into steamcommunity.com in your browser, open\n"
                      "DevTools (F12) → Application → Cookies, and copy\n"
                      "the values for sessionid and steamLoginSecure here.",
                 bg=BG, fg=GREY, font=SMALL, justify="left").pack(anchor="w")
        lnk2 = tk.Label(left, text="Open steamcommunity.com →", bg=BG, fg=ACCENT, font=SMALL, cursor="hand2")
        lnk2.pack(anchor="w", pady=(2, 0))
        lnk2.bind("<Button-1>", lambda e: webbrowser.open("https://steamcommunity.com/"))

        self._session_var = lfield(left, "sessionid",        "session_id")
        self._login_var   = lfield(left, "steamLoginSecure", "login_secure", hideable="hide_login_secure")
        tk.Label(left, text="Cookies expire periodically. If drop detection stops working, re-enter them.\n"
                            "You don't need to keep the browser tab open.",
                 bg=BG, fg=GREY, font=SMALL, justify="left").pack(anchor="w", pady=(4, 0))

        # ── Right panel: behaviour ───────────────────────────────────────────
        right = tk.Frame(self, bg=BG)
        right.grid(row=1, column=1, padx=(8, 16), pady=0, sticky="nwe")

        tk.Label(right, text="Display", bg=BG, fg=ACCENT, font=FONT).pack(anchor="w", pady=(0, 6))
        unit_row = tk.Frame(right, bg=BG)
        unit_row.pack(anchor="w")
        tk.Label(unit_row, text="Playtime unit", bg=BG, fg=FG, font=FONT).pack(side="left", padx=(0, 8))
        self._unit_cb = ttk.Combobox(unit_row, textvariable=self._unit_var,
                                     values=UNITS, state="readonly", width=10, font=FONT)
        self._unit_cb.pack(side="left")

        tk.Frame(right, bg=GREY, height=1).pack(fill="x", pady=(12, 10))

        tk.Label(right, text="Idle mode", bg=BG, fg=ACCENT, font=FONT).pack(anchor="w", pady=(0, 6))

        _MODE_INFO = [
            ("multi",          "Multi-idle",
             "Run all games simultaneously forever.\nGood for large libraries, slower drops per game."),
            ("solo",           "Solo",
             "One game at a time, full drop rate.\nMoves on automatically when a game hits 0 drops."),
            ("multi_then_solo","Multi then solo",
             "Multi-idle until a playtime threshold, then switch\nto solo. Use if your account has a 2-hour drop delay."),
            ("fast_cycle",     "Fast cycle",
             "Multi-idle for an interval, then stop/restart each\ngame to flush pending drops, then repeat."),
        ]

        self._mode_var = tk.StringVar(value=self._cfg.get("idle_mode", "multi"))
        for val, label, desc in _MODE_INFO:
            rb_f = tk.Frame(right, bg=BG)
            rb_f.pack(anchor="w", pady=(0, 4))
            tk.Radiobutton(rb_f, text=label, variable=self._mode_var, value=val,
                           bg=BG, fg=FG, selectcolor=BTN_BG, activebackground=BG,
                           font=FONT, command=self._on_mode_change).pack(anchor="w")
            tk.Label(rb_f, text=desc, bg=BG, fg=GREY, font=SMALL,
                     justify="left").pack(anchor="w", padx=(22, 0))

        # Per-mode options
        def time_row(parent, label, val_var, unit_var, units, hint):
            f = tk.Frame(parent, bg=BG)
            tk.Label(f, text=label, bg=BG, fg=FG, font=FONT).pack(anchor="w", pady=(6, 1))
            inner = tk.Frame(f, bg=BG)
            inner.pack(anchor="w")
            e = tk.Entry(inner, textvariable=val_var, bg=ENTRY_BG, fg=FG, font=FONT,
                         relief="flat", insertbackground=FG, width=6)
            e.pack(side="left")
            bind_entry_keys(e, on_escape=lambda: self.focus_set())
            ttk.Combobox(inner, textvariable=unit_var, values=units,
                         state="readonly", width=8, font=FONT).pack(side="left", padx=(4, 0))
            if hint:
                tk.Label(inner, text=hint, bg=BG, fg=GREY, font=SMALL).pack(side="left", padx=(6, 0))
            return f

        thresh_sec = float(self._cfg.get("phase1_threshold_seconds", 7200.0))
        tu, tv = _sec_to_display(thresh_sec)
        self._thresh_val_var  = tk.StringVar(value=str(tv))
        self._thresh_unit_var = tk.StringVar(value=tu)
        self._thresh_frame = time_row(right, "Switch to solo after",
                                      self._thresh_val_var, self._thresh_unit_var,
                                      ["seconds", "minutes", "hours"], "per game")

        poll_sec = float(self._cfg.get("phase2_poll_seconds", 300.0))
        pu, pv = _sec_to_display(poll_sec)
        self._poll_val_var  = tk.StringVar(value=str(pv))
        self._poll_unit_var = tk.StringVar(value=pu)
        self._poll_frame = time_row(right, "Check for drops every",
                                    self._poll_val_var, self._poll_unit_var,
                                    ["seconds", "minutes", "hours"], "(requires cookies)")

        cycle_sec = float(self._cfg.get("fast_cycle_seconds", 300.0))
        cu, cv = _sec_to_display(cycle_sec)
        self._cycle_val_var  = tk.StringVar(value=str(cv))
        self._cycle_unit_var = tk.StringVar(value=cu)
        self._cycle_frame = time_row(right, "Multi-idle for",
                                     self._cycle_val_var, self._cycle_unit_var,
                                     ["seconds", "minutes", "hours"], "per cycle")

        pause_sec = float(self._cfg.get("fast_cycle_stop_pause_seconds", 5.0))
        pau, pav = _sec_to_display(pause_sec)
        self._pause_val_var  = tk.StringVar(value=str(pav))
        self._pause_unit_var = tk.StringVar(value=pau)
        self._pause_frame = time_row(right, "Pause after stopping each game",
                                     self._pause_val_var, self._pause_unit_var,
                                     ["seconds", "minutes"], "for Steam to register")

        self._on_mode_change()

        tk.Frame(right, bg=GREY, height=1).pack(fill="x", pady=(14, 10))

        tk.Label(right, text="Behaviour", bg=BG, fg=ACCENT, font=FONT).pack(anchor="w", pady=(0, 6))

        self._merge_refresh_var = tk.BooleanVar(value=self._cfg.get("merge_refresh_buttons", False))
        tk.Checkbutton(right, text='Merge "Refresh Drops" and "Refresh Playtimes"\ninto a single "Refresh" button',
                       variable=self._merge_refresh_var,
                       bg=BG, fg=FG, selectcolor=BTN_BG, activebackground=BG,
                       font=FONT, justify="left").pack(anchor="w", pady=(0, 4))

        self._auto_remove_var = tk.BooleanVar(value=self._cfg.get("auto_remove_completed", False))
        tk.Checkbutton(right, text="Auto-remove games once all cards are dropped",
                       variable=self._auto_remove_var,
                       bg=BG, fg=FG, selectcolor=BTN_BG, activebackground=BG,
                       font=FONT).pack(anchor="w", pady=(0, 4))

        self._auto_start_var = tk.BooleanVar(value=self._cfg.get("auto_start_idling", False))
        tk.Checkbutton(right, text="Start idling automatically on launch",
                       variable=self._auto_start_var,
                       bg=BG, fg=FG, selectcolor=BTN_BG, activebackground=BG,
                       font=FONT).pack(anchor="w", pady=(0, 4))

        self._tray_var = tk.BooleanVar(value=self._cfg.get("minimize_to_tray", False))
        tray_cb = tk.Checkbutton(right, text="Minimize to system tray instead of closing\n(requires pystray + Pillow)",
                                 variable=self._tray_var,
                                 bg=BG, fg=FG, selectcolor=BTN_BG, activebackground=BG,
                                 font=FONT, justify="left")
        tray_cb.pack(anchor="w")

        # ── Bottom bar: Save / Cancel ────────────────────────────────────────
        sep = tk.Frame(self, bg=GREY, height=1)
        sep.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(14, 0))
        bf = tk.Frame(self, bg=BG)
        bf.grid(row=3, column=0, columnspan=2, pady=(10, 16), padx=16, sticky="e")
        tk.Button(bf, text="Save",   bg=ACCENT, fg="#fff", font=FONT, relief="flat",
                  padx=10, pady=5, cursor="hand2", bd=0, command=self._save
                  ).pack(side="right", padx=(6, 0))
        tk.Button(bf, text="Cancel", bg=BTN_BG, fg=FG, font=FONT, relief="flat",
                  padx=10, pady=5, cursor="hand2", bd=0, command=self.destroy
                  ).pack(side="right")

    def _on_mode_change(self):
        mode = self._mode_var.get()
        if mode == "multi_then_solo":
            self._thresh_frame.pack(anchor="w")
        else:
            self._thresh_frame.pack_forget()
        if mode in ("solo", "multi_then_solo"):
            self._poll_frame.pack(anchor="w")
        else:
            self._poll_frame.pack_forget()
        if mode == "fast_cycle":
            self._cycle_frame.pack(anchor="w")
            self._pause_frame.pack(anchor="w")
        else:
            self._cycle_frame.pack_forget()
            self._pause_frame.pack_forget()

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
        thresh_sec = _display_to_sec(self._thresh_val_var.get(), self._thresh_unit_var.get())
        poll_sec   = max(1.0, _display_to_sec(self._poll_val_var.get(), self._poll_unit_var.get()))
        cycle_sec  = max(1.0, _display_to_sec(self._cycle_val_var.get(), self._cycle_unit_var.get()))
        pause_sec  = max(0.5, _display_to_sec(self._pause_val_var.get(), self._pause_unit_var.get()))
        self.result = {
            "api_key":                       self._api_key_var.get().strip(),
            "steam_id":                      self._steam_id_var.get().strip(),
            "session_id":                    self._session_var.get().strip(),
            "login_secure":                  self._login_var.get().strip(),
            "playtime_unit":                 self._unit_var.get(),
            "idle_mode":                     self._mode_var.get(),
            "phase1_threshold_seconds":      thresh_sec,
            "phase2_poll_seconds":           poll_sec,
            "fast_cycle_seconds":            cycle_sec,
            "fast_cycle_stop_pause_seconds": pause_sec,
            "merge_refresh_buttons":         self._merge_refresh_var.get(),
            "auto_remove_completed":         self._auto_remove_var.get(),
            "auto_start_idling":             self._auto_start_var.get(),
            "minimize_to_tray":              self._tray_var.get(),
            "hide_api_key":                  self._hide_vars.get("hide_api_key",      tk.BooleanVar(value=True)).get(),
            "hide_login_secure":             self._hide_vars.get("hide_login_secure", tk.BooleanVar(value=True)).get(),
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

        self._not_in_list_var = tk.BooleanVar(value=False)
        tk.Checkbutton(ff, text="Only not in list", variable=self._not_in_list_var,
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
        not_in_list = self._not_in_list_var.get()

        # Un-pack everything first so pack order can be rebuilt cleanly.
        for w in self._row_widgets.values():
            w["frame"].pack_forget()

        visible_count = 0
        for g in self._sorted_games():
            app_id = g["app_id"]
            if ftext and ftext not in g["name"].lower() and ftext not in app_id:
                continue
            if not_in_list and app_id in self._existing:
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
        if ftext or not_in_list:
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
        bind_entry_keys(self)

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

        self._lbl(2, 0, "ETA (this game)", fg=GREY, font=SMALL)
        self._eta_val     = self._lbl(2, 1, font=MONO)
        self._lbl(2, 2, "Running (Phase 1)", fg=GREY, font=SMALL)
        self._p1list_val  = self._lbl(2, 3, font=SMALL)

        self._crash_val = self._lbl(3, 0, fg=WARN, font=SMALL, columnspan=4)

    def update_status(self, st: IdleStatus, running: bool):
        if not running:
            self._phase_val.config(text="Idle", fg=GREY)
            self._game_val.config(text="")
            self._elapsed_val.config(text="")
            self._check_val.config(text="")
            self._eta_val.config(text="")
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
            if st.eta_sec >= 0:
                self._eta_val.config(text=_fmt_time(st.eta_sec))
            else:
                self._eta_val.config(text="estimating..." if st.elapsed_sec > 0 else "")
        else:
            self._game_val.config(text="")
            self._elapsed_val.config(text="")
            self._check_val.config(text="")
            self._eta_val.config(text="")

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
        lbl = tk.Label(self, text=label, bg=PANEL_BG, fg=GREY, font=SMALL)
        lbl.grid(row=0, column=col, sticky="w", padx=(0, 4))
        val = tk.Label(self, text="", bg=PANEL_BG, fg=FG, font=BIG)
        val.grid(row=1, column=col, sticky="w", padx=(0, 28))
        return lbl, val

    def _build(self):
        self._lbl_drops,  self._total_drops = self._stat(0, "Total drops left")
        self._lbl_pt,     self._total_pt    = self._stat(1, "Multi-idle time left")
        self._lbl_p1,     self._games_p1    = self._stat(2, "Not yet solo-ready")
        self._lbl_p2,     self._games_p2    = self._stat(3, "Solo queue")
        self._lbl_done,   self._games_done  = self._stat(4, "Done")

    def _show(self, col, lbl, val, text):
        lbl.grid(row=0, column=col, sticky="w", padx=(0, 4))
        val.config(text=text)
        val.grid(row=1, column=col, sticky="w", padx=(0, 28))

    def _hide(self, lbl, val):
        lbl.grid_remove()
        val.grid_remove()

    def refresh(self, games: list, unit: str = "minutes", threshold_h: float = 0.0,
                phase1_remaining_sec: float | None = None, idle_mode: str = "multi",
                is_running: bool = False):
        total_drops = sum(g["cards_remaining"] for g in games if g["cards_remaining"] > 0)
        done = sum(1 for g in games if g["cards_done"])
        drops_text = str(total_drops) if total_drops else (
            "?" if any(g["cards_remaining"] < 0 for g in games) else "0")

        self._show(0, self._lbl_drops, self._total_drops, drops_text)
        self._show(4, self._lbl_done,  self._games_done,  str(done))

        if idle_mode == "multi_then_solo":
            # Show all five stats
            p1 = sum(1 for g in games if not g["phase1_done"])
            p2 = sum(1 for g in games if g["phase1_done"] and not g["cards_done"])

            if not is_running:
                if threshold_h <= 0.0:
                    pt_text = "∞"
                else:
                    max_h = max(
                        (max(0.0, threshold_h - g["playtime_hours"]) for g in games if not g["phase1_done"]),
                        default=0.0,
                    )
                    pt_text = f"~{hours_to_unit(max_h, unit):.0f} {unit}" if max_h > 0 else "0"
            elif phase1_remaining_sec is None:
                pt_text = "n/a"
            elif phase1_remaining_sec < 0:
                pt_text = "∞"
            else:
                pt_text = f"{hours_to_unit(phase1_remaining_sec / 3600, unit):.0f} {unit}"

            self._show(1, self._lbl_pt, self._total_pt, pt_text)
            self._show(2, self._lbl_p1, self._games_p1, str(p1))
            self._show(3, self._lbl_p2, self._games_p2, str(p2))

        elif idle_mode == "multi":
            # Multi-idle time left (∞ or countdown), no solo-ready columns
            if not is_running:
                pt_text = "∞" if threshold_h <= 0.0 else "n/a"
            elif phase1_remaining_sec is not None and phase1_remaining_sec < 0:
                pt_text = "∞"
            elif phase1_remaining_sec is not None:
                pt_text = f"{hours_to_unit(phase1_remaining_sec / 3600, unit):.0f} {unit}"
            else:
                pt_text = "n/a"
            self._show(1, self._lbl_pt, self._total_pt, pt_text)
            self._hide(self._lbl_p1, self._games_p1)
            self._hide(self._lbl_p2, self._games_p2)

        else:
            # solo / fast_cycle: no multi phase, no solo-ready concept
            self._hide(self._lbl_pt, self._total_pt)
            self._hide(self._lbl_p1, self._games_p1)
            self._hide(self._lbl_p2, self._games_p2)


# ---------------------------------------------------------------------------
# Wrapping two-block row (used by the toolbar)
# ---------------------------------------------------------------------------

class _WrapRow(tk.Frame):
    """
    A container that lays out two child frames (set via set_children),
    both anchored to the TOP of the container: the left one flush with the
    left edge, the right one flush with the right edge -- like a toolbar
    with actions on the left and Refresh/Settings pinned to the top-right
    corner, even when the left block is taller (e.g. spans two rows).
    When the window gets too narrow for both to fit on one line, the right
    block drops to its own row below the left block (still right-aligned)
    instead of clipping.
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
        # anchor="nw"/"ne": pack's anchor controls placement within the
        # widget's allocated slot on BOTH axes, not just the side it was
        # packed to. Without the "n", a single-row right block ends up
        # vertically centered next to a taller multi-row left block instead
        # of sitting flush with its top.
        self._left.pack(in_=self, side="left", anchor="nw")
        self._right.pack(in_=self, side="right", anchor="ne")
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
            self._left.pack(in_=self, side="top", anchor="nw", fill="x")
            self._right.pack(in_=self, side="top", anchor="ne", fill="x", pady=(6, 0))
        else:
            self._left.pack(in_=self, side="left", anchor="nw")
            self._right.pack(in_=self, side="right", anchor="ne")


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

        # phase1_done is only meaningful in multi_then_solo mode.
        # In all other modes every game is implicitly solo-ready, so
        # normalise on load to avoid stale values causing wrong counts.
        if self.config.get("idle_mode", "multi") != "multi_then_solo":
            for g in self.games:
                g["phase1_done"] = True
        self._controller: IdleController | None = None
        self._thread: threading.Thread | None   = None
        self._running = False
        self._drag_item: str | None = None
        self._resumed_before = False
        self._sort_col: str = "order"
        self._sort_desc: bool = False
        self._undo_stack: list[list[dict]] = []
        self._redo_stack: list[list[dict]] = []
        self._undo_limit = 50

        # Thread-safe hand-off from background threads (IdleController's
        # run loop, and the various *_fetch worker threads) to the GUI
        # thread. Calling self.after(0, ...) directly FROM a background
        # thread is not reliably safe -- Tcl/Tk's threading model can raise
        # "main thread is not in main loop", hang, or silently drop the
        # call depending on timing and how busy the main loop already is.
        # This was the real reason auto-remove-completed kept silently not
        # working even after the game-list reference bug was fixed: the
        # scheduled removal callback itself was never guaranteed to run.
        # Background threads only ever put a (callable, args) pair on this
        # queue; only _drain_dispatch_queue (scheduled by itself, always
        # running on the GUI thread) ever calls self.after().
        self._dispatch_queue: "queue.Queue[tuple]" = queue.Queue()
        self._drain_dispatch_queue()

        # Playtime display unit (kept in sync with a StringVar)
        self._unit_var = tk.StringVar(value=self.config.get("playtime_unit", "minutes"))
        self._unit_var.trace_add("write", self._on_unit_change)

        self._build_ui()
        self._refresh_table()
        self._summary.refresh(self.games, self._unit_var.get(), threshold_h=float(self.config.get("phase1_threshold_seconds", 7200.0) / 3600), idle_mode=self.config.get("idle_mode", "multi"), is_running=self._running)
        self.after(500, self._check_vac_in_background)

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
        self._tray_icon = None

        # Auto-start: begin idling immediately after a short delay so the UI
        # has time to finish rendering before the controller thread starts.
        if self.config.get("auto_start_idling", False) and self.games and SAM_GAME_EXE.exists():
            self.after(800, self._start_idling)

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
        self._undo_btn = self._mk_btn(tb_left_row1, "Undo", self._undo)
        self._undo_btn.pack(side="left")
        self._undo_btn.config(state="disabled")

        # Row 2: remove completed, remove all, full reset, force kill all sam
        self._mk_btn(tb_left_row2, "Remove Completed",  self._remove_completed, danger=True).pack(side="left", padx=(0, 6))
        self._mk_btn(tb_left_row2, "Remove All",        self._remove_all,  danger=True).pack(side="left", padx=(0, 6))
        self._mk_btn(tb_left_row2, "Full Reset",        self._full_reset,  danger=True).pack(side="left", padx=(0, 6))
        self._mk_btn(tb_left_row2, "Force Kill All SAM", self._force_kill_all, danger=True).pack(side="left")

        tb_right = tk.Frame(tb_wrap, bg=BG)
        self._refresh_drops_btn = self._mk_btn(tb_right, "Refresh Drops", self._refresh_drops)
        self._refresh_pt_btn    = self._mk_btn(tb_right, "Refresh Playtimes", self._refresh_playtimes)
        self._settings_btn      = self._mk_btn(tb_right, "Settings", self._open_settings)
        self._refresh_btn = self._refresh_drops_btn
        # Initial pack order: Refresh Drops, Refresh Playtimes, Settings
        self._refresh_drops_btn.pack(side="left", padx=(0, 6))
        self._refresh_pt_btn.pack(side="left", padx=(0, 6))
        self._settings_btn.pack(side="left")

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
        bind_entry_keys(search_entry)
        self._search_count_lbl = tk.Label(search_frame, text="", bg=BG, fg=GREY, font=SMALL)
        self._search_count_lbl.pack(side="left", padx=(6, 0))
        self._mk_btn(search_frame, "Clear", lambda: self._search_var.set("")).pack(side="left", padx=(4, 0))

        # Game table
        list_frame = tk.Frame(self, bg=BG)
        list_frame.pack(fill="both", expand=True, padx=16, pady=(8, 0))

        tk.Label(list_frame,
                 text="Drag rows to reorder. Double-click a cell to edit. Solo mode idles in list order.",
                 font=SMALL, bg=BG, fg=GREY, anchor="w").pack(anchor="w", pady=(0, 4))

        cols = ("order", "app_id", "name", "playtime", "drops", "phase1", "cards", "vac")
        self._tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="extended")
        self._style_tree()

        self._tree.heading("order",    text="#",         command=lambda: self._sort_by("order"))
        self._tree.heading("app_id",   text="App ID",    command=lambda: self._sort_by("app_id"))
        self._tree.heading("name",     text="Name",      command=lambda: self._sort_by("name"))
        self._tree.heading("playtime", text="Playtime",  command=lambda: self._sort_by("playtime"))
        self._tree.heading("drops",    text="Drops left",command=lambda: self._sort_by("drops"))
        self._tree.heading("phase1",   text="Solo ready", command=lambda: self._sort_by("phase1"))
        self._tree.heading("cards",    text="Cards done",command=lambda: self._sort_by("cards"))
        self._tree.heading("vac",      text="VAC",       command=lambda: self._sort_by("vac"))

        self._tree.column("order",    width=38,  anchor="center", stretch=False)
        self._tree.column("app_id",   width=82,  anchor="center", stretch=False)
        self._tree.column("name",     width=270)
        self._tree.column("playtime", width=110, anchor="center", stretch=False)
        self._tree.column("drops",    width=78,  anchor="center", stretch=False)
        self._tree.column("phase1",   width=70,  anchor="center", stretch=False)
        self._tree.column("cards",    width=84,  anchor="center", stretch=False)
        self._tree.column("vac",      width=44,  anchor="center", stretch=False)

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
            state="disabled",
        )
        # Selectable but read-only: re-enable Ctrl+A and Ctrl+C through the disabled state
        self._log_text.bind("<Control-a>", lambda e: (self._log_text.tag_add("sel", "1.0", "end"), "break"))
        self._log_text.bind("<Control-A>", lambda e: (self._log_text.tag_add("sel", "1.0", "end"), "break"))
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
    # Thread-safe dispatch to the GUI thread
    # -----------------------------------------------------------------------

    def _dispatch(self, fn, *args):
        """
        Safe to call from ANY thread, including the IdleController's
        background thread and the various *_fetch worker threads. Queues
        fn(*args) to run on the GUI thread shortly. Never calls self.after()
        directly -- only _drain_dispatch_queue does that, and it always
        runs on the GUI thread (it reschedules itself), so Tk is never
        touched from anywhere but the thread that owns it.
        """
        self._dispatch_queue.put((fn, args))

    def _drain_dispatch_queue(self):
        # Runs on the GUI thread only: either the initial call from
        # __init__, or a reschedule of itself via self.after below.
        try:
            while True:
                fn, args = self._dispatch_queue.get_nowait()
                try:
                    fn(*args)
                except Exception as exc:
                    # Don't let one bad callback kill the drain loop --
                    # log it and keep processing the rest of the queue.
                    print(f"Dispatch callback error: {exc}")
        except queue.Empty:
            pass
        self.after(75, self._drain_dispatch_queue)

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
        self._redo_stack.clear()
        self._undo_btn.config(state="normal")

    def _on_ctrl_z(self, event=None):
        focused = self.focus_get()
        if isinstance(focused, (tk.Entry, tk.Spinbox, ttk.Entry, ttk.Combobox, tk.Text)):
            return
        self._undo()

    def _on_ctrl_y(self, event=None):
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
        self._undo_btn.config(state="normal" if self._undo_stack else "disabled")

    def _redo(self):
        if not self._redo_stack:
            self._append_log("Nothing to redo.")
            return
        self._undo_stack.append([dict(g) for g in self.games])
        self.games[:] = self._redo_stack.pop()
        save_games(self.games)
        self._refresh_table()
        self._append_log("Redid last undone change.")
        self._undo_btn.config(state="normal")

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
        self._summary.refresh(self.games, self._unit, threshold_h=float(self.config.get("phase1_threshold_seconds", 7200.0) / 3600), idle_mode=self.config.get("idle_mode", "multi"), is_running=self._running)

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
            "phase1":   "Solo ready",
            "cards":    "Cards done",
        }
        arrow = " ↓" if self._sort_desc else " ↑"
        for col, base in labels.items():
            text = base + arrow if col == self._sort_col else base
            self._tree.heading(col, text=text)

    def _update_column_visibility(self):
        """Solo ready only means anything in multi_then_solo mode (it's the
        multi-idle -> solo handoff flag). In every other mode it's not a
        real state the user set, just whatever it happened to default to,
        so showing it as a column invites reading meaning into a value that
        has none. Hide it outside multi_then_solo, matching the summary bar
        which already hides its solo-ready stats the same way."""
        all_cols = ("order", "app_id", "name", "playtime", "drops", "phase1", "cards", "vac")
        if self.config.get("idle_mode", "multi") == "multi_then_solo":
            self._tree.configure(displaycolumns=all_cols)
        else:
            self._tree.configure(displaycolumns=tuple(c for c in all_cols if c != "phase1"))

    def _refresh_table(self):
        sel     = self._tree.selection()
        sel_iids = set(sel)
        self._tree.delete(*self._tree.get_children())

        self._update_heading_arrows()
        self._update_column_visibility()

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
            vac_val = "yes" if g.get("vac_enabled") is True else ("no" if g.get("vac_enabled") is False else "?")
            self._tree.insert("", "end", iid=str(orig_idx),
                values=(
                    orig_idx + 1,
                    g["app_id"],
                    g["name"],
                    self._playtime_display(g["playtime_hours"]),
                    drops_str,
                    "yes" if g["phase1_done"] else "no",
                    "yes" if g["cards_done"]  else "no",
                    vac_val,
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

        self._summary.refresh(self.games, self._unit, threshold_h=float(self.config.get("phase1_threshold_seconds", 7200.0) / 3600), idle_mode=self.config.get("idle_mode", "multi"), is_running=self._running)

        # Auto-remove sweep: catches manual toggles, bulk edits, startup, etc.
        # The Phase 2 loop has its own call to on_auto_remove for the active
        # game; this handles everything else.
        if self.config.get("auto_remove_completed", False):
            to_remove = {g["app_id"] for g in self.games if g["cards_done"]}
            if to_remove:
                self.games[:] = [g for g in self.games if g["app_id"] not in to_remove]
                save_games(self.games)
                for app_id in to_remove:
                    self._append_log(f"Auto-removed {app_id} (cards done).")
                # Redraw without removed games. Can't call _refresh_table again
                # (recursion) so rebuild the treeview rows directly here.
                self._tree.delete(*self._tree.get_children())
                for display_pos, (orig_idx, g) in enumerate(self._sorted_games_for_display()):
                    if search_norm:
                        name_norm = self._filter_normalize(g["name"])
                        if search_norm not in name_norm and search_norm not in g["app_id"]:
                            continue
                    tag = "active" if g["phase1_done"] else ("even" if display_pos % 2 == 0 else "odd")
                    drops_str = str(g["cards_remaining"]) if g["cards_remaining"] >= 0 else "?"
                    vac_val = "yes" if g.get("vac_enabled") is True else ("no" if g.get("vac_enabled") is False else "?")
                    self._tree.insert("", "end", iid=str(orig_idx),
                        values=(
                            orig_idx + 1,
                            g["app_id"],
                            g["name"],
                            self._playtime_display(g["playtime_hours"]),
                            drops_str,
                            "yes" if g["phase1_done"] else "no",
                            "yes" if g["cards_done"]  else "no",
                            vac_val,
                        ),
                        tags=(tag,))
                self._summary.refresh(self.games, self._unit, threshold_h=float(self.config.get("phase1_threshold_seconds", 7200.0) / 3600), idle_mode=self.config.get("idle_mode", "multi"), is_running=self._running)

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
                self.games[idx]["phase1_done"] = phase1_done_for_playtime(hours, self.config)
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
            g["phase1_done"]    = phase1_done_for_playtime(hours, self.config)
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
        thresh = float(self.config.get("phase1_threshold_seconds", 7200.0) / 3600)
        label_2h = f"Mark {len(selected)} game(s) solo ready" if multi else (
            "Mark solo ready" if not g["phase1_done"] else "Mark NOT solo ready"
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
        self._log_text.config(state="normal")
        self._log_text.insert("end", line + "\n")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _log_from_thread(self, msg: str):
        self._dispatch(self._append_log, msg)

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
        self._dispatch(_apply)

    def _update_from_thread(self):
        self._dispatch(self._refresh_table)

    def _status_from_thread(self, st: IdleStatus):
        def _apply():
            self._status_panel.update_status(st, self._running)
            if self._running and st.phase1_running:
                self._summary.refresh(
                    self.games, self._unit,
                    threshold_h=float(self.config.get("phase1_threshold_seconds", 7200.0) / 3600),
                    phase1_remaining_sec=st.next_check_sec,
                    idle_mode=self.config.get("idle_mode", "multi"),
                    is_running=True,
                )
        self._dispatch(_apply)

    def _on_all_done(self):
        self._dispatch(self._handle_all_done)

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
        # Unpack all three, then re-pack in the correct order so there is
        # never a chance of Settings ending up between the refresh buttons.
        self._refresh_drops_btn.pack_forget()
        self._refresh_pt_btn.pack_forget()
        self._settings_btn.pack_forget()
        if self.config.get("merge_refresh_buttons", False):
            self._refresh_drops_btn.config(text="Refresh", command=self._refresh_all)
            self._refresh_drops_btn.pack(side="left", padx=(0, 6))
        else:
            self._refresh_drops_btn.config(text="Refresh Drops", command=self._refresh_drops)
            self._refresh_drops_btn.pack(side="left", padx=(0, 6))
            self._refresh_pt_btn.pack(side="left", padx=(0, 6))
        self._settings_btn.pack(side="left")

    def _refresh_all(self, silent: bool = False):
        self._refresh_drops(silent=silent)
        self._refresh_playtimes(silent=silent)

    def _force_kill_all(self):
        import subprocess as sp
        import platform as _platform
        try:
            if _platform.system() == "Windows":
                sp.run(["taskkill", "/F", "/IM", "SAM.Game.exe"],
                       stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            else:
                sp.run(["pkill", "-f", "SAM.Game.exe"],
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
            # If mode changed away from multi_then_solo, solo-ready is no
            # longer a meaningful concept so normalise all games to True.
            if self.config.get("idle_mode", "multi") != "multi_then_solo":
                for g in self.games:
                    g["phase1_done"] = True
                save_games(self.games)
            self._refresh_table()

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
                self._dispatch(messagebox.showerror, "Error", f"Library fetch failed:\n{exc}")
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
                    self._dispatch(self._append_log, f"Drop counts unavailable for import: {exc}")
            self._dispatch(self._show_import_dialog, games)

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
            self._check_vac_in_background()
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

        # VAC check — runs in background so it doesn't block the UI for long
        vac = is_vac_enabled(app_id)
        if vac is True:
            proceed = messagebox.askyesno(
                "VAC-enabled game",
                f"App {app_id} has VAC (Valve Anti-Cheat) enabled.\n\n"
                "Idling a VAC game while you are actively playing another VAC-secured game "
                "on the same machine can cause you to be kicked or temporarily banned from "
                "that game's servers.\n\n"
                "To be safe: pause the idler before launching any VAC-protected multiplayer game.\n\n"
                "Add it anyway?",
                parent=self,
            )
            if not proceed:
                return

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
        self._push_undo()
        game = default_game(app_id, name or "", hours)
        game["phase1_done"] = phase1_done_for_playtime(hours, self.config)
        if vac is True:
            game["vac_enabled"] = True
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
        save_games(self.games)
        self._refresh_table()
        self._append_log(f"Removed {g['name']} ({g['app_id']}). Press Ctrl+Z or click Undo to bring it back.")

    def _undo_remove(self):
        # Legacy method kept so any serialised calls don't break; just delegates.
        self._undo()

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
        save_games(self.games)
        self._refresh_table()
        self._append_log(f"Removed all {count} game(s).")

    def _full_reset(self):
        if not messagebox.askyesno(
            "Full Reset",
            "This will delete all games, all settings (API key, cookies, preferences), "
            "and the entire undo history.\n\n"
            "The app will be in the same state as a fresh launch. This cannot be undone. Are you sure?",
        ):
            return
        # Wipe everything in memory
        self.games.clear()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._undo_btn.config(state="disabled")
        # Wipe both data files from disk
        for path in (DATA_FILE, CONFIG_FILE):
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass
        # Reset config to defaults
        self.config.clear()
        self.config.update(dict(_DEFAULT_CONFIG))
        self._unit_var.set(self.config.get("playtime_unit", "minutes"))
        self._apply_refresh_button_mode()
        self._update_cards_hint()
        self._refresh_table()
        self._append_log("Full reset: all data and settings cleared.")

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
                self._dispatch(self._append_log, f"Bulk drop scrape skipped: {exc}")

            unresolved = [g for g in games_snapshot if g["app_id"] not in confirmed]
            for i, g in enumerate(unresolved):
                try:
                    confirmed[g["app_id"]] = fetch_app_card_drops(
                        session_id, login_secure, g["app_id"], steam_id
                    )
                except Exception as exc:
                    self._dispatch(self._append_log, f"{g['name']}: {exc}")
                if (i + 1) % 5 == 0:
                    self._dispatch(self._append_log,
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
            self._dispatch(_apply)

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
                    self._dispatch(self._append_log, f"Playtime fetch failed: {exc}")
            if has_cookies:
                try:
                    new_drops = fetch_app_card_drops(session_id, login_secure, app_id, steam_id)
                except Exception as exc:
                    self._dispatch(self._append_log, f"Drop check failed: {exc}")

            def _apply():
                targets = [x for x in self.games if x["app_id"] == app_id]
                if not targets:
                    return
                t = targets[0]
                msgs = []
                if new_pt is not None:
                    t["playtime_hours"] = new_pt
                    t["phase1_done"]    = phase1_done_for_playtime(new_pt, self.config)
                    msgs.append(f"playtime = {new_pt:.1f}h")
                if new_drops is not None:
                    t["cards_remaining"] = new_drops
                    t["cards_done"]      = new_drops == 0
                    msgs.append(f"drops = {new_drops}")
                save_games(self.games)
                self._refresh_table()
                self._append_log(f"{g['name']}: " + (", ".join(msgs) if msgs else "nothing changed") + ".")
            self._dispatch(_apply)

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

        def _fetch():
            try:
                fetched  = fetch_owned_games(api_key, steam_id)
                pt_map   = {g["app_id"]: g["playtime_hours"] for g in fetched}
            except Exception as exc:
                self._dispatch(self._append_log, f"Playtime fetch failed: {exc}")
                return

            def _apply():
                updated = 0
                for g in self.games:
                    if g["app_id"] in pt_map:
                        g["playtime_hours"] = pt_map[g["app_id"]]
                        g["phase1_done"]    = phase1_done_for_playtime(g["playtime_hours"], self.config)
                        updated += 1
                save_games(self.games)
                self._refresh_table()
                self._append_log(f"Playtimes updated for {updated}/{len(self.games)} game(s).")
            self._dispatch(_apply)

        threading.Thread(target=_fetch, daemon=True).start()

    def _check_vac_in_background(self):
        """Check VAC status for any games without a stored vac_enabled value.
        Runs in a background thread, updates game dicts and refreshes the table
        as results come in one by one."""
        unchecked = [g for g in self.games if g.get("vac_enabled") is None]
        if not unchecked:
            return
        self._append_log(f"Checking VAC status for {len(unchecked)} game(s)...")
        def _run():
            for g in unchecked:
                result = is_vac_enabled(g["app_id"])
                if result is not None:
                    g["vac_enabled"] = result
                self._dispatch(self._refresh_table)
            save_games(self.games)
            vac_count = sum(1 for g in self.games if g.get("vac_enabled") is True)
            if vac_count:
                self._dispatch(lambda: self._append_log(
                    f"VAC check done: {vac_count} VAC-enabled game(s) in your list. "
                    "Pause the idler before playing any VAC-secured game."
                ))
            else:
                self._dispatch(lambda: self._append_log("VAC check done: no VAC-enabled games found."))
        threading.Thread(target=_run, daemon=True).start()

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

        vac_games = [g["name"] for g in self.games if g.get("vac_enabled") is True]
        if vac_games:
            dlg = VacWarningDialog(self, vac_games)
            if dlg.result is None:
                return
            if dlg.result == "remove_and_start":
                self._push_undo()
                removed = len(vac_games)
                self.games[:] = [g for g in self.games if g.get("vac_enabled") is not True]
                save_games(self.games)
                self._refresh_table()
                self._append_log(f"Removed {removed} VAC-enabled game(s) before starting.")
                if not self.games:
                    messagebox.showinfo("No games", "All games in the list were VAC-enabled and have been removed. Add at least one non-VAC game first.")
                    return
            # dlg.result == "start" falls through and idles as normal

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
    # System tray
    # -----------------------------------------------------------------------

    def _try_build_tray(self):
        """
        Build a pystray system-tray icon. Returns True on success.
        Fails gracefully if pystray or Pillow are not installed.
        """
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return False

        # Build a simple 64x64 dark icon with an "S" letter.
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([2, 2, size - 2, size - 2], fill="#3a7ebf")
        draw.text((18, 14), "S", fill="#ffffff")

        def _on_restore(icon, item):
            self._tray_restore()

        def _on_quit(icon, item):
            self._tray_quit()

        menu = pystray.Menu(
            pystray.MenuItem("Show / Restore", _on_restore, default=True),
            pystray.MenuItem("Quit", _on_quit),
        )
        self._tray_icon = pystray.Icon("SAM Idler", img, "SAM Idler", menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()
        return True

    def _tray_restore(self):
        """Called from the tray menu to bring the window back."""
        self.after(0, self._do_restore)

    def _do_restore(self):
        self.deiconify()
        self.lift()
        self.focus_force()
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None

    def _tray_quit(self):
        """Called from tray 'Quit' menu item."""
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None
        self.after(0, self._do_quit)

    def _do_quit(self):
        if self._running:
            self._stop_idling()
        self.destroy()

    # -----------------------------------------------------------------------
    # Close
    # -----------------------------------------------------------------------

    def _on_close(self):
        if self.config.get("minimize_to_tray", False):
            # Try to hide to tray; only prompt/quit if tray setup fails.
            if self._try_build_tray():
                self.withdraw()
                return
            # pystray not available: fall through to normal quit behaviour
            # but warn once so the user knows why.
            self._append_log(
                "Minimize to tray is enabled but pystray or Pillow is not installed. "
                "Install them with: pip install pystray pillow"
            )
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