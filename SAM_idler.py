"""
SAM Idler
Idles games using SAM.Game.exe (same mechanism SAM uses internally).

Phase 1: Run all games with < 2h playtime simultaneously until each hits 2h.
Phase 2: Run each game one at a time until cards are confirmed dropped.

Automatic detection:
- Library + playtime: Steam Web API (requires API key + Steam ID)
- Card drops remaining: steamcommunity.com/my/badges (requires session cookies)

Requirements:
- SAM.Game.exe and SAM.API.dll in the same folder as this script
- Steam running and logged in
- Python 3.8+, no extra packages
"""

import json
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

PHASE1_POLL_INTERVAL = 30   # seconds between phase 1 timer checks
PHASE2_CARD_POLL_MIN = 5    # minutes between automatic card-drop checks
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


def default_game(app_id: str, name: str = "", playtime_h: float = 0.0) -> dict:
    return {
        "app_id": str(app_id).strip(),
        "name": name.strip() or f"App {app_id}",
        "playtime_hours": playtime_h,
        "cards_remaining": -1,
        "phase1_done": playtime_h >= 2.0,
        "cards_done": False,
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
# Badge page parser
# ---------------------------------------------------------------------------

class _BadgeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.drops: dict[str, int] = {}
        self._current_appid: str | None = None
        self._in_badge_row = False
        self._depth = 0         # div nesting depth since badge_row entry
        self._capture_next = False

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        cls  = attr.get("class", "")
        href = attr.get("href", "")

        if tag == "div":
            if self._in_badge_row:
                self._depth += 1
            # badge_row is_link is the outer container; exclude overlay/inner subclasses
            if ("badge_row" in cls
                    and "badge_row_overlay" not in cls
                    and "badge_row_inner" not in cls):
                self._in_badge_row = True
                self._current_appid = None
                self._depth = 1

        # The gamecards link is on the badge_row_overlay anchor, outside badge_title_row
        if tag == "a" and "badge_row_overlay" in cls:
            m = re.search(r"/gamecards/(\d+)/", href)
            if m:
                self._current_appid = m.group(1)

        if "progress_info_bold" in cls:
            self._capture_next = True

    def handle_endtag(self, tag):
        if tag == "div" and self._in_badge_row:
            self._depth -= 1
            if self._depth <= 0:
                self._in_badge_row = False
                self._depth = 0

    def handle_data(self, data):
        if self._capture_next:
            self._capture_next = False
            m = re.search(r"(\d+)\s+card drop", data.strip(), re.IGNORECASE)
            if m and self._current_appid:
                self.drops[self._current_appid] = int(m.group(1))


def fetch_card_drops(session_id: str, login_secure: str, steam_id: str = "") -> dict[str, int]:
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
        if not parser.drops and page > 1:
            break
        all_drops.update(parser.drops)
        if f"p={page + 1}" not in html:
            break
        page += 1
    return all_drops


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
    def __init__(self, games: list, config: dict, on_update, on_status, on_log, on_done):
        self.games     = games
        self.config    = config
        self.on_update = on_update
        self.on_status = on_status
        self.on_log    = on_log
        self.on_done   = on_done
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
        ts = time.strftime("%H:%M:%S")
        self.on_log(f"[{ts}] {msg}")

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
            drops = fetch_card_drops(
                self.config["session_id"],
                self.config["login_secure"],
                self.config.get("steam_id", ""),
            )
            return drops.get(app_id, 0)
        except Exception:
            return -1

    # Phase 1 ---------------------------------------------------------------

    def _run_phase1(self):
        targets = [g for g in self.games if not g["phase1_done"]]
        if not targets:
            self._status.phase = "Phase 1 skipped (all games at 2h+)"
            self._emit()
            self._log("All games already past 2h, skipping Phase 1.")
            return

        self._status.phase = "Phase 1"
        self._status.crash_notice = ""
        self._log(f"Phase 1: {len(targets)} game(s) need >= 2h, running simultaneously.")

        for g in targets:
            if self._stop.is_set():
                return
            try:
                self._start_idle(g["app_id"])
                self._log(f"Started: {g['name']} ({g['app_id']})")
            except Exception as exc:
                self._log(f"ERROR starting {g['app_id']}: {exc}")

        start_times    = {g["app_id"]: time.time() for g in targets}
        paused_secs    = {g["app_id"]: 0.0 for g in targets}   # time lost to crashes, not counted
        crash_since    = {g["app_id"]: None for g in targets}  # time.time() when crash first seen, else None
        retry_counts   = {g["app_id"]: 0 for g in targets}
        gave_up        = set()                                  # app_ids past the quick-retry burst
        last_giveup_retry = {}                                  # app_id -> last slow-retry timestamp
        last_crash_check = time.time()

        while not self._stop.is_set():
            now = time.time()

            # Liveness check, throttled so we're not calling poll() every second for nothing.
            if now - last_crash_check >= CRASH_CHECK_INTERVAL:
                last_crash_check = now
                for g in targets:
                    app_id = g["app_id"]
                    if g["phase1_done"]:
                        continue
                    alive = self._is_idle_alive(app_id)
                    if not alive and crash_since[app_id] is None:
                        crash_since[app_id] = now
                        self._status.crash_notice = f"{g['name']} stopped unexpectedly, attempting to restart..."
                        self._emit()
                    if not alive:
                        if app_id in gave_up:
                            # Already past the initial retry burst; keep trying, just less often.
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
                                f"Still paused, will keep retrying every {CRASH_GIVEUP_RETRY_INTERVAL // 60} min "
                                "in the background, time isn't counting meanwhile."
                            )
                            self._log(
                                f"{g['name']}: giving up on quick retries after {CRASH_MAX_RETRIES} attempts. "
                                f"Will keep trying every {CRASH_GIVEUP_RETRY_INTERVAL // 60} min. "
                                "Not counted as failed, its 2h clock is just paused."
                            )
                            self._emit()

            still_going = []
            for g in targets:
                app_id = g["app_id"]
                if g["phase1_done"]:
                    self._stop_idle(app_id)
                    continue
                if app_id in gave_up or crash_since[app_id] is not None:
                    # Currently crashed/paused: don't advance this game's clock.
                    still_going.append((g, None, None))
                    continue
                elapsed_h = (time.time() - start_times[app_id] - paused_secs[app_id]) / 3600
                needed_h  = max(0.0, 2.0 - g["playtime_hours"])
                if elapsed_h >= needed_h:
                    g["phase1_done"] = True
                    self._stop_idle(app_id)
                    self._log(f"{g['name']} reached 2h mark.")
                    save_games(self.games)
                    self.on_update()
                else:
                    still_going.append((g, elapsed_h, needed_h))

            self._status.phase1_running = [g["name"] for g, _, _ in still_going]

            if not still_going:
                break

            timed = [(eh, nh) for _, eh, nh in still_going if eh is not None]
            if timed:
                min_secs = min((nh - eh) * 3600 for eh, nh in timed)
                self._status.next_check_sec = min_secs
            else:
                # Everything currently in flight is crashed/paused; nothing to count down.
                self._status.next_check_sec = 0.0
            self._emit()
            self.on_update()
            self._stop.wait(PHASE1_POLL_INTERVAL)

        self._status.phase1_running = []
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
            poll_sec          = PHASE2_CARD_POLL_MIN * 60
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


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, config: dict):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.result: dict | None = None
        self._cfg = config.copy()
        # Track show/hide vars keyed by field name so _save can persist them
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

        bf = tk.Frame(self, bg=BG)
        bf.grid(row=12, column=0, columnspan=2, pady=(10, 16), padx=16, sticky="e")
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
        self.result = {
            "api_key":           self._api_key_var.get().strip(),
            "steam_id":          self._steam_id_var.get().strip(),
            "session_id":        self._session_var.get().strip(),
            "login_secure":      self._login_var.get().strip(),
            # Persist hide checkbox states
            "hide_api_key":      self._hide_vars.get("hide_api_key",      tk.BooleanVar(value=True)).get(),
            "hide_login_secure": self._hide_vars.get("hide_login_secure", tk.BooleanVar(value=True)).get(),
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
        self.geometry("760x560")
        self.grab_set()
        self.selected: list[dict] = []
        self._games      = games
        self._existing   = existing_ids
        self._unit       = unit
        self._rows: list[tuple] = []   # (frame, BooleanVar, game_dict)
        # Map app_id -> BooleanVar so selections survive filter/sort changes
        self._check_state: dict[str, tk.BooleanVar] = {}
        self._sort_key   = "default"   # "default" | "name" | "playtime"
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
        self._filter_var.trace_add("write", lambda *_: self._rebuild(keep_sel=True))
        tk.Entry(ff, textvariable=self._filter_var, bg=ENTRY_BG, fg=FG, font=FONT,
                 relief="flat", insertbackground=FG, width=22
                 ).pack(side="left", padx=(6, 0))

        self._sub2h_var = tk.BooleanVar(value=False)
        tk.Checkbutton(ff, text="Only under 2h", variable=self._sub2h_var,
                       bg=BG, fg=FG, selectcolor=BTN_BG, font=FONT, activebackground=BG,
                       command=lambda: self._rebuild(keep_sel=True)).pack(side="left", padx=(10, 0))

        tk.Label(ff, text="Sort:", bg=BG, fg=GREY, font=SMALL).pack(side="left", padx=(12, 4))
        self._sort_var = tk.StringVar(value="default")
        for val, label in (("default", "App ID"), ("name", "Name"), ("playtime", "Playtime")):
            tk.Radiobutton(
                ff, text=label, variable=self._sort_var, value=val,
                bg=BG, fg=FG, selectcolor=BTN_BG, activebackground=BG, font=SMALL,
                command=lambda: self._rebuild(keep_sel=True),
            ).pack(side="left", padx=(0, 4))

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

        # Mouse wheel scrolling — bind to canvas AND inner frame so the wheel
        # works wherever the cursor is, not just over the scrollbar.
        def _on_wheel(event):
            # Windows/macOS use event.delta; Linux uses Button-4/5
            if event.num == 4 or event.delta > 0:
                self._canvas.yview_scroll(-1, "units")
            elif event.num == 5 or event.delta < 0:
                self._canvas.yview_scroll(1, "units")
        for widget in (self._canvas, self._inner):
            widget.bind("<MouseWheel>", _on_wheel)
            widget.bind("<Button-4>",   _on_wheel)
            widget.bind("<Button-5>",   _on_wheel)
        # Also bind on child rows as they're created (done in _rebuild)
        self._wheel_handler = _on_wheel

        self._rebuild(keep_sel=False)

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
        tk.Button(bf, text="Add Selected",      bg=ACCENT,  fg="#fff", font=FONT, relief="flat",
                  padx=10, pady=5, cursor="hand2", bd=0,
                  command=self._confirm).pack(side="right")

    def _sorted_games(self) -> list[dict]:
        key = self._sort_var.get()
        if key == "name":
            return sorted(self._games, key=lambda g: g["name"].lower())
        if key == "playtime":
            return sorted(self._games, key=lambda g: g["playtime_hours"], reverse=True)
        return self._games   # "default" = original order (by app_id from API)

    def _rebuild(self, keep_sel: bool = True):
        for w in self._inner.winfo_children():
            w.destroy()
        self._rows.clear()
        ftext  = self._filter_var.get().lower()
        sub2h  = self._sub2h_var.get()
        unit   = self._unit

        for i, g in enumerate(self._sorted_games()):
            if ftext and ftext not in g["name"].lower():
                continue
            if sub2h and g["playtime_hours"] >= 2.0:
                continue

            already = g["app_id"] in self._existing

            # Reuse existing BooleanVar if keep_sel=True; otherwise init to already-in-list
            if g["app_id"] not in self._check_state:
                self._check_state[g["app_id"]] = tk.BooleanVar(value=already)
            elif not keep_sel:
                self._check_state[g["app_id"]].set(already)
            var = self._check_state[g["app_id"]]

            row_bg   = ROW_ODD if i % 2 == 0 else ROW_EVEN
            fg_color = GREY if already else FG
            f = tk.Frame(self._inner, bg=row_bg)
            f.pack(fill="x")

            tk.Checkbutton(f, variable=var, bg=row_bg, fg=fg_color,
                           selectcolor=BTN_BG, activebackground=row_bg).pack(side="left", padx=(6, 0))
            tk.Label(f, text=g["name"], bg=row_bg, fg=fg_color,
                     font=FONT, anchor="w", width=34).pack(side="left", padx=4)

            pt_display = hours_to_unit(g["playtime_hours"], unit)
            tk.Label(f, text=f"{pt_display:.1f} {unit}",
                     bg=row_bg, fg=GREY, font=FONT, width=13, anchor="e").pack(side="left")

            drops = g.get("cards_remaining", -1)
            drops_str = str(drops) if drops >= 0 else "?"
            drops_color = GREEN if drops == 0 else (ORANGE if drops > 0 else GREY)
            tk.Label(f, text=f"{drops_str} drops",
                     bg=row_bg, fg=drops_color, font=FONT, width=10, anchor="e").pack(side="left", padx=(4, 6))

            # Propagate wheel scrolling from row widgets too
            for w in (f,):
                w.bind("<MouseWheel>", self._wheel_handler)
                w.bind("<Button-4>",   self._wheel_handler)
                w.bind("<Button-5>",   self._wheel_handler)

            self._rows.append((f, var, g))

    # Selection helpers
    def _select_all(self):
        for _, v, _ in self._rows:
            v.set(True)

    def _select_none(self):
        for _, v, _ in self._rows:
            v.set(False)

    def _invert(self):
        for _, v, _ in self._rows:
            v.set(not v.get())

    def _select_with_drops(self):
        for _, v, g in self._rows:
            if g.get("cards_remaining", -1) > 0:
                v.set(True)

    def _confirm(self):
        self.selected = [g for _, v, g in self._rows if v.get()]
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

    def refresh(self, games: list, unit: str = "minutes"):
        total_drops = sum(g["cards_remaining"] for g in games if g["cards_remaining"] > 0)
        total_h     = sum(max(0.0, 2.0 - g["playtime_hours"]) for g in games if not g["phase1_done"])
        total_disp  = hours_to_unit(total_h, unit)
        p1   = sum(1 for g in games if not g["phase1_done"])
        p2   = sum(1 for g in games if g["phase1_done"] and not g["cards_done"])
        done = sum(1 for g in games if g["cards_done"])

        self._total_drops.config(text=str(total_drops) if total_drops else ("?" if any(g["cards_remaining"] < 0 for g in games) else "0"))
        self._total_pt.config(text=f"{total_disp:.0f} {unit}")
        self._games_p1.config(text=str(p1))
        self._games_p2.config(text=str(p2))
        self._games_done.config(text=str(done))


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SAM Idler")
        self.geometry("960x780")
        self.minsize(740, 580)
        self.configure(bg=BG)

        self.games, games_warning   = load_games()
        self.config, config_warning = load_config()
        self._controller: IdleController | None = None
        self._thread: threading.Thread | None   = None
        self._running = False
        self._drag_item: str | None = None
        self._last_removed: tuple | None = None
        self._resumed_before = False

        # Playtime display unit (kept in sync with a StringVar)
        self._unit_var = tk.StringVar(value=self.config.get("playtime_unit", "minutes"))
        self._unit_var.trace_add("write", self._on_unit_change)

        self._build_ui()
        self._refresh_table()
        self._summary.refresh(self.games, self._unit_var.get())

        # Clicking on empty space unfocuses any active entry/cell editor
        self.bind("<Button-1>", self._maybe_unfocus)
        self.bind("<Return>",   self._maybe_unfocus)
        self.bind("<Escape>",   self._maybe_unfocus)

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

        # Toolbar
        tb = tk.Frame(self, bg=BG)
        tb.pack(fill="x", padx=16, pady=(10, 0))
        self._mk_btn(tb, "Import from Steam", self._import_library, accent=True).pack(side="left", padx=(0, 6))
        self._mk_btn(tb, "Add via App ID",    self._add_by_id).pack(side="left", padx=(0, 6))
        self._mk_btn(tb, "Remove",            self._remove_game).pack(side="left", padx=(0, 6))
        self._undo_btn = self._mk_btn(tb, "Undo Remove", self._undo_remove)
        self._undo_btn.pack(side="left", padx=(0, 6))
        self._undo_btn.config(state="disabled")
        self._mk_btn(tb, "Refresh Drops",     self._refresh_drops).pack(side="left", padx=(0, 6))
        self._mk_btn(tb, "Settings",          self._open_settings).pack(side="right")

        # Playtime unit selector
        unit_frame = tk.Frame(tb, bg=BG)
        unit_frame.pack(side="right", padx=(0, 10))
        tk.Label(unit_frame, text="Playtime unit:", bg=BG, fg=GREY, font=SMALL).pack(side="left", padx=(0, 4))
        unit_cb = ttk.Combobox(
            unit_frame, textvariable=self._unit_var,
            values=UNITS, state="readonly", width=9, font=FONT,
        )
        unit_cb.pack(side="left")
        # Style the combobox to match dark theme
        style = ttk.Style()
        style.configure("TCombobox",
            fieldbackground=ENTRY_BG, background=BTN_BG,
            foreground=FG, arrowcolor=FG, selectbackground=ACCENT)

        # Summary bar
        self._summary = SummaryBar(self)
        self._summary.pack(fill="x", padx=16, pady=(10, 0))

        # Game table
        list_frame = tk.Frame(self, bg=BG)
        list_frame.pack(fill="both", expand=True, padx=16, pady=(8, 0))

        tk.Label(list_frame,
                 text="Drag rows to reorder. Double-click a cell to edit. Phase 2 idles in list order.",
                 font=SMALL, bg=BG, fg=GREY, anchor="w").pack(anchor="w", pady=(0, 4))

        cols = ("order", "app_id", "name", "playtime", "drops", "phase1", "cards")
        self._tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="browse")
        self._style_tree()

        self._tree.heading("order",    text="#")
        self._tree.heading("app_id",   text="App ID")
        self._tree.heading("name",     text="Name")
        self._tree.heading("playtime", text="Playtime")
        self._tree.heading("drops",    text="Drops left")
        self._tree.heading("phase1",   text="2h done")
        self._tree.heading("cards",    text="Cards done")

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

        # Move buttons
        of = tk.Frame(self, bg=BG)
        of.pack(fill="x", padx=16, pady=(4, 0))
        self._mk_btn(of, "Move Up",   self._move_up).pack(side="left", padx=(0, 4))
        self._mk_btn(of, "Move Down", self._move_down).pack(side="left")

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

        tk.Label(log_frame, text="Log", font=BOLD, bg=BG, fg=FG).pack(anchor="w")
        self._log_text = tk.Text(
            log_frame, height=6, state="disabled",
            bg=ENTRY_BG, fg=FG, font=MONO, relief="flat", wrap="word", bd=0,
        )
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

    def _maybe_unfocus(self, event=None):
        """
        Shift keyboard focus to the main window when the user clicks on empty
        space, presses Enter, or presses Escape. This dismisses any active
        inline cell editor (which commits on FocusOut) and clears the cursor
        from any entry widget.
        """
        focused = self.focus_get()
        if focused and focused is not self:
            self.focus_set()

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
        self._summary.refresh(self.games, self._unit)

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

    def _refresh_table(self):
        sel     = self._tree.selection()
        sel_iid = sel[0] if sel else None
        self._tree.delete(*self._tree.get_children())
        for i, g in enumerate(self.games):
            if g["cards_done"]:
                tag = "done"
            elif g["phase1_done"]:
                tag = "active"
            elif i % 2 == 0:
                tag = "even"
            else:
                tag = "odd"
            drops_str = str(g["cards_remaining"]) if g["cards_remaining"] >= 0 else "?"
            self._tree.insert("", "end", iid=str(i),
                values=(
                    i + 1,
                    g["app_id"],
                    g["name"],
                    self._playtime_display(g["playtime_hours"]),
                    drops_str,
                    "yes" if g["phase1_done"] else "no",
                    "yes" if g["cards_done"]  else "no",
                ),
                tags=(tag,))
        if sel_iid and self._tree.exists(sel_iid):
            self._tree.selection_set(sel_iid)
        self._summary.refresh(self.games, self._unit)

    # -----------------------------------------------------------------------
    # Inline cell editing
    # -----------------------------------------------------------------------

    # Columns that are editable and what kind of edit they need
    _EDITABLE = {
        "order":    "order",
        "app_id":   "app_id",
        "name":     "text",
        "playtime": "playtime",
        "phase1":   "toggle",
        "cards":    "toggle",
    }

    def _on_double_click(self, event):
        region = self._tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        iid = self._tree.identify_row(event.y)
        col = self._tree.identify_column(event.x)  # e.g. "#3"
        if not iid or not col:
            return

        col_idx  = int(col[1:]) - 1
        col_name = self._tree["columns"][col_idx]
        edit_type = self._EDITABLE.get(col_name)
        if not edit_type:
            return

        idx = int(iid)
        g   = self.games[idx]

        if edit_type == "toggle":
            # Flip boolean
            if col_name == "phase1":
                g["phase1_done"] = not g["phase1_done"]
            else:
                g["cards_done"] = not g["cards_done"]
            save_games(self.games)
            self._refresh_table()
            return

        if edit_type == "order":
            current_val = str(idx + 1)
        elif edit_type == "playtime":
            current_val = f"{hours_to_unit(g['playtime_hours'], self._unit):.4g}"
        elif edit_type == "app_id":
            current_val = g["app_id"]
        else:
            current_val = g["name"]

        _CellEditor(self._tree, iid, col_name, current_val, self._commit_edit)

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
                g["name"] = stripped
            save_games(self.games)
            self._refresh_table()
            return

        if col_name == "app_id":
            digits = re.sub(r"[^\d]", "", raw_val)
            if not digits:
                self._append_log(f"App ID edit: '{raw_val}' has no digits, App ID left unchanged.")
                return
            if digits != g["app_id"] and any(other["app_id"] == digits for other in self.games if other is not g):
                self._append_log(f"App ID {digits} is already used elsewhere in the list, but changing it anyway.")
            g["app_id"] = digits
            save_games(self.games)
            self._refresh_table()
            return

        if col_name == "playtime":
            hours = parse_playtime(raw_val, self._unit)
            g["playtime_hours"] = hours
            g["phase1_done"]    = hours >= 2.0
            save_games(self.games)
            self._refresh_table()
            return

    # -----------------------------------------------------------------------
    # Drag reorder
    # -----------------------------------------------------------------------

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
        self.games[idx - 1], self.games[idx] = self.games[idx], self.games[idx - 1]
        save_games(self.games)
        self._refresh_table()
        if self._tree.exists(str(idx - 1)):
            self._tree.selection_set(str(idx - 1))

    def _move_down(self):
        idx = self._selected_index()
        if idx is None or idx >= len(self.games) - 1:
            return
        self.games[idx], self.games[idx + 1] = self.games[idx + 1], self.games[idx]
        save_games(self.games)
        self._refresh_table()
        if self._tree.exists(str(idx + 1)):
            self._tree.selection_set(str(idx + 1))

    # -----------------------------------------------------------------------
    # Log
    # -----------------------------------------------------------------------

    def _append_log(self, msg: str):
        self._log_text.config(state="normal")
        self._log_text.insert("end", msg + "\n")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _log_from_thread(self, msg: str):
        self.after(0, self._append_log, msg)

    # -----------------------------------------------------------------------
    # Thread callbacks
    # -----------------------------------------------------------------------

    def _update_from_thread(self):
        self.after(0, self._refresh_table)

    def _status_from_thread(self, st: IdleStatus):
        self.after(0, self._status_panel.update_status, st, self._running)

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

    def _open_settings(self):
        dlg = SettingsDialog(self, self.config)
        if dlg.result:
            self.config.update(dlg.result)
            save_config(self.config)
            self._update_cards_hint()

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

        def _fetch():
            try:
                games = fetch_owned_games(self.config["api_key"], self.config["steam_id"])
                self.after(0, self._show_import_dialog, games)
            except Exception as exc:
                self.after(0, messagebox.showerror, "Error", f"Library fetch failed:\n{exc}")

        threading.Thread(target=_fetch, daemon=True).start()

    def _show_import_dialog(self, fetched: list):
        self._append_log(f"Fetched {len(fetched)} games.")
        existing = {g["app_id"] for g in self.games}
        dlg = ImportDialog(self, fetched, existing, unit=self._unit)
        added = 0
        skipped = 0
        for g in dlg.selected:
            if g["app_id"] not in existing:
                self.games.append(default_game(g["app_id"], g["name"], g["playtime_hours"]))
                added += 1
            else:
                skipped += 1
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
        self.games.append(default_game(app_id, name or "", hours))
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

    # -----------------------------------------------------------------------
    # Refresh drops
    # -----------------------------------------------------------------------

    def _refresh_drops(self):
        if not self.config.get("session_id") or not self.config.get("login_secure"):
            messagebox.showinfo("Cookies required",
                "Enter your sessionid and steamLoginSecure in Settings first.")
            return
        self._append_log("Refreshing card drop counts...")

        def _fetch():
            try:
                drops = fetch_card_drops(
                    self.config["session_id"],
                    self.config["login_secure"],
                    self.config.get("steam_id", ""),
                )
                def _apply():
                    for g in self.games:
                        if g["app_id"] in drops:
                            g["cards_remaining"] = drops[g["app_id"]]
                            g["cards_done"] = False
                        elif g["cards_remaining"] != 0:
                            g["cards_remaining"] = 0
                    save_games(self.games)
                    self._refresh_table()
                    self._append_log(
                        f"Drop counts refreshed. {len(drops)} game(s) have drops remaining."
                    )
                self.after(0, _apply)
            except Exception as exc:
                self.after(0, self._append_log, f"Drop refresh failed: {exc}")

        threading.Thread(target=_fetch, daemon=True).start()

    # -----------------------------------------------------------------------
    # Idling control
    # -----------------------------------------------------------------------

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

        self._controller = IdleController(
            games=self.games,
            config=self.config,
            on_update=self._update_from_thread,
            on_status=self._status_from_thread,
            on_log=self._log_from_thread,
            on_done=self._on_all_done,
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