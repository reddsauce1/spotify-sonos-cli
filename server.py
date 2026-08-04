import anthropic
import cherrypy
import spotipy
from spotipy.exceptions import SpotifyBaseException, SpotifyException
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError
import requests
import functools
import inspect
import datetime
import json
import logging
import logging.handlers
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse

# Application logging.
#
# Deliberately NOT logging.basicConfig(): cherrypy.error and cherrypy.access
# both carry their own handlers *and* propagate=True, so adding a root handler
# duplicates every access-log line. Attaching an explicit handler to each
# named logger, with propagate off, keeps them from feeding each other.
#
# All three write to one file, because an application error is far easier to
# read next to the request that caused it than in a separate file.
#
# The process owns the file rather than letting launchd capture stdout,
# because launchd never rotates and nothing else was rotating either: the UI
# polls /nowplaying every ten seconds, which is about 1.1MB of access log a
# day, growing without bound. It has to be this process that rotates, and it
# has to not be a file launchd holds open -- a rename underneath launchd's
# descriptor leaves it appending to the rotated-away file forever. So the
# plist points stdout and stderr at a separate crash log, which stays small
# because almost nothing is written to it.
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
LOG_PATH = os.path.join(LOG_DIR, 'spotify-server.log')
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5          # so at most ~30MB total, months of history

os.makedirs(LOG_DIR, exist_ok=True)
_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)-7s %(name)s: %(message)s',
    datefmt='%d/%b/%Y:%H:%M:%S',  # matches cherrypy's access-log timestamps
))

log = logging.getLogger('dj')
log.setLevel(logging.INFO)
log.propagate = False
log.addHandler(_handler)


def _route_cherrypy_logs_to_file():
    """Send CherryPy's access and error logs to the same rotating file.

    Called after cherrypy.config.update, which is what installs the default
    screen handlers -- doing it earlier just gets them added back.
    """
    cherrypy.log.access_log.handlers = [_handler]
    cherrypy.log.error_log.handlers = [_handler]

# Load config
config_path = os.path.join(os.path.dirname(__file__), 'config.json')
with open(config_path) as f:
    config = json.load(f)


# Tunable values, all overridable per-install from config.json. Collected here
# so they can be found and changed without reading the whole file.
DEFAULTS = {
    "server_port": 5006,
    "claude_model": "claude-sonnet-5",
    "claude_max_tokens": 512,
    "search_limit": 5,
    # node-sonos-http-api serves the entire queue from /queue. On a long queue
    # that is several megabytes and takes over 6 seconds -- longer than
    # sonos_timeout -- so getqueue used to time out every time and report an
    # empty queue. /queue/{limit} answers in milliseconds.
    "queue_display_limit": 50,
    "sonos_timeout": 5,
    # Loading a playlist or album is not like pause/volume: Sonos expands the
    # whole container before it answers, so the wait scales with the track
    # count. A 51-track playlist returns in well under a second, but the
    # 8,864-track "DW Archive" takes ~46s. At sonos_timeout it 502'd every
    # time -- and the add still landed a minute later, so each retry silently
    # queued another 8,864 tracks (the queue was found at 8,950).
    # Only spotify/now, spotify/queue and spotify/next get this longer budget.
    "sonos_content_timeout": 90,
    "cookie_max_age": 86400 * 7,
    # Login sessions are held in memory; the cap stops a long-running server
    # accumulating tokens indefinitely.
    "max_sessions": 100,
    # Search results are per-session scratch state, not data worth keeping.
    "search_result_ttl": 3600,
    "max_search_sessions": 100,
    # Scheduler tick. Must be under 60s or a schedule whose minute falls
    # between two ticks is skipped entirely.
    "schedule_tick_seconds": 20,
    # A step used to be stamped fired before the Sonos call ran, so a single
    # failure burned it for the whole day -- a wake-up that silently did
    # nothing. Now the attempt is stamped and the success is committed
    # separately, and a step that failed is retried while it is still inside
    # this window past its fire time. The window has to exceed the trigger
    # minute or a retry could never be claimed: matching is on HH:MM, and by
    # the time a failed call returns that minute is often gone.
    "schedule_max_attempts": 3,
    "schedule_retry_window_seconds": 300,
    # launchd's KeepAlive only sees the process. It cannot see the case that
    # actually happened: node-sonos-http-api alive and answering, but with no
    # system discovered, so every playback call fails. The watchdog watches for
    # that and says so once per outage rather than once per tick.
    # A content load that timed out may still be running on the speaker: the
    # 8,864-track add came back at ~46s having worked, long after the caller
    # gave up. Repeating it queued another 8,864 tracks. Within this window a
    # repeat of the same container collapses into the first one instead.
    "content_dedup_seconds": 180,
    # /nowplaying was ~90% of all traffic: every open tab polled it every 10s
    # whether anything had changed or not. node-sonos-http-api can POST the
    # moment something actually changes, so the browser is told instead of
    # asking. Note the cost: CherryPy is thread-per-connection, so every open
    # stream holds a worker for its lifetime. The default pool of 10 would be
    # exhausted by a handful of tabs, hence both numbers below.
    "server_thread_pool": 30,
    "max_stream_clients": 12,
    # Cloudflare will close an idle tunnelled connection; a comment line keeps
    # it open and lets the browser notice a dead stream and reconnect.
    "stream_heartbeat_seconds": 25,
    "sonos_readiness_timeout": 3,
    "watchdog_tick_seconds": 60,
    "watchdog_failures_before_alert": 2,
    "watchdog_notify": True,
    "max_schedules": 50,
    "max_steps_per_schedule": 20,
    "max_stations": 50,
    # /chat bills the Anthropic key on every call, so these are a spend limit
    # as much as a load limit. 500 characters is a long spoken request; 20 a
    # minute is far more than a person types and far less than a loop.
    "max_chat_message_chars": 500,
    "chat_calls_per_minute": 20,
    "max_chat_sessions": 100,
    # Largest legitimate body is a routine with the maximum number of steps,
    # which is a few kilobytes. CherryPy defaults to 100MB, and a 38MB body
    # was accepted and parsed.
    "max_request_body_bytes": 262144,
    # 12h: enough for a wind-down that starts in the evening and ends after
    # midnight, without letting an offset drift into ambiguity.
    "max_step_offset_minutes": 720,
}


def _setting(name):
    """Value from config.json if set, otherwise the built-in default."""
    value = config.get(name)
    return DEFAULTS[name] if value is None else value


def _validate_config():
    """Refuse to start on a config that cannot possibly work.

    Without this the process starts happily and every Spotify call fails at
    request time with an authentication error, which points at the token
    rather than at the missing credential that actually caused it.
    """
    problems = []
    for key in ('client_id', 'client_secret'):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"  {key}: missing or empty (required)")

    for key in ('anthropic_api_key', 'sonos_room', 'ui_password', 'cli_token'):
        value = config.get(key)
        if value is not None and not isinstance(value, str):
            problems.append(f"  {key}: must be a string, got {type(value).__name__}")

    # These were optional, and an empty ui_password only logged a warning at
    # startup. That is the wrong default for something published to the
    # internet through a Cloudflare tunnel: forgetting a key and deciding to
    # run without authentication produced exactly the same result, and only
    # one of them is a decision. Running open is still allowed -- it just has
    # to be written down rather than arrived at by omission.
    if not config.get('allow_open_access'):
        for key in ('ui_password', 'cli_token'):
            value = config.get(key)
            if not isinstance(value, str) or not value.strip():
                problems.append(
                    f"  {key}: missing or empty -- set it, or set"
                    ' "allow_open_access": true to run with no authentication'
                )

    if problems:
        sys.stderr.write(
            f"config.json at {config_path} is not usable:\n"
            + "\n".join(problems)
            + "\n\nSee config.example.json for the expected shape.\n"
        )
        raise SystemExit(1)


_validate_config()

# Spotify setup
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=config['client_id'],
    client_secret=config['client_secret'],
    redirect_uri="http://127.0.0.1:8888/callback",
    scope="user-library-read user-library-modify playlist-read-private playlist-modify-public playlist-modify-private user-read-recently-played user-top-read",
    cache_path=os.path.join(os.path.dirname(__file__), '.cache')
))

# Sonos setup
#
# The room goes straight into a URL path, so it has to be percent-encoded --
# "Living Room" with a real space builds a malformed request. Encoding it
# blindly is not safe either: existing configs already store the encoded form
# ("Dining%20Room"), and quoting that again yields "Dining%2520Room" and a 404
# on every call. Unquoting first makes both spellings land on the same URL.
SONOS_ROOM = urllib.parse.quote(
    urllib.parse.unquote(config.get('sonos_room', 'Dining Room')), safe=''
)
# /zones and other API-wide endpoints are not room-scoped, so keep the base
# separate from the room-prefixed URL the playback helpers use.
SONOS_BASE_URL = "http://localhost:5005"
SONOS_URL = f"{SONOS_BASE_URL}/{SONOS_ROOM}"

# Monotonic so uptime is unaffected by the clock being adjusted under us.
SERVER_START = time.monotonic()

# Web UI markup, kept out of this file so the HTML/CSS/JS can be edited as
# HTML rather than as a 400-line Python string literal.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
UI_INDEX_PATH = os.path.join(STATIC_DIR, 'index.html')

QUEUE_DISPLAY_LIMIT = _setting('queue_display_limit')
SONOS_TIMEOUT = _setting('sonos_timeout')
SONOS_CONTENT_TIMEOUT = _setting('sonos_content_timeout')

# Claude setup
ANTHROPIC_API_KEY = config.get('anthropic_api_key', '')

# The SDK handles retries (429 and 5xx) and connection errors with backoff.
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

CLAUDE_MODEL = _setting('claude_model')
CLAUDE_MAX_TOKENS = _setting('claude_max_tokens')

# Every DJ command Claude may return. Enforced by the API rather than trusted:
# with output_config.format the response is guaranteed to match this schema, so
# there is no need to defend against Claude wrapping JSON in prose or markdown.
DJ_COMMAND_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "search", "play", "queue", "next", "pause", "resume", "skip",
                "previous", "volume", "nowplaying", "showqueue", "clear",
                "help", "chat",
            ],
        },
        "message": {"type": "string", "description": "Friendly reply for the user"},
        "query": {"type": "string", "description": "Search terms, for action=search"},
        "num": {"type": "integer", "description": "Result number, for play/queue/next"},
        "level": {"type": "integer", "description": "Volume 0-100, for action=volume"},
        "change": {"type": "string", "description": "Relative volume such as +10"},
    },
    "required": ["action", "message"],
    "additionalProperties": False,
}

# Password for web UI (optional -- if unset, the server runs unauthenticated)
UI_PASSWORD = config.get('ui_password', '')

# Shared secret for the `dj` CLI and other local tooling. The CLI sends it as
# an X-DJ-Token header. A separate credential from UI_PASSWORD so that handing
# someone the web password does not also hand them a non-expiring API key.
CLI_TOKEN = config.get('cli_token', '')

# Session tokens issued by /login. In-memory, so a restart logs everyone out.
_sessions = set()

MAX_SESSIONS = _setting('max_sessions')
COOKIE_MAX_AGE = _setting('cookie_max_age')
SEARCH_RESULT_TTL = _setting('search_result_ttl')
MAX_SEARCH_SESSIONS = _setting('max_search_sessions')
SEARCH_LIMIT = _setting('search_limit')

# Paths reachable without credentials. Everything else is denied by default,
# so a new endpoint is protected unless it is deliberately added here.
PUBLIC_PATHS = {'', '/index', '/ui', '/login'}

# Last search results per session, as {session_id: (stored_at, results)}.
#
# Every distinct session_id the web UI sends creates an entry, and nothing used
# to remove them -- a public URL plus a few months of guests is an unbounded
# dict. Entries expire after SEARCH_RESULT_TTL and the total is capped; the
# data is scratch state for "play number 3", not anything worth persisting.
search_results = {}

# CherryPy serves requests on a thread pool, so two guests searching at once
# run this concurrently, and the expiry sweep is not safe under that. It
# iterates the dict to find stale entries -- one thread inserting mid-iteration
# raises "dictionary changed size during iteration" -- and then picks the
# oldest key and deletes it, a check-then-act where both threads can choose
# the same key and the second raises KeyError. Reproduced with a short thread
# switch interval; see test_hardening_sweep.py.
_results_lock = threading.Lock()


def _expire_search_results_locked():
    """Drop stale sessions, then oldest-first if still over the cap."""
    now = time.monotonic()
    for session_id in [
        s for s, (stored_at, _) in search_results.items()
        if now - stored_at > SEARCH_RESULT_TTL
    ]:
        del search_results[session_id]

    while len(search_results) > MAX_SEARCH_SESSIONS:
        oldest = min(search_results, key=lambda s: search_results[s][0])
        del search_results[oldest]


# ==================== SCHEDULES ====================
#
# A schedule is a *routine*: a trigger time, the days it applies to, and an
# ordered list of steps, each with a minute offset from the trigger. That is
# what makes a gradual wake-up expressible -- volume 12 and play at +0, then
# volume 22 at +10, and so on.
#
# Steps are matched against the clock on every tick rather than run by sleeping
# a thread. A restart between the trigger and a +60m step therefore still runs
# that step, and no work is lost when the process is replaced mid-routine.
#
# Time handling is local wall-clock, so the DST consequences are the usual
# ones: a trigger inside the skipped hour on the spring-forward day does not
# fire, and on fall-back the last_fired date stops the repeated hour from
# firing anything twice.

SCHEDULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'schedules.json')

SCHEDULE_TICK_SECONDS = _setting('schedule_tick_seconds')
SCHEDULE_MAX_ATTEMPTS = _setting('schedule_max_attempts')
SCHEDULE_RETRY_WINDOW_SECONDS = _setting('schedule_retry_window_seconds')
CONTENT_DEDUP_SECONDS = _setting('content_dedup_seconds')
MAX_STREAM_CLIENTS = _setting('max_stream_clients')
STREAM_HEARTBEAT_SECONDS = _setting('stream_heartbeat_seconds')

# One bounded queue per connected browser. Bounded on purpose: a client that
# has stopped reading -- a laptop that slept with the tab open -- must not grow
# a queue until the process dies. It drops events and resyncs on reconnect.
_stream_clients = []
_stream_lock = threading.Lock()
SONOS_READINESS_TIMEOUT = _setting('sonos_readiness_timeout')

# Named because the dedupe below has to tell an ambiguous failure from a
# certain one, and matching on a loose string in two places would rot.
SONOS_TIMEOUT_ERROR = "Sonos request timed out"

# (action, uri) -> {'finished': monotonic or None, 'result': dict or None}
_content_loads = {}
_content_lock = threading.Lock()
WATCHDOG_TICK_SECONDS = _setting('watchdog_tick_seconds')
WATCHDOG_FAILURES_BEFORE_ALERT = _setting('watchdog_failures_before_alert')
WATCHDOG_NOTIFY = _setting('watchdog_notify')

# (entry id, step index) for every step a tick is currently firing. Claiming is
# minute-based and a Sonos call can outlast several ticks, so without this the
# retry path would start a second copy of a step that is still running.
_steps_in_flight = set()
MAX_SCHEDULES = _setting('max_schedules')
MAX_STEPS = _setting('max_steps_per_schedule')
MAX_OFFSET_MINUTES = _setting('max_step_offset_minutes')

# Actions a step may perform, and the field each one requires.
SCHEDULE_ACTIONS = {
    'play': ('uri',),
    'pause': (),
    'resume': (),
    'skip': (),
    'previous': (),
    'volume': ('volume',),
    'clearqueue': (),
}

TIME_RE = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')

# Guarded because the Monitor thread reads while request handlers write.
_schedules_lock = threading.Lock()
_schedules = []


def _migrate_schedule(entry):
    """Bring a pre-routine entry forward.

    The first version stored one action per schedule, flat. Rather than
    require a hand edit of schedules.json, fold that shape into a single
    zero-offset step.
    """
    if 'steps' in entry:
        return entry

    step = {'offset': 0, 'action': entry.pop('action', 'pause')}
    for field in ('uri', 'volume'):
        if entry.get(field) is not None:
            step[field] = entry.pop(field)
    step['last_fired'] = entry.pop('last_fired', None)
    entry['steps'] = [step]
    return entry


def _load_schedules():
    """Read schedules.json. A missing or corrupt file is not fatal -- losing
    alarms is better than refusing to serve music."""
    try:
        with open(SCHEDULES_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (ValueError, OSError) as exc:
        log.error("Cannot read %s (%s) -- continuing with no schedules", SCHEDULES_PATH, exc)
        return []

    if not isinstance(data, list):
        log.error("%s is not a list -- continuing with no schedules", SCHEDULES_PATH)
        return []

    migrated = [_migrate_schedule(e) for e in data if isinstance(e, dict)]
    if any('steps' not in e for e in data if isinstance(e, dict)):
        log.info("Migrated %d schedule(s) to the routine format", len(migrated))
    return migrated


def _save_schedules_locked():
    """Persist via a temp file + rename, so a crash mid-write cannot leave a
    truncated file that reads back as zero schedules. Caller holds the lock."""
    tmp = SCHEDULES_PATH + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(_schedules, f, indent=2)
        os.replace(tmp, SCHEDULES_PATH)
    except OSError as exc:
        log.error("Could not write %s: %s", SCHEDULES_PATH, exc)


def _validate_step(action, offset=0, uri=None, volume=None):
    """Build one step of a routine, or abort with 400."""
    if action not in SCHEDULE_ACTIONS:
        _bad_request(f"action must be one of: {', '.join(sorted(SCHEDULE_ACTIONS))}")

    step = {
        'offset': _validate_int(offset if offset not in (None, '') else 0,
                                "offset", 0, MAX_OFFSET_MINUTES),
        'action': action,
        'last_fired': None,
    }

    required = SCHEDULE_ACTIONS[action]
    if 'uri' in required:
        step['uri'] = _validate_uri(uri)
    if 'volume' in required or volume not in (None, ''):
        step['volume'] = _validate_int(volume, "volume", 0, 100)
    return step


def _validate_schedule(time_str, days, label):
    """Build a routine shell (no steps yet), or abort with 400."""
    if not TIME_RE.match(time_str or ''):
        _bad_request("time must be HH:MM in 24-hour form, e.g. 07:00")

    # days: "0,1,2" with 0=Monday. Empty means every day.
    parsed = []
    for part in (days or '').split(','):
        part = part.strip()
        if part:
            parsed.append(_validate_int(part, "days", 0, 6))

    return {
        "id": "sch_" + secrets.token_hex(6),
        "time": time_str,
        "days": sorted(set(parsed)),
        "label": (label or '').strip()[:80],
        "enabled": True,
        "steps": [],
    }


def _step_fire_time(trigger, offset):
    """Return (HH:MM, day_shift) for a step at `offset` minutes past `trigger`.

    day_shift is 1 when the offset carries the step past midnight, which the
    caller needs in order to check the *trigger* day rather than the day the
    step happens to land on.
    """
    hour, minute = int(trigger[:2]), int(trigger[3:])
    day_shift, minutes = divmod(hour * 60 + minute + offset, 1440)
    return f"{minutes // 60:02d}:{minutes % 60:02d}", day_shift


def _next_run(entry, now=None):
    """When this routine next triggers, as an ISO datetime, or None.

    The UI used to show nothing at all about when a routine would fire, which
    is how a wake-up alarm sat on Sunday-only for weeks without anyone
    noticing. Computed here rather than in JavaScript because the weekday
    convention (0=Monday, empty=every day) belongs to _due_steps; a second
    implementation would be free to drift from the one that actually fires.
    """
    if not entry.get('enabled', True):
        return None
    trigger = entry.get('time', '')
    if not TIME_RE.match(trigger):
        return None

    now = now or datetime.datetime.now()
    days = entry.get('days') or list(range(7))
    hour, minute = int(trigger[:2]), int(trigger[3:])

    # 8 rather than 7: if today matches but the time has already passed, the
    # answer is next week's same weekday.
    for ahead in range(8):
        date = now.date() + datetime.timedelta(days=ahead)
        if date.weekday() not in days:
            continue
        when = datetime.datetime.combine(date, datetime.time(hour, minute))
        if when > now:
            return when.isoformat(timespec='minutes')
    return None


def _annotate_schedule(entry, now=None):
    """A copy of a routine carrying what the UI needs to render it.

    Steps gain the wall-clock time they land on, so the editor can show
    '07:15' instead of '+75m' without doing the midnight-wrap arithmetic
    itself.
    """
    result = dict(entry)
    result['next_run'] = _next_run(entry, now)
    trigger = entry.get('time', '')
    steps = []
    for step in entry.get('steps', []):
        step = dict(step)
        if TIME_RE.match(trigger):
            step['at'], shift = _step_fire_time(trigger, step.get('offset', 0))
            step['next_day'] = bool(shift)
        steps.append(step)
    result['steps'] = steps
    return result


def _retry_is_open(step, fire_at, day_shift, now_dt):
    """True if `step` failed earlier and is still inside its retry window.

    Claiming is minute-exact, so a step whose call failed slowly -- a content
    load can run to sonos_content_timeout -- would never see its own HH:MM
    again and could not be retried at all. This reopens the claim for a bounded
    period after the fire time instead.
    """
    attempted = step.get('last_attempt')
    if not attempted:
        return False
    if step.get('last_fired') == attempted:
        return False
    if step.get('attempts', 0) >= SCHEDULE_MAX_ATTEMPTS:
        return False

    try:
        trigger_date = datetime.date.fromisoformat(attempted)
        hour, minute = (int(part) for part in fire_at.split(':'))
    except ValueError:
        # A hand-edited schedules.json can carry a malformed stamp. Log it and
        # leave the step alone rather than retrying on a date we cannot read.
        log.warning("Schedule step has unreadable last_attempt %r", attempted)
        return False

    fired_on = trigger_date + datetime.timedelta(days=day_shift)
    fire_dt = datetime.datetime.combine(fired_on, datetime.time(hour, minute))
    elapsed = (now_dt - fire_dt).total_seconds()
    return 0 <= elapsed <= SCHEDULE_RETRY_WINDOW_SECONDS


def _due_steps(now=None):
    """Claim every step due right now, stamping the attempt under the lock.

    Stamping before running matters: a Sonos call takes seconds and the tick
    is shorter than a minute, so an unclaimed step would fire on every tick
    until the minute passed.

    What is stamped here is the *attempt*, not the success. `last_fired` is
    committed by _record_step_outcome only once the call comes back clean, so
    a step whose Sonos call failed can be claimed again inside its retry
    window rather than being burned for the day. `_steps_in_flight` is what
    stops that retry path from double-firing a step that is merely slow.
    """
    now = now or time.localtime()
    hhmm = f"{now.tm_hour:02d}:{now.tm_min:02d}"
    today = datetime.date(now.tm_year, now.tm_mon, now.tm_mday)
    now_dt = datetime.datetime(now.tm_year, now.tm_mon, now.tm_mday,
                               now.tm_hour, now.tm_min, now.tm_sec)

    claimed = []
    with _schedules_lock:
        for entry in _schedules:
            if not entry.get('enabled', True):
                continue
            trigger = entry.get('time', '')
            if not TIME_RE.match(trigger):
                continue

            for index, step in enumerate(entry.get('steps', [])):
                fire_at, day_shift = _step_fire_time(trigger, step.get('offset', 0))

                if fire_at == hhmm:
                    # A step that wrapped past midnight belongs to the previous
                    # day's run, so both the weekday filter and the fired-stamp
                    # key off the trigger date rather than today's.
                    trigger_date = today - datetime.timedelta(days=day_shift)
                    days = entry.get('days') or list(range(7))
                    if trigger_date.weekday() not in days:
                        continue
                    if step.get('last_fired') == trigger_date.isoformat():
                        continue
                    if (step.get('last_attempt') == trigger_date.isoformat()
                            and step.get('attempts', 0) >= SCHEDULE_MAX_ATTEMPTS):
                        continue
                elif _retry_is_open(step, fire_at, day_shift, now_dt):
                    trigger_date = datetime.date.fromisoformat(step['last_attempt'])
                else:
                    continue

                key = (entry.get('id'), index)
                if key in _steps_in_flight:
                    continue

                stamp = trigger_date.isoformat()
                step['attempts'] = (
                    step.get('attempts', 0) + 1 if step.get('last_attempt') == stamp else 1
                )
                step['last_attempt'] = stamp
                _steps_in_flight.add(key)
                claimed.append({
                    **step,
                    'label': entry.get('label') or entry.get('id'),
                    '_entry_id': entry.get('id'),
                    '_step_index': index,
                    '_trigger_date': stamp,
                })

        if claimed:
            _save_schedules_locked()
    return claimed


def _record_step_outcome(claimed, ok, error):
    """Commit the result of one fired step and release its in-flight claim.

    Success writes `last_fired`, which is what stops the step being claimed
    again for that trigger date. Failure leaves it unset so the retry window
    can pick it up, and records `last_error` so a routine that quietly did
    nothing is visible in /schedules instead of only in the log.
    """
    key = (claimed.get('_entry_id'), claimed.get('_step_index'))
    stamp = claimed.get('_trigger_date')

    with _schedules_lock:
        _steps_in_flight.discard(key)

        entry = next((e for e in _schedules if e.get('id') == key[0]), None)
        if entry is None:
            return
        steps = entry.get('steps', [])
        if not 0 <= key[1] < len(steps):
            # The routine was edited while this step was in flight.
            return
        step = steps[key[1]]

        if ok:
            step['last_fired'] = stamp
            step.pop('last_error', None)
            _record_metric('schedule_fires')
        else:
            _record_metric('schedule_failures')
            step['last_error'] = {
                'date': stamp,
                'message': error,
                'attempts': step.get('attempts', 0),
                'final': step.get('attempts', 0) >= SCHEDULE_MAX_ATTEMPTS,
            }
        _save_schedules_locked()


def _fire_schedule(dj, entry):
    """Run one step. Never raises -- one bad step must not stop the rest.

    Returns (ok, error) so the caller can commit or retry. Still never raises:
    the outcome is reported, not thrown.
    """
    action = entry.get('action')
    label = entry.get('label') or action
    try:
        if action == 'play':
            # Volume before play, so a wake-up cannot blast at whatever level
            # last night ended on.
            if entry.get('volume') is not None:
                dj._do_volume(level=entry['volume'])
            result = dj._do_play(uri=entry['uri'])
        elif action == 'volume':
            result = dj._do_volume(level=entry['volume'])
        elif action == 'pause':
            result = dj._do_pause()
        elif action == 'resume':
            result = dj._do_resume()
        elif action == 'skip':
            result = dj._do_skip()
        elif action == 'previous':
            result = dj._do_previous()
        elif action == 'clearqueue':
            result = dj._do_clearqueue()
        else:
            # Not retryable -- the routine itself is wrong, so report it as a
            # final failure rather than letting the window re-run it.
            log.error("Schedule %r has unknown action %r", label, action)
            return False, f"unknown action {action!r}"
    except Exception as exc:
        log.error("Schedule %r raised %s: %s", label, type(exc).__name__, exc)
        return False, f"{type(exc).__name__}: {exc}"

    if isinstance(result, dict) and 'error' in result:
        log.error("Schedule %r failed: %s", label, result['error'])
        return False, result['error']

    log.info("Schedule %r ran %s", label, action)
    return True, None


def _broadcast(payload):
    """Hand one event to every connected browser.

    Never blocks and never raises. A slow or dead client gets its event
    dropped rather than stalling the Sonos webhook that is delivering it --
    the stream is a convenience, and the poll fallback still covers anyone
    who misses one.
    """
    message = f"data: {json.dumps(payload)}\n\n"
    with _stream_lock:
        clients = list(_stream_clients)
    delivered = 0
    for client in clients:
        try:
            client.put_nowait(message)
            delivered += 1
        except queue.Full:
            log.debug("Stream client is not keeping up; dropping an event")
    return delivered


def _sonos_readiness():
    """Is Sonos actually usable right now? Returns (ok, detail).

    One implementation with two callers -- /health reports it, the watchdog
    alerts on it. Two copies of this rule would be free to drift, which is the
    same trap _next_run and _due_steps sit in.

    A 200 is deliberately not enough. node-sonos-http-api answers before SSDP
    discovery has found anything, and an empty zone list means every playback
    call is about to fail.
    """
    try:
        response = requests.get(f"{SONOS_BASE_URL}/zones",
                                timeout=SONOS_READINESS_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        # Class name only -- the full message can carry internal hostnames.
        return False, f"error: {exc.__class__.__name__}"

    if response.status_code != 200:
        return False, f"error: HTTP {response.status_code}"
    try:
        zones = response.json()
    except ValueError:
        return False, "error: unparseable zone list"
    if not zones:
        return False, "error: no zones discovered"
    return True, "ok"


_watchdog = {
    'ok': None,            # None until the first check, so boot is not an alert
    'consecutive_failures': 0,
    'detail': 'not yet checked',
    'changed_at': None,
    'outages': 0,
}
_watchdog_lock = threading.Lock()


# Counters, not a log. The log answers "what happened at 07:00"; these answer
# "is this getting worse", which no amount of grepping a rotating file does
# well. In-process and therefore reset by a restart -- uptime_seconds is
# reported alongside so a reading is never mistaken for all-time history.
_metrics = {
    'sonos_calls': 0,
    'sonos_failures': 0,
    'sonos_seconds_total': 0.0,
    'sonos_seconds_max': 0.0,
    'content_loads': 0,
    'content_seconds_max': 0.0,
    'events_received': 0,
    'stream_clients_peak': 0,
    'schedule_fires': 0,
    'schedule_failures': 0,
    'chat_calls': 0,
}
_metrics_lock = threading.Lock()


def _is_content_endpoint(endpoint):
    """Content loads expand a whole container and are the slow class; keeping
    them out of the transport average is the difference between a useful
    number and one dominated by a single 46-second playlist."""
    return endpoint.startswith('spotify/')


def _record_sonos_call(endpoint, seconds, ok):
    with _metrics_lock:
        _metrics['sonos_calls'] += 1
        if not ok:
            _metrics['sonos_failures'] += 1
        if _is_content_endpoint(endpoint):
            _metrics['content_loads'] += 1
            _metrics['content_seconds_max'] = max(_metrics['content_seconds_max'], seconds)
        else:
            _metrics['sonos_seconds_total'] += seconds
            _metrics['sonos_seconds_max'] = max(_metrics['sonos_seconds_max'], seconds)


def _record_metric(name, amount=1):
    with _metrics_lock:
        _metrics[name] += amount


def _notify(title, message):
    """Best-effort desktop notification. Never raises, never blocks a tick.

    A log line is not an alert -- nobody was reading the log the morning Sonos
    went dark for hours. This is the part that actually reaches someone.
    """
    if not WATCHDOG_NOTIFY:
        return
    try:
        subprocess.run(
            ['osascript', '-e',
             f'display notification {json.dumps(message)} with title {json.dumps(title)}'],
            capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not post notification: %s", exc.__class__.__name__)


def check_sonos_readiness():
    """Watchdog tick. Alerts on the transition, not on every failing check.

    Alerting per tick would mean 60 notifications an hour for one outage, which
    trains you to ignore them.
    """
    try:
        ok, detail = _sonos_readiness()
        with _watchdog_lock:
            was = _watchdog['ok']
            _watchdog['detail'] = detail
            _watchdog['consecutive_failures'] = 0 if ok else _watchdog['consecutive_failures'] + 1

            # One blip between ticks is not an outage; Sonos is on wifi.
            newly_down = (not ok
                          and was is not False
                          and _watchdog['consecutive_failures'] >= WATCHDOG_FAILURES_BEFORE_ALERT)
            recovered = ok and was is False

            if newly_down:
                _watchdog['ok'] = False
                _watchdog['outages'] += 1
                _watchdog['changed_at'] = time.time()
            elif recovered:
                _watchdog['ok'] = True
                _watchdog['changed_at'] = time.time()
            elif was is None and ok:
                _watchdog['ok'] = True
                _watchdog['changed_at'] = time.time()

        if newly_down:
            log.error("Sonos unreachable: %s", detail)
            _notify("DJ server", f"Sonos is unreachable -- {detail}")
        elif recovered:
            log.info("Sonos recovered")
            _notify("DJ server", "Sonos is back")
    except Exception as exc:
        # Same reasoning as the scheduler tick: a raising Monitor thread dies
        # silently, and a dead watchdog is worse than none because it looks fine.
        log.error("Watchdog tick failed: %s: %s", type(exc).__name__, exc)


def run_due_schedules(dj):
    """Scheduler tick. Wrapped so an unexpected error cannot kill the Monitor
    thread and silently stop every future routine."""
    try:
        for step in _due_steps():
            # finally, not just the happy path: an unreleased in-flight claim
            # blocks that step forever, which is the silent failure this whole
            # change exists to remove.
            ok, error = False, "tick aborted before the step reported"
            try:
                ok, error = _fire_schedule(dj, step)
            finally:
                _record_step_outcome(step, ok, error)
    except Exception as exc:
        log.error("Scheduler tick failed: %s: %s", type(exc).__name__, exc)


_schedules = _load_schedules()



# ==================== STATIONS ====================
#
# Spotify exposes Song Radio as an algorithmic playlist (37i9dQZF1E8...).
# Those URIs 404 on the Web API but Sonos resolves them, so they play fine --
# what is not possible is *deriving* one for a track, because the endpoints
# that did that (recommendations, related-artists) were withdrawn from
# third-party apps in Nov 2024.
#
# So the user copies a radio URI out of Spotify once, names it here, and it
# becomes reusable: playable in a tap and selectable as a schedule step.

STATIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stations.json')
MAX_STATIONS = _setting('max_stations')
MAX_CHAT_MESSAGE_CHARS = _setting('max_chat_message_chars')
CHAT_CALLS_PER_MINUTE = _setting('chat_calls_per_minute')
MAX_CHAT_SESSIONS = _setting('max_chat_sessions')
MAX_REQUEST_BODY_BYTES = _setting('max_request_body_bytes')

# Per-session call times for /chat, as {session_id: [monotonic, ...]}.
_chat_calls = {}
_chat_lock = threading.Lock()


def _check_chat_rate(session_id):
    """Allow CHAT_CALLS_PER_MINUTE per session per minute, or abort with 429.

    /chat is the only endpoint that costs money -- it bills the Anthropic key
    on every call -- so this is a spend limit first and a load limit second.
    A sliding window rather than a fixed one, so a caller cannot get a double
    allowance by straddling a minute boundary.
    """
    now = time.monotonic()
    with _chat_lock:
        recent = [t for t in _chat_calls.get(session_id, []) if now - t < 60]
        if len(recent) >= CHAT_CALLS_PER_MINUTE:
            _chat_calls[session_id] = recent
            wait = max(1, int(60 - (now - recent[0])) + 1)
            _too_many_requests(
                f"Too many requests -- try again in {wait}s", wait)

        recent.append(now)
        _chat_calls[session_id] = recent

        # Bounded like search_results: every distinct session_id the UI sends
        # creates an entry, and a public URL means an unbounded dict.
        if len(_chat_calls) > MAX_CHAT_SESSIONS:
            for stale in [s for s, times in _chat_calls.items() if not times
                          or now - times[-1] > 60]:
                del _chat_calls[stale]
            while len(_chat_calls) > MAX_CHAT_SESSIONS:
                del _chat_calls[min(_chat_calls, key=lambda s: _chat_calls[s][-1])]

_stations_lock = threading.Lock()
_stations = []


def _load_stations():
    try:
        with open(STATIONS_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (ValueError, OSError) as exc:
        log.error("Cannot read %s (%s) -- continuing with no stations", STATIONS_PATH, exc)
        return []
    return data if isinstance(data, list) else []


def _save_stations_locked():
    """Temp file plus rename, as for schedules."""
    tmp = STATIONS_PATH + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(_stations, f, indent=2)
        os.replace(tmp, STATIONS_PATH)
    except OSError as exc:
        log.error("Could not write %s: %s", STATIONS_PATH, exc)


_stations = _load_stations()


# ==================== SONOS QUEUE EDITING ====================
#
# Reordering and removing queue tracks go through two custom actions added to
# node-sonos-http-api (sonos-actions/queueedit.js in this repo; see the README
# for the one-line install).
#
# They live there rather than here because macOS grants Local Network access
# per process: the launchd-run Python server cannot open a connection to the
# speaker at all -- UPnP calls fail with "no route to host" -- while
# node-sonos-http-api, which talks to it constantly, can. Calling UPnP
# directly from this process works when run from a terminal and fails once
# deployed, which is a difference worth stating rather than rediscovering.
#
# Sonos queue indices are 1-based and shift as the queue is edited or plays
# on, so every mutation is guarded: the caller states which track it believes
# is at the index, and the edit is refused if the queue moved underneath it.


def _sonos_get_queue(limit, offset=0):
    """Read a window of the queue. Raises on transport failure."""
    url = f"{SONOS_URL}/queue/{int(limit)}/{int(offset)}"
    response = requests.get(url, timeout=SONOS_TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"Sonos returned HTTP {response.status_code} for the queue")
    data = response.json()
    return data if isinstance(data, list) else []


def _queue_track_at(index):
    """Read the single queue entry at a 1-based index, or None."""
    entries = _sonos_get_queue(limit=1, offset=index - 1)
    return entries[0] if entries else None


def _is_container_uri(uri):
    """A playlist or album -- the kind Sonos expands track by track, and the
    only kind slow enough to be worth deduplicating."""
    return not str(uri).startswith('spotify:track:')


def _guard_queue_index(index, expected_title):
    """Refuse the edit if the queue moved under the caller.

    A drag that began ten seconds ago may now point at a different track,
    because tracks finish and other clients add and remove. Rejecting with a
    409 and letting the client refresh beats silently reordering something
    the user never touched.
    """
    track = _queue_track_at(index)
    if track is None:
        _bad_request(f"there is no track at position {index}")
    actual = (track.get('title') or '').strip()
    if expected_title is not None and actual != (expected_title or '').strip():
        raise cherrypy.HTTPError(
            409, f"position {index} now holds {actual!r} -- the queue moved, refresh and retry")
    return track


def _is_authenticated():
    """True if the current request carries a valid session cookie or CLI token.

    Note: we deliberately do NOT trust the source IP. cloudflared connects to
    localhost, so tunnelled internet traffic arrives from 127.0.0.1 exactly
    like the local CLI does -- a loopback exemption would whitelist everyone.
    """
    if not UI_PASSWORD:
        return True

    token = cherrypy.request.headers.get('X-DJ-Token')
    if token and CLI_TOKEN and secrets.compare_digest(token, CLI_TOKEN):
        return True

    cookie = cherrypy.request.cookie.get('dj_auth')
    if cookie and cookie.value in _sessions:
        return True

    return False


def _check_auth():
    """before_handler hook: reject unauthenticated requests with 401 JSON."""
    if cherrypy.request.path_info.rstrip('/') in PUBLIC_PATHS or _is_authenticated():
        return

    cherrypy.response.status = 401
    cherrypy.response.headers['Content-Type'] = 'application/json'
    cherrypy.response.body = json.dumps({"error": "Authentication required"}).encode()
    # Suppress the page handler so the body above is what gets sent.
    cherrypy.request.handler = None


cherrypy.tools.djauth = cherrypy.Tool('before_handler', _check_auth, priority=10)


# ==================== INPUT VALIDATION ====================

# A Spotify URI is three colon-separated parts, e.g. spotify:track:4uLU6hMCjM.
SPOTIFY_URI_RE = re.compile(r'^spotify:[a-z]+:[A-Za-z0-9]+$')

# Spotify IDs are base62. Bounded so a malformed URI cannot push arbitrary
# text into a Spotify API path.
SPOTIFY_ID_RE = re.compile(r'^[A-Za-z0-9]{1,64}$')


def _json_error_page(status, message, traceback, version):
    """Render CherryPy's error pages as JSON.

    Without this a rejected request returns an HTML page, which every
    caller here (jq in the CLI, fetch() in the UI) fails to parse.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    return json.dumps({"error": message or status})


def _bad_request(message):
    """Abort the request with a 400 that _json_error_page renders as JSON."""
    raise cherrypy.HTTPError(400, message)


def _too_many_requests(message, retry_after):
    """Abort with a 429, telling the caller how long to wait.

    Retry-After is set as well as being in the message: the UI shows the
    message, but anything scripted should be able to read the header.
    """
    cherrypy.response.headers['Retry-After'] = str(retry_after)
    raise cherrypy.HTTPError(429, message)


def _json_body():
    """Read and parse the request body as a JSON object, or abort with 400.

    Parsed here rather than by cherrypy.tools.json_in because that tool
    catches ValueError only. json.loads recurses per nesting level, and a
    deeply nested body raises RecursionError -- which is a RuntimeError, so it
    escaped as a 500. Reading the body directly also keeps the size check
    next to the parse.
    """
    raw = cherrypy.request.body.read()
    if len(raw) > MAX_REQUEST_BODY_BYTES:
        _bad_request(f"body must be at most {MAX_REQUEST_BODY_BYTES} bytes")
    try:
        parsed = json.loads(raw)
    except RecursionError:
        _bad_request("body is nested too deeply")
    except (ValueError, UnicodeDecodeError):
        _bad_request("body must be valid JSON")
    if not isinstance(parsed, dict):
        _bad_request("expected a JSON object")
    return parsed


def _validate_text(value, field, max_length):
    """Return a stripped string, or abort with 400.

    Rejects rather than truncates: a silently shortened playlist name is a
    playlist the caller did not ask for. Control characters are refused
    outright -- they are never meant, and a NUL in a search query came back
    from Spotify as a 502, blaming the upstream for the caller's input.
    """
    text = (value or '').strip()
    if not text:
        _bad_request(f"{field} is required")
    if len(text) > max_length:
        _bad_request(f"{field} must be at most {max_length} characters")
    if any(ch < ' ' or ch == '\x7f' for ch in text):
        _bad_request(f"{field} must not contain control characters")
    return text


def _validate_uri(uri):
    """Reject anything that is not a literal Spotify URI.

    This value is interpolated straight into the Sonos API path, so an
    unvalidated uri such as '../../Bedroom/pause' would let a caller reach
    arbitrary rooms and endpoints on the Sonos API rather than just play a
    track. The regex also excludes the slashes and dots needed to escape.
    """
    if not SPOTIFY_URI_RE.match(uri or ''):
        _bad_request("uri must look like spotify:track:<id>")
    return uri


def _validate_int(value, name, minimum, maximum):
    """Coerce a query parameter to an int within an inclusive range."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        _bad_request(f"{name} must be a whole number, got {value!r}")
    if number < minimum or number > maximum:
        _bad_request(f"{name} must be between {minimum} and {maximum}, got {number}")
    return number


def _truthy(value):
    """Read an optional query-string flag.

    Absent means false. Anything a caller would plausibly type for yes counts
    -- including the bare '?force' that CherryPy hands over as an empty
    string, which would otherwise read as no.
    """
    if value is None:
        return False
    return str(value).strip().lower() in ('', '1', 'true', 'yes', 'on')


def _validate_volume_change(change):
    """Normalise a relative volume change back to the +N / -N Sonos wants."""
    number = _validate_int(change, "change", -100, 100)
    return f"{number:+d}"


# ==================== UPSTREAM ERROR HANDLING ====================


def _handles_spotify_errors(fn):
    """Turn spotipy failures into JSON errors instead of an HTML 500.

    Every sp.*() call can fail for reasons that have nothing to do with the
    request: the cached token expired or was revoked, the account hit a rate
    limit, a playlist was deleted, or Spotify is simply down. Untrapped, any
    of those propagates out of the handler and CherryPy renders a 500 HTML
    page that neither the CLI's jq nor the UI's fetch() can parse.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SpotifyOauthError as exc:
            log.error("Spotify authorisation failed in %s: %s", fn.__name__, exc)
            raise cherrypy.HTTPError(
                502, f"Spotify authorisation failed ({exc}). Re-run auth.py to refresh .cache."
            )
        except SpotifyException as exc:
            status = getattr(exc, 'http_status', None)
            detail = getattr(exc, 'msg', None) or str(exc)
            log.warning("Spotify error in %s: HTTP %s %s", fn.__name__, status, detail)
            if status == 429:
                # Surfaced as-is so clients can back off rather than retry.
                raise cherrypy.HTTPError(429, "Spotify rate limit reached -- try again shortly")
            if status == 404:
                raise cherrypy.HTTPError(404, f"Spotify found nothing for that request: {detail}")
            if status in (401, 403):
                raise cherrypy.HTTPError(
                    502, "Spotify rejected the token -- re-run auth.py to re-authorise"
                )
            raise cherrypy.HTTPError(502, f"Spotify error: {detail}")
        except SpotifyBaseException as exc:
            raise cherrypy.HTTPError(502, f"Spotify error: {exc}")
        except requests.exceptions.RequestException as exc:
            raise cherrypy.HTTPError(502, f"Could not reach Spotify: {exc}")

    # CherryPy decides whether a query parameter is expected by reading the
    # handler's signature with inspect.getfullargspec, which -- unlike
    # inspect.signature -- does not follow the __wrapped__ chain that
    # functools.wraps sets. So a decorated handler looked like (*args,
    # **kwargs): every parameter was accepted, and an unknown one became a
    # TypeError inside the call, i.e. a 500 with a stack trace instead of the
    # 404 an undecorated endpoint gives. getfullargspec does honour an
    # explicit __signature__.
    wrapper.__signature__ = inspect.signature(fn)
    return wrapper

def get_results(session_id='global'):
    """Get search results for a session, or [] if absent or expired."""
    with _results_lock:
        entry = search_results.get(session_id)
        if entry is None:
            return []

        stored_at, results = entry
        if time.monotonic() - stored_at > SEARCH_RESULT_TTL:
            del search_results[session_id]
            return []
        return results


def set_results(results, session_id='global'):
    """Store search results for a session, then sweep.

    The sweep runs *after* the insert: sweeping first trims to the cap and
    then adds one more, so the dict settles at MAX_SEARCH_SESSIONS + 1.
    """
    with _results_lock:
        search_results[session_id] = (time.monotonic(), results)
        _expire_search_results_locked()


def call_claude(message, session_id='global'):
    """Send message to Claude and get DJ command"""
    if not ANTHROPIC_API_KEY:
        return None
    
    current_results = get_results(session_id)
    results_context = ""
    if current_results:
        results_context = "\n\nCurrent search results:\n"
        for r in current_results:
            if 'artist' in r:
                results_context += f"{r['num']}. {r['name']} by {r['artist']}\n"
            else:
                results_context += f"{r['num']}. {r['name']}\n"
    
    system_prompt = f"""You are a friendly DJ assistant. Help users find and queue music.

Available commands you can return:
- {{"action": "search", "query": "search terms"}} - Search for music
- {{"action": "play", "num": 1}} - Play a numbered result immediately
- {{"action": "queue", "num": 1}} - Add numbered result to end of queue
- {{"action": "next", "num": 1}} - Add numbered result to play next
- {{"action": "pause"}} - Pause playback
- {{"action": "resume"}} - Resume playback
- {{"action": "skip"}} - Skip to next track
- {{"action": "previous"}} - Go to previous track
- {{"action": "volume", "level": 50}} - Set volume (0-100)
- {{"action": "volume", "change": "+10"}} - Adjust volume
- {{"action": "nowplaying"}} - Show what's playing
- {{"action": "showqueue"}} - Show the queue
- {{"action": "clear"}} - Clear the queue
- {{"action": "help"}} - Show help
- {{"action": "chat", "message": "response"}} - Just chat, no action needed
{results_context}

Respond with JSON only. Include a friendly "message" field with your response to the user.
If user says a number like "2" or "queue 3", they want that result from the current search.
If user says "add" or "queue", add to end. If they say "next" or "play next", insert after current song.

Examples:
User: "play some beatles"
{{"action": "search", "query": "beatles", "message": "Let me find some Beatles for you! 🎸"}}

User: "3"
{{"action": "queue", "num": 3, "message": "Added to the queue! 🎵"}}

User: "play 2 next"
{{"action": "next", "num": 2, "message": "That'll play right after this song! ⏭️"}}

User: "thanks!"
{{"action": "chat", "message": "You're welcome! Enjoy the music! 🎉"}}
"""

    try:
        response = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=system_prompt,
            # Parsing a DJ command is a short classification, and the reply is
            # blocking someone standing at a speaker -- skip thinking entirely
            # and keep the token spend down.
            thinking={"type": "disabled"},
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": DJ_COMMAND_SCHEMA},
            },
            messages=[{"role": "user", "content": message}],
        )
    except anthropic.RateLimitError:
        # The SDK already retried with backoff; this is the give-up path.
        return {"action": "chat", "message": "Too many requests right now -- try again in a moment!"}
    except anthropic.APIStatusError as exc:
        log.error("Claude API error %s: %s", exc.status_code, exc.message)
        return {"action": "chat", "message": "Sorry, I had trouble understanding that. Try again!"}
    except anthropic.APIConnectionError as exc:
        log.error("Claude unreachable: %s", exc)
        return {"action": "chat", "message": "I can't reach my brain right now. Use the buttons instead!"}

    if response.stop_reason == "refusal":
        return {"action": "chat", "message": "I'd rather not answer that one!"}

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        # max_tokens truncation is the realistic cause of an empty response.
        log.warning("Claude returned no text (stop_reason=%s)", response.stop_reason)
        return {"action": "chat", "message": "Sorry, I had trouble understanding that. Try again!"}

    try:
        return json.loads(text)
    except ValueError:
        # output_config pins a json_schema, so this should not happen -- but it
        # is the one parse in this function that could still 500 the request,
        # and every other failure above degrades to a spoken reply instead.
        log.error("Claude returned unparseable JSON (stop_reason=%s)", response.stop_reason)
        return {"action": "chat", "message": "Sorry, I had trouble understanding that. Try again!"}


class DJServer:

    # Auth is on for every handler; PUBLIC_PATHS carves out the exceptions.
    # Error pages render as JSON so clients never have to parse HTML.
    _cp_config = {
        'tools.djauth.on': True,
        'error_page.400': _json_error_page,
        'error_page.401': _json_error_page,
        'error_page.404': _json_error_page,
        'error_page.409': _json_error_page,
        'error_page.429': _json_error_page,
        'error_page.500': _json_error_page,
        'error_page.502': _json_error_page,
    }

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def index(self):
        return {
            "status": "DJ Server running",
            "endpoints": {
                "ui": "/ui - Web interface",
                "chat": "/chat?message=<text> - Natural language commands",
                "search": "/search?q=<query>",
                "play": "/play?num=<num>",
                "queue": "/queue?num=<num>",
                "next": "/next?num=<num>",
                "pause": "/pause",
                "resume": "/resume",
                "skip": "/skip",
                "previous": "/previous",
                "volume": "/volume?level=<0-100>",
                "nowplaying": "/nowplaying",
                "getqueue": "/getqueue",
                "clearqueue": "/clearqueue"
            }
        }

    # ==================== WEB UI ====================

    @cherrypy.expose
    def ui(self):
        # /ui is public so we can serve the login form rather than a bare 401.
        if not _is_authenticated():
            return '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>DJ Assistant - Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0;
        }
        .login-box {
            background: rgba(255,255,255,0.1);
            padding: 40px;
            border-radius: 15px;
            text-align: center;
        }
        h1 { color: white; margin-bottom: 20px; }
        input[type="password"] {
            padding: 15px;
            font-size: 18px;
            border: none;
            border-radius: 10px;
            background: rgba(255,255,255,0.2);
            color: white;
            width: 200px;
            text-align: center;
        }
        input::placeholder { color: #888; }
        button {
            display: block;
            width: 100%;
            margin-top: 15px;
            padding: 15px;
            font-size: 16px;
            border: none;
            border-radius: 10px;
            background: #1db954;
            color: white;
            cursor: pointer;
            font-weight: bold;
        }
        button:hover { background: #1ed760; }
    </style>
</head>
<body>
    <div class="login-box">
        <h1>🎵 DJ Assistant</h1>
        <form method="POST" action="/login">
            <input type="password" name="password" placeholder="Password" autofocus>
            <button type="submit">Enter</button>
        </form>
    </div>
</body>
</html>'''

        # The UI markup lives in static/index.html; serve_file streams it and
        # handles caching headers. Deliberately not mounted as a staticdir:
        # this is one file, and a directory mount would be another surface to
        # get the auth config right on.
        return cherrypy.lib.static.serve_file(UI_INDEX_PATH, content_type='text/html')

    @cherrypy.expose
    # POST only. The handler accepts `password` from either a query string or a
    # form body, and CherryPy's access log records the full request line -- so a
    # GET login writes the password in cleartext to logs/spotify-server.log, and
    # onward to browser history, the Referer header and the Cloudflare tunnel's
    # own logs. Rejecting GET means the credential cannot be put in a URL at all.
    # The login form already posts, so this is invisible to the UI.
    @cherrypy.tools.allow(methods=['POST'])
    def login(self, password=None):
        # compare_digest avoids leaking the password length/prefix via timing.
        if password is not None and secrets.compare_digest(password, UI_PASSWORD):
            # Issue a random session token. The password itself never goes in
            # the cookie, so a stolen cookie can be revoked by restarting.
            if len(_sessions) >= MAX_SESSIONS:
                _sessions.clear()
            token = secrets.token_hex(32)
            _sessions.add(token)

            cherrypy.response.cookie['dj_auth'] = token
            cherrypy.response.cookie['dj_auth']['path'] = '/'
            cherrypy.response.cookie['dj_auth']['max-age'] = COOKIE_MAX_AGE
            cherrypy.response.cookie['dj_auth']['httponly'] = True
            log.info("Login succeeded (%d active sessions)", len(_sessions))
        else:
            # Never log the submitted value -- a near-miss typo of the real
            # password would end up in a file that is not treated as secret.
            log.warning("Login failed")
        raise cherrypy.HTTPRedirect('/ui')

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def health(self):
        """Report whether Sonos and Spotify are actually reachable.

        Deliberately NOT wrapped in _handles_spotify_errors: that decorator
        aborts with a 502 on any Spotify failure, which is precisely the
        condition this endpoint exists to report. It must return a body.

        Also deliberately not in PUBLIC_PATHS -- it makes a live Spotify call,
        so leaving it open would let anyone burn the account's rate limit.
        Monitors authenticate with the X-DJ-Token header like the CLI does.
        """
        checks = {}

        # Shared with the watchdog rather than reimplemented -- see
        # _sonos_readiness for why there is exactly one copy of this rule.
        _, checks["sonos"] = _sonos_readiness()

        try:
            sp.me()
            checks["spotify"] = "ok"
        except SpotifyBaseException as exc:
            checks["spotify"] = f"error: {exc.__class__.__name__}"
        except requests.exceptions.RequestException as exc:
            checks["spotify"] = f"error: {exc.__class__.__name__}"

        checks["uptime_seconds"] = round(time.monotonic() - SERVER_START)

        # A routine that failed is not a reachability problem, so it is
        # reported but deliberately does not turn the check red -- otherwise a
        # stale error from last week would keep the endpoint alarming forever.
        with _schedules_lock:
            checks["schedule_steps_failed"] = sum(
                1 for entry in _schedules
                for step in entry.get('steps', [])
                if step.get('last_error')
            )

        # 503 so a monitor can alert on the status code alone.
        if checks["sonos"] != "ok" or checks["spotify"] != "ok":
            cherrypy.response.status = 503

        return checks

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.allow(methods=['POST'])
    def sonos_event(self, **_):
        """Receive one push from node-sonos-http-api and fan it out.

        Authenticated by the same X-DJ-Token as the CLI, configured on the
        Sonos side as webhookHeaderName/webhookHeaderContents -- this endpoint
        is reachable from the tunnel like everything else, so it cannot be
        open.

        The payload is deliberately rebuilt with _do_nowplaying rather than
        translated from the event body. Sonos sends a player object, and a
        second mapping from that shape to the UI's would be a second thing to
        keep in step with the first.
        """
        try:
            body = cherrypy.request.body.read(MAX_REQUEST_BODY_BYTES)
            event = json.loads(body) if body else {}
        except ValueError:
            _bad_request("event body was not JSON")

        kind = event.get('type', 'unknown')
        # topology-change fires on grouping and on discovery settling; it says
        # nothing about the track, so it is not worth waking every browser for.
        if kind == 'topology-change':
            return {"status": "ignored", "type": kind}

        payload = self._do_nowplaying()
        payload['event'] = kind
        delivered = _broadcast(payload)
        _record_metric('events_received')
        return {"status": "broadcast", "type": kind, "clients": delivered}

    @cherrypy.expose
    def stream(self):
        """Server-sent events for the browser: told, rather than asking.

        Replaces a 10-second poll that was about 90% of all traffic. Every
        connected stream holds a CherryPy worker thread for its lifetime,
        which is why there is a hard client cap -- running out of workers
        would take down the whole server, not just the stream.
        """
        cherrypy.response.headers['Content-Type'] = 'text/event-stream'
        cherrypy.response.headers['Cache-Control'] = 'no-cache'
        # Tells any buffering proxy in front of us to pass bytes straight
        # through; without it an event can sit unsent for minutes.
        cherrypy.response.headers['X-Accel-Buffering'] = 'no'

        with _stream_lock:
            if len(_stream_clients) >= MAX_STREAM_CLIENTS:
                raise cherrypy.HTTPError(
                    503, "too many open streams -- the UI will fall back to polling")
            client = queue.Queue(maxsize=32)
            _stream_clients.append(client)
            connected = len(_stream_clients)
        with _metrics_lock:
            _metrics['stream_clients_peak'] = max(
                _metrics['stream_clients_peak'], connected)

        def events():
            try:
                # Send the current state immediately, so a browser that has
                # just connected is never showing a blank player while it
                # waits for something to change.
                yield f"data: {json.dumps(self._do_nowplaying())}\n\n"
                while True:
                    try:
                        yield client.get(timeout=STREAM_HEARTBEAT_SECONDS)
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                # Runs on client disconnect too -- CherryPy closes the
                # generator, and a leaked entry would hold a worker forever.
                with _stream_lock:
                    if client in _stream_clients:
                        _stream_clients.remove(client)

        return events()
    stream._cp_config = {'response.stream': True}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def metrics(self):
        """Counters since process start.

        Separate from /health on purpose: /health is polled by the watchdog and
        by anything alerting, and must stay cheap and side-effect free. This
        makes a live Sonos call nowhere -- it only reads what has already
        happened.

        Averages are computed here rather than stored so the stored numbers
        stay addable; transport and content are kept apart because one 46s
        playlist would otherwise swallow the transport average whole.
        """
        with _metrics_lock:
            snapshot = dict(_metrics)
        with _watchdog_lock:
            watchdog = dict(_watchdog)

        transport_calls = snapshot['sonos_calls'] - snapshot['content_loads']
        snapshot['sonos_seconds_avg'] = round(
            snapshot['sonos_seconds_total'] / transport_calls, 4
        ) if transport_calls else 0.0
        snapshot['sonos_seconds_total'] = round(snapshot['sonos_seconds_total'], 3)
        snapshot['sonos_seconds_max'] = round(snapshot['sonos_seconds_max'], 3)
        snapshot['content_seconds_max'] = round(snapshot['content_seconds_max'], 3)
        snapshot['uptime_seconds'] = round(time.monotonic() - SERVER_START)
        snapshot['sonos_ready'] = watchdog['ok']
        snapshot['sonos_outages'] = watchdog['outages']
        return snapshot

    # ==================== QUEUE EDITING ====================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.allow(methods=['POST'])
    def queue_move(self, index=None, to=None, title=None):
        """Move the track at `index` so it ends up at position `to`.

        Both are 1-based, matching what the queue listing shows. `title` is
        the caller's belief about what sits at `index`; the move is refused
        with 409 if that no longer holds.
        """
        start = _validate_int(index, "index", 1, 100000)
        dest = _validate_int(to, "to", 1, 100000)
        if start == dest:
            return {"status": "unchanged", "index": start}

        track = _guard_queue_index(start, title)

        result = self._sonos_request(f"queuemove/{start}/{dest}")
        if "error" in result:
            return result

        log.info("Queue: moved %r from %d to %d", track.get('title'), start, dest)
        return {"status": "moved", "from": start, "to": dest, "title": track.get('title')}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.allow(methods=['POST'])
    def queue_remove(self, index=None, title=None):
        """Remove the track at a 1-based `index`, guarded by `title`."""
        position = _validate_int(index, "index", 1, 100000)
        track = _guard_queue_index(position, title)

        result = self._sonos_request(f"queueremove/{position}")
        if "error" in result:
            return result

        log.info("Queue: removed %r from position %d", track.get('title'), position)
        return {"status": "removed", "index": position, "title": track.get('title')}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def queue_window(self, offset=None, limit=None):
        """A slice of the queue, plus where playback currently is.

        The queue can run to tens of thousands of tracks, so the client asks
        for the part it is showing rather than pulling the lot -- the full
        listing takes longer than the request timeout.
        """
        start = _validate_int(offset or 0, "offset", 0, 100000)
        count = _validate_int(limit or QUEUE_DISPLAY_LIMIT, "limit", 1, 200)
        try:
            entries = _sonos_get_queue(limit=count, offset=start)
        except (RuntimeError, requests.exceptions.RequestException) as exc:
            cherrypy.response.status = 502
            return {"error": str(exc), "queue": []}

        payload = {"queue": entries, "offset": start, "limit": count}
        state = self._sonos_request("state")
        if "error" not in state:
            payload["track_no"] = state.get("trackNo")
        return payload

    # ==================== SCHEDULES ====================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def schedules(self):
        """List schedules, newest state included."""
        with _schedules_lock:
            return {
                "schedules": [_annotate_schedule(e) for e in _schedules],
                "tick_seconds": SCHEDULE_TICK_SECONDS,
            }

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.allow(methods=['POST'])
    def schedule_save(self, **_):
        """Create or replace a whole routine in one request.

        The old flow needed schedule_add then one schedule_step_add per extra
        step, which meant a half-built routine was already live and armed
        between requests, and meant there was no way to change a routine at
        all -- correcting 06:00 to 06:30 required deleting and rebuilding it.
        Taking the entire routine at once makes editing possible and makes
        every save atomic.
        """
        body = _json_body()

        days = body.get('days')
        if isinstance(days, (list, tuple)):
            days = ','.join(str(d) for d in days)
        entry = _validate_schedule(body.get('time'), days, body.get('label'))
        entry['enabled'] = bool(body.get('enabled', True))

        raw_steps = body.get('steps') or []
        if not isinstance(raw_steps, list):
            _bad_request("steps must be a list")
        if len(raw_steps) > MAX_STEPS:
            _bad_request(f"at most {MAX_STEPS} steps per schedule")
        for step in raw_steps:
            if not isinstance(step, dict):
                _bad_request("each step must be an object")
            entry['steps'].append(_validate_step(
                step.get('action'), step.get('offset'),
                step.get('uri'), step.get('volume')))
        entry['steps'].sort(key=lambda s: s.get('offset', 0))

        existing_id = body.get('id') or None
        with _schedules_lock:
            position = next((i for i, e in enumerate(_schedules)
                             if e.get('id') == existing_id), None)
            if existing_id and position is None:
                _bad_request(f"no schedule with id {existing_id!r}")

            if position is None:
                if len(_schedules) >= MAX_SCHEDULES:
                    _bad_request(f"at most {MAX_SCHEDULES} schedules")
                _schedules.append(entry)
                verb = "added"
            else:
                # Carry the fired-stamp across on steps that did not change.
                # Without this, saving a routine during the very minute one of
                # its steps fires would let that step fire a second time on
                # the next tick.
                def key(s):
                    return (s.get('offset', 0), s.get('action'),
                            s.get('uri'), s.get('volume'))
                stamps = {key(s): s.get('last_fired')
                          for s in _schedules[position].get('steps', [])}
                for step in entry['steps']:
                    step['last_fired'] = stamps.get(key(step))
                entry['id'] = existing_id
                _schedules[position] = entry
                verb = "updated"
            _save_schedules_locked()
            result = _annotate_schedule(entry)

        log.info("Schedule %s: %s %s (%d step(s))",
                 verb, entry['time'], entry.get('label', ''), len(entry['steps']))
        return {"status": verb, "schedule": result}




    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.allow(methods=['POST'])
    def schedule_delete(self, id=None):
        with _schedules_lock:
            before = len(_schedules)
            _schedules[:] = [e for e in _schedules if e.get('id') != id]
            if len(_schedules) == before:
                _bad_request(f"no schedule with id {id!r}")
            _save_schedules_locked()
        log.info("Schedule deleted: %s", id)
        return {"status": "deleted", "id": id}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.allow(methods=['POST'])
    def schedule_toggle(self, id=None):
        with _schedules_lock:
            for entry in _schedules:
                if entry.get('id') == id:
                    entry['enabled'] = not entry.get('enabled', True)
                    _save_schedules_locked()
                    log.info("Schedule %s %s", id,
                             "enabled" if entry['enabled'] else "disabled")
                    return {"status": "ok", "schedule": dict(entry)}
        _bad_request(f"no schedule with id {id!r}")

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.allow(methods=['POST'])
    def schedule_run(self, id=None):
        """Run every step of a routine now, ignoring offsets.

        Waiting until tomorrow morning to discover a wrong playlist URI is a
        poor feedback loop. Offsets are skipped deliberately -- nobody wants
        to sit through a 60-minute fade to test it.
        """
        with _schedules_lock:
            entry = next((dict(e) for e in _schedules if e.get('id') == id), None)
        if entry is None:
            _bad_request(f"no schedule with id {id!r}")
        label = entry.get('label') or entry.get('id')
        for step in entry.get('steps', []):
            _fire_schedule(self, {**step, 'label': label})
        return {"status": "ran", "id": id, "steps": len(entry.get('steps', []))}

    # ==================== STATIONS ====================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def stations(self):
        with _stations_lock:
            return {"stations": [dict(s) for s in _stations]}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.allow(methods=['POST'])
    def station_add(self, name=None, uri=None):
        """Save a named radio URI.

        Validated as a Spotify URI like any other: it is interpolated into the
        Sonos request path, so '../../Bedroom/pause' must not get through here
        just because it arrived by a different door.
        """
        clean_uri = _validate_uri(uri)
        clean_name = (name or '').strip()[:80]
        if not clean_name:
            _bad_request("name is required")

        entry = {"id": "stn_" + secrets.token_hex(6), "name": clean_name, "uri": clean_uri}
        with _stations_lock:
            if len(_stations) >= MAX_STATIONS:
                _bad_request(f"at most {MAX_STATIONS} stations")
            _stations.append(entry)
            _save_stations_locked()
        log.info("Station added: %s", clean_name)
        return {"status": "added", "station": entry}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.allow(methods=['POST'])
    def station_delete(self, id=None):
        with _stations_lock:
            before = len(_stations)
            _stations[:] = [s for s in _stations if s.get('id') != id]
            if len(_stations) == before:
                _bad_request(f"no station with id {id!r}")
            _save_stations_locked()
        return {"status": "deleted", "id": id}

    # ==================== CHAT (Natural Language) ====================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def chat(self, message=None, session_id='global'):
        if not message:
            return {"error": "No message provided", "message": "Please say something!"}

        # Both guards are about spend: the message becomes input tokens
        # verbatim, and nothing else on the server costs anything to call.
        if len(message) > MAX_CHAT_MESSAGE_CHARS:
            _bad_request(
                f"Message too long -- keep it under {MAX_CHAT_MESSAGE_CHARS} characters")
        _check_chat_rate(session_id)
        _record_metric('chat_calls')

        # Get Claude's interpretation
        claude_response = call_claude(message, session_id)
        
        if not claude_response:
            return {"error": "Claude not configured", "message": "Natural language not available. Use direct commands."}
        
        action = claude_response.get('action', 'chat')
        friendly_message = claude_response.get('message', '')
        result = {"message": friendly_message, "action": action}
        
        try:
            # Every branch records what the helper returned, so the single
            # check below can catch an upstream failure. Claude's friendly
            # message is written before the call happens, so a branch that
            # drops the result reports success for something that never ran.
            outcome = None

            if action == 'search':
                query = claude_response.get('query', '')
                outcome = self._do_search(q=query, session_id=session_id)
                result['results'] = outcome.get('results', [])
                result['message'] = friendly_message + f" Found {len(result['results'])} tracks."

            elif action == 'play':
                num = claude_response.get('num', 1)
                outcome = self._do_play(num=num, session_id=session_id)
                if outcome.get('item'):
                    result['message'] = f"▶️ Now playing: {outcome['item']['name']}"

            elif action == 'queue':
                num = claude_response.get('num', 1)
                outcome = self._do_queue(num=num, session_id=session_id)
                if outcome.get('item'):
                    result['message'] = f"➕ Queued: {outcome['item']['name']}"

            elif action == 'next':
                num = claude_response.get('num', 1)
                outcome = self._do_next(num=num, session_id=session_id)
                if outcome.get('item'):
                    result['message'] = f"⏭️ Playing next: {outcome['item']['name']}"

            elif action == 'pause':
                outcome = self._do_pause()

            elif action == 'resume':
                outcome = self._do_resume()

            elif action == 'skip':
                outcome = self._do_skip()

            elif action == 'previous':
                outcome = self._do_previous()

            elif action == 'volume':
                level = claude_response.get('level')
                change = claude_response.get('change')
                outcome = self._do_volume(level=level, change=change)

            elif action == 'nowplaying':
                outcome = self._do_nowplaying()
                if outcome.get('title'):
                    result['message'] = f"🎵 {outcome['title']} by {outcome['artist']}"
                else:
                    result['message'] = "🔇 Nothing playing"

            elif action == 'showqueue':
                outcome = self._do_getqueue()
                queue_list = outcome.get('queue', [])
                if queue_list:
                    result['message'] = f"📋 Queue has {len(queue_list)} tracks"
                    result['queue'] = queue_list[:10]
                else:
                    result['message'] = "📭 Queue is empty"

            elif action == 'clear':
                outcome = self._do_clearqueue()
                result['message'] = "🗑️ Queue cleared!"

            # The helpers signal upstream failure by returning {"error": ...}.
            # Replace the optimistic message so the user is not told the music
            # paused while Sonos was unreachable.
            if isinstance(outcome, dict) and 'error' in outcome:
                result['error'] = outcome['error']
                result['message'] = f"❌ {outcome['error']}"
            
        except Exception as e:
            result['message'] = f"Error: {str(e)}"
        
        return result

    # ==================== INTERNAL METHODS ====================

    def _sonos_request(self, endpoint, timeout=None):
        """Make a request to the Sonos HTTP API with error handling.

        Returns parsed JSON on success, or {"ok": True} if the response
        has no JSON body.  On failure returns {"error": "...", "endpoint": endpoint}
        and sets the response status to 502.

        The error dict is returned rather than raised so callers can decide
        what to do with it -- chat() turns it into a friendly sentence, and
        the _do_* helpers propagate it. The 502 is set here, at the single
        point where an upstream failure is detected, so no caller can forget.

        Every Sonos call funnels through here, so this is also the one place
        worth counting from -- see _record_sonos_call.
        """
        started = time.monotonic()
        result = self._sonos_fetch(endpoint, timeout)
        _record_sonos_call(endpoint, time.monotonic() - started, "error" not in result)
        return result

    def _sonos_fetch(self, endpoint, timeout=None):
        """The request itself. Split out only so _sonos_request can time it."""
        if timeout is None:
            timeout = SONOS_TIMEOUT
        url = f"{SONOS_URL}/{endpoint.lstrip('/')}"
        try:
            response = requests.get(url, timeout=timeout)
        except requests.exceptions.Timeout:
            return self._sonos_error(SONOS_TIMEOUT_ERROR, endpoint)
        except requests.exceptions.ConnectionError:
            return self._sonos_error(
                "Cannot reach Sonos API (node-sonos-http-api)", endpoint
            )
        except requests.exceptions.RequestException as e:
            return self._sonos_error(f"Sonos request failed: {str(e)}", endpoint)

        if response.status_code != 200:
            return self._sonos_error(
                f"Sonos returned HTTP {response.status_code}", endpoint
            )

        try:
            return response.json()
        except ValueError:
            return {"ok": True}

    @staticmethod
    def _sonos_error(message, endpoint):
        """Build a Sonos error payload and mark the response as an upstream failure.

        502 rather than 500: the DJ server is fine, the thing behind it is not.
        Without this the caller sees HTTP 200 and a body it may never inspect,
        so a dead Sonos looks like a successful pause.
        """
        log.warning("Sonos %s failed: %s", endpoint, message)
        cherrypy.response.status = 502
        return {"error": message, "endpoint": endpoint}

    @staticmethod
    def _parse_track_id(uri):
        """Extract the Spotify track ID from a Sonos currentTrack URI.

        Sonos reports it wrapped and percent-encoded, e.g.
            x-sonos-spotify:spotify%3atrack%3a0z1IquwlPxx?sid=12&flags=8232
        so the id has to be unquoted, split out of the middle, and stripped of
        the trailing query string.

        Returns None when the track is not a Spotify track (Sonos also plays
        radio and line-in) or when the id does not look like an id -- callers
        pass this straight to the Spotify API.
        """
        if not uri or 'spotify' not in uri:
            return None

        decoded = urllib.parse.unquote(uri)
        if 'track:' not in decoded:
            return None

        track_id = decoded.split('track:')[1].split('?')[0]
        return track_id if SPOTIFY_ID_RE.match(track_id) else None

    def _get_result_item(self, num, session_id='global'):
        """Resolve a 1-based selection number into a stored search result.

        Raises 400 rather than ValueError/IndexError so a typo in `num`
        cannot take the request down with an unhandled exception.
        """
        results = get_results(session_id)
        if not results:
            _bad_request("No search results yet -- run a search first")
        number = _validate_int(num, "num", 1, len(results))
        return results[number - 1]

    @_handles_spotify_errors
    def _do_search(self, q, type="track", limit=None, session_id='global'):
        if limit is None:
            limit = SEARCH_LIMIT
        # A NUL or other control character reached Spotify and came back as a
        # 502; it is the caller's mistake, not the upstream's.
        q = _validate_text(q, "q", 200)
        results = sp.search(q=q, type=type, limit=_validate_int(limit, "limit", 1, 50))
        output = []

        if type == "track":
            for i, track in enumerate(results['tracks']['items'], 1):
                item = {
                    "num": i,
                    "name": track['name'],
                    "artist": track['artists'][0]['name'],
                    "album": track['album']['name'],
                    "uri": track['uri']
                }
                output.append(item)

        elif type == "album":
            for i, album in enumerate(results['albums']['items'], 1):
                output.append({
                    "num": i,
                    "name": album['name'],
                    "artist": album['artists'][0]['name'] if album.get('artists') else '',
                    "tracks": album.get('total_tracks', 0),
                    "year": (album.get('release_date') or '')[:4],
                    "artwork": album['images'][0]['url'] if album.get('images') else None,
                    "uri": album['uri'],
                })

        elif type == "playlist":
            # Spotify can return null entries here; skip rather than crash.
            for i, pl in enumerate(
                    [p for p in results['playlists']['items'] if p], 1):
                output.append({
                    "num": i,
                    "name": pl['name'],
                    "artist": (pl.get('owner') or {}).get('display_name', ''),
                    "tracks": (pl.get('tracks') or {}).get('total', 0),
                    "uri": pl['uri'],
                })

        set_results(output, session_id)
        return {"query": q, "type": type, "results": output}

    def _content_load(self, action, uri, force=False):
        """Issue spotify/{now,queue,next}, collapsing a repeat of the same
        container into the load already in flight or just completed.

        Only containers are deduplicated. A track add returns in about 50ms
        and never times out, so it is never retried -- refusing a deliberate
        second copy of the same song would cost something and prevent nothing.

        A timed-out load is remembered as *ambiguous*, not as failed: the add
        may well have landed on the speaker after we stopped waiting, which is
        exactly how one queue reached 8,950 tracks. A connection error is a
        different thing -- nothing reached the speaker -- so it is deliberately
        not remembered, or the scheduler's retry would have nothing to retry.
        """
        validated = _validate_uri(uri)
        endpoint = f"spotify/{action}/{validated}"

        if force or not _is_container_uri(uri):
            return self._sonos_request(endpoint, timeout=SONOS_CONTENT_TIMEOUT)

        key = (action, validated)
        now = time.monotonic()
        with _content_lock:
            for stale, entry in [(k, v) for k, v in _content_loads.items()
                                 if now - v['at'] > CONTENT_DEDUP_SECONDS]:
                del _content_loads[stale]

            existing = _content_loads.get(key)
            if existing is not None:
                if existing['finished'] is None:
                    return {"status": "already loading", "uri": uri, "deduped": True}
                if existing['ambiguous']:
                    return {
                        "error": "an identical load timed out recently and may "
                                 "still be running -- not repeated",
                        "uri": uri, "deduped": True,
                    }
                return {**existing['result'], "deduped": True}

            _content_loads[key] = {'at': now, 'finished': None,
                                   'result': None, 'ambiguous': False}

        try:
            result = self._sonos_request(endpoint, timeout=SONOS_CONTENT_TIMEOUT)
        except Exception:
            with _content_lock:
                _content_loads.pop(key, None)
            raise

        timed_out = result.get('error') == SONOS_TIMEOUT_ERROR
        with _content_lock:
            if 'error' in result and not timed_out:
                # Certain failure: forget it so a retry can actually retry.
                _content_loads.pop(key, None)
            else:
                entry = _content_loads.get(key)
                if entry is not None:
                    entry.update(at=time.monotonic(), finished=time.monotonic(),
                                 result=result, ambiguous=timed_out)
        return result

    def _do_play(self, num=None, uri=None, session_id='global', force=False):
        if uri:
            result = self._content_load("now", uri, force=force)
            if "error" in result:
                return result
            return {"status": "playing", "uri": uri}

        if num:
            item = self._get_result_item(num, session_id)
            result = self._content_load("now", item['uri'], force=force)
            if "error" in result:
                return result
            return {"status": "playing", "item": item}

        _bad_request("Provide num or uri")

    def _do_queue(self, num=None, uri=None, session_id='global', force=False):
        if uri:
            result = self._content_load("queue", uri, force=force)
            if "error" in result:
                return result
            return {"status": "queued", "uri": uri}

        if num:
            item = self._get_result_item(num, session_id)
            result = self._content_load("queue", item['uri'], force=force)
            if "error" in result:
                return result
            return {"status": "queued", "item": item}

        _bad_request("Provide num or uri")

    def _do_next(self, num=None, uri=None, session_id='global', force=False):
        if uri:
            result = self._content_load("next", uri, force=force)
            if "error" in result:
                return result
            return {"status": "playing next", "uri": uri}

        if num:
            item = self._get_result_item(num, session_id)
            result = self._content_load("next", item['uri'], force=force)
            if "error" in result:
                return result
            return {"status": "playing next", "item": item}

        _bad_request("Provide num or uri")

    def _do_pause(self):
        result = self._sonos_request("pause")
        if "error" in result:
            return result
        return {"status": "paused"}

    def _do_resume(self):
        result = self._sonos_request("play")
        if "error" in result:
            return result
        return {"status": "playing"}

    def _do_skip(self):
        result = self._sonos_request("next")
        if "error" in result:
            return result
        return {"status": "skipped"}

    def _do_previous(self):
        result = self._sonos_request("previous")
        if "error" in result:
            return result
        return {"status": "previous"}

    def _do_volume(self, level=None, change=None):
        # level/change are interpolated into the Sonos path, so they are
        # validated as numbers rather than passed through as free text.
        if level:
            level = _validate_int(level, "level", 0, 100)
            result = self._sonos_request(f"volume/{level}")
            if "error" in result:
                return result
            return {"status": "volume set", "level": level}
        elif change:
            change = _validate_volume_change(change)
            # relvolume, not volume/+N. The shipped action resolves the sign in
            # JavaScript against a cached volume; that was not observed to lose
            # updates (node is single-threaded and updates the cache before the
            # SOAP call), but letting the speaker apply the delta removes the
            # cached value from the story entirely and returns where it landed,
            # which saves the follow-up state read below.
            # See sonos-actions/relvolume.js for the measurements.
            result = self._sonos_request(f"relvolume/{change}")
            if "error" in result:
                return result
            # Report where it landed, not just what was asked for. A caller
            # nudging by one has no way to know the result otherwise, and it
            # differs from the arithmetic whenever Sonos clamps at 0 or 100
            # or something else moved the volume in between. The speaker
            # returns this, so it costs no extra round trip.
            payload = {"status": "volume adjusted", "change": change}
            if result.get('newVolume') is not None:
                payload["level"] = result['newVolume']
            return payload
        else:
            result = self._sonos_request("state")
            if "error" in result:
                return result
            return {"volume": result.get('volume', 'unknown')}

    def _do_shuffle(self, state=None):
        """Read or set shuffle.

        The reply from Sonos is trusted over a follow-up /state read because
        play mode lags: for about a second and a half after the change, state
        still reports the old value. Reading it back would show the user the
        setting they just turned off.
        """
        if state is None:
            result = self._sonos_request("state")
            if "error" in result:
                return result
            return {"shuffle": bool(result.get('playMode', {}).get('shuffle'))}

        wanted = str(state).strip().lower()
        if wanted in ('on', 'true', '1', 'yes'):
            wanted = 'on'
        elif wanted in ('off', 'false', '0', 'no'):
            wanted = 'off'
        else:
            _bad_request("shuffle must be on or off")

        result = self._sonos_request(f"shuffle/{wanted}")
        if "error" in result:
            return result
        return {"status": "shuffle set", "shuffle": wanted == 'on'}

    def _do_nowplaying(self):
        result = self._sonos_request("state")
        if "error" in result:
            return {
                "title": "Nothing playing",
                "artist": "",
                "album": "",
                "artwork": "",
                "uri": "",
                "elapsed": 0,
                "duration": 0,
                "volume": 0,
                "shuffle": False,
                "playbackState": "unknown",
                "error": result["error"]
            }
        track = result.get('currentTrack', {})
        return {
            "title": track.get('title', 'Nothing playing'),
            "artist": track.get('artist', ''),
            "album": track.get('album', ''),
            # Sonos already knows the artwork, so a client does not have to ask
            # Spotify for it. That matters beyond saving a call: album_tracks
            # writes to the shared numbered-results slot, so polling it for
            # artwork silently changed what "play number 3" referred to.
            "artwork": track.get('absoluteAlbumArtUri', ''),
            "uri": track.get('uri', ''),
            # Both in whole seconds. A client polling every few seconds has to
            # tick between polls to look right, so it needs the raw numbers
            # rather than the pre-formatted string Sonos also offers.
            "elapsed": result.get('elapsedTime', 0),
            "duration": track.get('duration', 0),
            "volume": result.get('volume', 0),
            "shuffle": bool(result.get('playMode', {}).get('shuffle')),
            "playbackState": result.get('playbackState', 'unknown')
        }

    def _do_getqueue(self):
        """Return the first QUEUE_DISPLAY_LIMIT tracks in the queue.

        `limit` is echoed back so a client can tell a full queue of exactly
        that many tracks from a truncated one, rather than presenting the cap
        as the real total.

        `track_no` is the 1-based position of the track playing now, which is
        what lets a client separate what has already played from what is still
        to come. Sonos is the only thing that knows this -- Spotify's
        recently-played history stays empty because playback happens through
        Sonos rather than a Spotify client.
        """
        result = self._sonos_request(f"queue/{QUEUE_DISPLAY_LIMIT}")
        if "error" in result:
            return {"queue": [], "error": result["error"]}

        payload = {"queue": result, "limit": QUEUE_DISPLAY_LIMIT}
        state = self._sonos_request("state")
        if "error" not in state:
            payload["track_no"] = state.get("trackNo")
        return payload

    def _do_clearqueue(self):
        result = self._sonos_request("clearqueue")
        if "error" in result:
            return result
        return {"status": "queue cleared"}

    # ==================== PUBLIC API ENDPOINTS ====================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def search(self, q=None, type="track", limit=None):
        if not q:
            _bad_request("No query provided. Use /search?q=your+search+terms")
        return self._do_search(q=q, type=type, limit=limit)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def play(self, num=None, uri=None, force=None):
        # force=1 opts out of the container dedupe, for the rare case of
        # genuinely wanting the same playlist queued twice in a row.
        return self._do_play(num=num, uri=uri, force=_truthy(force))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def queue(self, num=None, uri=None, force=None):
        # force=1 opts out of the container dedupe, for the rare case of
        # genuinely wanting the same playlist queued twice in a row.
        return self._do_queue(num=num, uri=uri, force=_truthy(force))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def next(self, num=None, uri=None, force=None):
        # force=1 opts out of the container dedupe, for the rare case of
        # genuinely wanting the same playlist queued twice in a row.
        return self._do_next(num=num, uri=uri, force=_truthy(force))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def pause(self):
        return self._do_pause()

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def resume(self):
        return self._do_resume()

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def skip(self):
        return self._do_skip()

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def previous(self):
        return self._do_previous()

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def volume(self, level=None, change=None):
        return self._do_volume(level=level, change=change)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def shuffle(self, state=None):
        """Read shuffle with no argument, set it with state=on|off.

        GET like the other transport controls, for the reason given on seek.
        """
        return self._do_shuffle(state=state)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def seek(self, to=None):
        """Jump to a position in the current track, in whole seconds.

        GET like the other transport controls (pause, skip, volume) rather
        than POST: it changes nothing that outlives the track, and the CLI
        addresses them all the same way.

        Sonos wants a bare seconds value here -- passing HH:MM:SS to the
        timeseek action returns a 500.
        """
        seconds = _validate_int(to, "to", 0, 86400)
        result = self._sonos_request(f"timeseek/{seconds}")
        if "error" in result:
            return result
        return {"status": "seeked", "to": seconds}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def nowplaying(self):
        return self._do_nowplaying()

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def getqueue(self):
        return self._do_getqueue()

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def clearqueue(self):
        return self._do_clearqueue()

    # ==================== LIBRARY ====================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @_handles_spotify_errors
    def my(self, action=None, limit=20, offset=0):
        limit = _validate_int(limit, "limit", 1, 50)
        offset = _validate_int(offset, "offset", 0, 100000)

        if action == "playlists":
            results = sp.current_user_playlists(limit=limit, offset=offset)
            output = []
            for i, playlist in enumerate(results['items'], 1):
                item = {
                    "num": i + offset,
                    "name": playlist['name'],
                    "tracks": playlist['tracks']['total'],
                    "uri": playlist['uri']
                }
                output.append(item)
            set_results(output)
            return {"your_playlists": output, "total": results['total']}

        elif action == "liked":
            results = sp.current_user_saved_tracks(limit=limit, offset=offset)
            output = []
            for i, item in enumerate(results['items'], 1):
                track = item['track']
                entry = {
                    "num": i + offset,
                    "name": track['name'],
                    "artist": track['artists'][0]['name'],
                    "uri": track['uri']
                }
                output.append(entry)
            set_results(output)
            return {"your_liked_songs": output, "total": results['total']}

        return {"error": "Use /my/playlists or /my/liked"}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @_handles_spotify_errors
    def like(self):
        state = self._sonos_request("state")
        if "error" in state:
            return state
        track_uri = state.get('currentTrack', {}).get('uri', '')

        track_id = self._parse_track_id(track_uri)
        if not track_id:
            return {"error": "Current track is not from Spotify"}

        sp.current_user_saved_tracks_add(tracks=[track_id])
        return {"status": "liked", "track": state.get('currentTrack', {}).get('title')}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def help(self):
        return {
            "web_ui": "/ui - Interactive web interface",
            "chat": "/chat?message=<text> - Natural language (uses Claude)",
            "search": "/search?q=<query>",
            "playback": {
                "play": "/play?num=<num>",
                "queue": "/queue?num=<num> (add to end)",
                "next": "/next?num=<num> (play after current)",
                "pause": "/pause",
                "resume": "/resume",
                "skip": "/skip",
                "previous": "/previous"
            },
            "volume": "/volume?level=<0-100> or /volume?change=<+/-10>",
            "queue_mgmt": {
                "view": "/getqueue",
                "clear": "/clearqueue"
            },
            "library": {
                "playlists": "/my/playlists",
                "liked": "/my/liked",
                "like": "/like (like current track)"
            }
        }

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.allow(methods=['POST'])
    @_handles_spotify_errors
    def create_playlist(self, name=None):
        # Bounded like a station name. Unbounded, a long one reached Spotify
        # and came back as a 502 -- an upstream failure for what is really a
        # bad request.
        clean_name = _validate_text(name, "name", 80)
        user_id = sp.current_user()['id']
        playlist = sp.user_playlist_create(user_id, clean_name, public=False)
        return {
            "status": "created",
            "name": playlist['name'],
            "uri": playlist['uri'],
            "id": playlist['id']
        }
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.allow(methods=['POST'])
    @_handles_spotify_errors
    def add_to_playlist(self, playlist_id=None, num=None, uri=None):
        if not playlist_id:
            _bad_request("Provide playlist_id")
        
        # Get track URI
        track_uri = uri
        if num and not uri:
            track_uri = self._get_result_item(num)['uri']

        if not track_uri:
            _bad_request("Provide num or uri")

        sp.playlist_add_items(playlist_id, [_validate_uri(track_uri)])
        return {"status": "added", "uri": track_uri, "playlist_id": playlist_id}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @_handles_spotify_errors
    def recommend(self, based_on=None, limit=10):
        """Get top tracks from the current artist"""
        
        if based_on != "nowplaying":
            return {"error": "Use /recommend?based_on=nowplaying"}
        
        # Get currently playing track's artist
        state = self._sonos_request("state")
        if "error" in state:
            return state
        track_uri = state.get('currentTrack', {}).get('uri', '')

        track_id = self._parse_track_id(track_uri)
        if not track_id:
            return {"error": "Current track is not from Spotify"}

        track = sp.track(track_id)
        artist_id = track['artists'][0]['id']
        artist_name = track['artists'][0]['name']
        
        # Get artist's top tracks
        top = sp.artist_top_tracks(artist_id)
        
        output = []
        for i, t in enumerate(top['tracks'][:_validate_int(limit, "limit", 1, 50)], 1):
            item = {
                "num": i,
                "name": t['name'],
                "artist": t['artists'][0]['name'],
                "album": t['album']['name'],
                "uri": t['uri']
            }
            output.append(item)
        
        set_results(output)
        return {"recommendations": output, "artist": artist_name}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @_handles_spotify_errors
    def album_tracks(self, based_on=None, uri=None):
        """List an album's tracks.

        Either name the album (`uri=spotify:album:...`), which is what the
        search results do when you expand one, or ask for whatever is playing
        (`based_on=nowplaying`).
        """
        if uri:
            album_id = _validate_uri(uri).split(':')[-1]
            album = sp.album(album_id)
            output = [{
                "num": i,
                "name": t['name'],
                "artist": t['artists'][0]['name'] if t.get('artists') else '',
                "uri": t['uri'],
                "duration_ms": t.get('duration_ms'),
            } for i, t in enumerate(album['tracks']['items'], 1)]
            # Deliberately NOT stored as the numbered results: expanding an
            # album in the UI should not silently change what "play number 3"
            # means for a CLI session running alongside it.
            return {
                "album": album['name'],
                "artist": album['artists'][0]['name'] if album.get('artists') else '',
                "artwork": album['images'][0]['url'] if album.get('images') else None,
                "year": (album.get('release_date') or '')[:4],
                "uri": album['uri'],
                "tracks": output,
            }

        if based_on != "nowplaying":
            return {"error": "Use /album_tracks?based_on=nowplaying or ?uri=spotify:album:..."}
        
        # Get currently playing track
        state = self._sonos_request("state")
        if "error" in state:
            return state
        track_uri = state.get('currentTrack', {}).get('uri', '')

        track_id = self._parse_track_id(track_uri)
        if not track_id:
            return {"error": "Current track is not from Spotify"}

        track = sp.track(track_id)
        
        album_id = track['album']['id']
        album = sp.album(album_id)
        
        output = []
        for i, t in enumerate(album['tracks']['items'], 1):
            item = {
                "num": i,
                "name": t['name'],
                "artist": t['artists'][0]['name'],
                "uri": t['uri'],
                "duration_ms": t['duration_ms']
            }
            output.append(item)
        
        set_results(output)
        return {
            "album": album['name'],
            "artist": album['artists'][0]['name'],
            "artwork": album['images'][0]['url'] if album['images'] else None,
            "year": album['release_date'][:4] if album['release_date'] else None,
            "tracks": output
        }

if __name__ == '__main__':
    # One line stating what this process actually believes, so a misconfigured
    # restart is visible in the log instead of being inferred from behaviour.
    # No secret values -- only whether each one is present.
    log.info(
        "Starting DJ server: room=%s model=%s auth=%s cli_token=%s claude=%s",
        SONOS_ROOM,
        CLAUDE_MODEL,
        "on" if UI_PASSWORD else "OFF (all endpoints public)",
        "set" if CLI_TOKEN else "MISSING (dj CLI will get 401s)",
        "configured" if claude else "disabled (no api key)",
    )
    if not UI_PASSWORD:
        # Reaching here now means allow_open_access was set deliberately, so
        # this records a choice rather than reporting an accident.
        log.warning(
            "ui_password is empty and allow_open_access is set -- "
            "every endpoint is reachable without credentials")

    log.info("Loaded %d schedule(s) from %s", len(_schedules), SCHEDULES_PATH)

    cherrypy.config.update({
        'server.socket_host': '0.0.0.0',
        'server.socket_port': _setting('server_port'),
        # CherryPy defaults to 100MB. The largest legitimate body here is a
        # routine with the maximum number of steps, a few kilobytes; a 38MB
        # body was accepted, buffered and parsed.
        # SSE holds a worker per connected browser; the default of 10
        # would be exhausted by a few open tabs and stall every request.
        'server.thread_pool': _setting('server_thread_pool'),
        'server.max_request_body_size': MAX_REQUEST_BODY_BYTES,
        # Without this CherryPy also writes both logs to stdout, which the
        # plist sends to the crash log -- every line stored twice, and the
        # crash log growing exactly as fast as the one now being rotated.
        'log.screen': False,
    })
    _route_cherrypy_logs_to_file()

    dj_server = DJServer()

    # A CherryPy Monitor rather than a bare thread: it starts and stops with
    # the engine, so a restart cannot leave an orphaned ticker behind.
    cherrypy.process.plugins.Monitor(
        cherrypy.engine,
        lambda: run_due_schedules(dj_server),
        frequency=SCHEDULE_TICK_SECONDS,
        name='dj_scheduler',
    ).subscribe()

    cherrypy.process.plugins.Monitor(
        cherrypy.engine,
        check_sonos_readiness,
        frequency=WATCHDOG_TICK_SECONDS,
        name='dj_watchdog',
    ).subscribe()

    cherrypy.quickstart(dj_server)