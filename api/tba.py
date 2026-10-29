import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from django.core.cache import cache

from utils import config_loader

TEAM_KEY = "frc2073"
X_TBA_Auth_Key = os.environ.get("X_TBA_AUTH_KEY")

TEAM_EVENTS_CACHE_TTL = 300  # seconds

TBA_BASE = "https://www.thebluealliance.com/api/v3"

# Seconds each kind of TBA data stays cached. A TBA request costs 1-1.5 s, so
# without this every page load paid that once per call. Team lists barely
# change during an event; scores and rankings move every few minutes.
TTL_TEAMS = 1800
TTL_LIVE = 60          # schedule, rankings, single matches
TTL_VIDEOS = 300
TTL_AVATAR = 86400
TTL_FAILURE = 30       # back off briefly when TBA is down instead of waiting on every request

# One keep-alive session, so repeat calls skip the TCP/TLS handshake. The pool
# is sized for the 12-worker avatar fetches in the team views.
_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16))

# Per-process copy in front of the shared Django cache. The shared cache lives
# in the database, so even a hit is a round trip; this answers instantly when
# the same server instance asks again.
_local = {}
_FAILED = object()
_LOCAL_MAX_AGE = 30


def _local_get(key):
    hit = _local.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def _fetch(path, timeout):
    """Network only: no cache and no database, so it is safe on worker threads."""
    try:
        resp = _session.get(TBA_BASE + path, headers={"X-TBA-Auth-Key": X_TBA_Auth_Key}, timeout=timeout)
        return resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def _remember(key, data, ttl, shared):
    now = time.time()
    if data is None:
        _local[key] = (now + TTL_FAILURE, _FAILED)
        return
    if shared:
        cache.set(key, data, ttl)
    _local[key] = (now + min(ttl, _LOCAL_MAX_AGE), data)


def _tba_get(path, ttl, timeout=10, shared=True):
    """GET a TBA endpoint and return the parsed JSON, or None on any failure.

    Results are cached in memory and (when `shared`) in the Django cache for
    `ttl` seconds; failures are remembered for TTL_FAILURE seconds so a TBA
    outage does not make every page wait out the full timeout. Callers on
    worker threads pass shared=False: the Django cache is a database table,
    and each thread would open its own connection."""
    key = "tba:" + path
    hit = _local_get(key)
    if hit is not None:
        return None if hit is _FAILED else hit

    if shared:
        stored = cache.get(key)
        if stored is not None:
            _local[key] = (time.time() + min(ttl, _LOCAL_MAX_AGE), stored)
            return stored

    data = _fetch(path, timeout)
    _remember(key, data, ttl, shared)
    return data


def prefetch(items, timeout=10):
    """Warm several (path, ttl) endpoints at once.

    Cache reads and writes stay on the calling thread; only the uncached
    network requests run in parallel, so three cold calls cost one round
    trip instead of three."""
    missing = []
    for path, ttl in items:
        key = "tba:" + path
        if _local_get(key) is not None:
            continue
        stored = cache.get(key)
        if stored is not None:
            _local[key] = (time.time() + min(ttl, _LOCAL_MAX_AGE), stored)
        else:
            missing.append((path, ttl))
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=len(missing)) as pool:
        results = list(pool.map(lambda item: _fetch(item[0], timeout), missing))
    for (path, ttl), data in zip(missing, results):
        _remember("tba:" + path, data, ttl, True)


def get_team_events():
    config = config_loader.get_config()
    year = str(config.get('year', 2025))
    cache_key = f"tba_team_events_{year}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    events = {}
    try:
        resp = requests.get(f"https://www.thebluealliance.com/api/v3/team/{TEAM_KEY}/events/{year}",
                            headers={"X-TBA-Auth-Key": X_TBA_Auth_Key},
                            timeout=10)
    except requests.exceptions.RequestException:
        events["testing"] = "Training"
        return events

    # If the API didn't return success, return a sensible default
    if resp.status_code != 200:
        events["testing"] = "Training"
        return events

    try:
        data = resp.json()
    except ValueError:
        # Non-JSON response (e.g., HTML error page)
        events["testing"] = "Training"
        return events

    # Some responses may be a dict with an error message or a string; normalize to list
    if isinstance(data, dict) and data.get("message"):
        events["testing"] = "Training"
        return events

    if not isinstance(data, list):
        data = [data]

    for event in data:
        # Support both dict entries (normal API) and string entries (fallbacks)
        if isinstance(event, dict):
            key = event.get("key")
            name = event.get("name")
            if key and name:
                events[key] = name
        elif isinstance(event, str):
            events[event] = event

    events["testing"] = "Training"

    # Manual addition of data, MUST wait until TBA adds these events
    # to their website or else the API will break... (do I "NEED" to make this a dynamic loader?)

    cache.set(cache_key, events, TEAM_EVENTS_CACHE_TTL)
    return events


def get_match_schedule(event_key):
    data = _tba_get(f"/event/{event_key}/matches/simple", TTL_LIVE)
    return data if isinstance(data, list) else []


def get_teams_list(event_key):
    data = _tba_get(f"/event/{event_key}/teams/simple", TTL_TEAMS)
    return data if isinstance(data, list) else []


def get_single_match(event_key, match_id):
    match_key = event_key + "_" + match_id
    try:
        raw_match = _tba_get(f"/match/{match_key}/simple", TTL_LIVE)

        # Check if alliances data exists
        if not raw_match or 'alliances' not in raw_match:
            return None
        
        match = {"red": [], "blue": []}
        for red_team in raw_match["alliances"]["red"]["team_keys"]:
            match["red"].append(red_team.split("frc")[1])
        for blue_team in raw_match["alliances"]["blue"]["team_keys"]:
            match["blue"].append(blue_team.split("frc")[1])

        return match
    except (KeyError, TypeError, IndexError):
        return None


def get_team_avatar(team_key: str, year: int) -> 'str | None':
    """Fetch the TBA avatar for a team and return a base64 data URI, or None."""
    try:
        # Called from the views' avatar thread pools, so memory cache only;
        # the views already persist avatars to Teams.logo_url.
        data = _tba_get(f"/team/{team_key}/media/{year}", TTL_AVATAR, shared=False)
        if not isinstance(data, list):
            return None
        for item in data:
            if isinstance(item, dict) and item.get("type") == "avatar":
                b64 = item.get("details", {}).get("base64Image")
                if b64:
                    return "data:image/png;base64," + b64
        return None
    except Exception:
        return None


def get_event_rankings(event_key):
    """Return {team_number: rank} for all ranked teams at an event."""
    try:
        data = _tba_get(f"/event/{event_key}/rankings", TTL_LIVE)
        if not isinstance(data, dict) or not isinstance(data.get('rankings'), list):
            return {}
        result = {}
        for entry in data['rankings']:
            key = entry.get('team_key', '')
            rank = entry.get('rank')
            if key.startswith('frc') and rank is not None:
                result[int(key[3:])] = rank
        return result
    except Exception:
        return {}


def get_match_videos(event_key):
    """Return {match_key: youtube_video_key} for all matches at an event."""
    try:
        matches = _tba_get(f"/event/{event_key}/matches", TTL_VIDEOS, timeout=15)
        if not isinstance(matches, list):
            return {}
        videos = {}
        for m in matches:
            for v in m.get('videos', []):
                if v.get('type') == 'youtube':
                    videos[m['key']] = v['key']
                    break
        return videos
    except Exception:
        return {}
