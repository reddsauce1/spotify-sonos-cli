import anthropic
import cherrypy
import spotipy
from spotipy.exceptions import SpotifyBaseException, SpotifyException
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError
import requests
import functools
import json
import os
import re
import secrets
import time
import urllib.parse

# Load config
config_path = os.path.join(os.path.dirname(__file__), 'config.json')
with open(config_path) as f:
    config = json.load(f)

# Spotify setup
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=config['client_id'],
    client_secret=config['client_secret'],
    redirect_uri="http://127.0.0.1:8888/callback",
    scope="user-library-read user-library-modify playlist-read-private playlist-modify-public playlist-modify-private user-read-recently-played user-top-read",
    cache_path=os.path.join(os.path.dirname(__file__), '.cache')
))

# Sonos setup
SONOS_ROOM = config.get('sonos_room', 'Dining%20Room')
# /zones and other API-wide endpoints are not room-scoped, so keep the base
# separate from the room-prefixed URL the playback helpers use.
SONOS_BASE_URL = "http://localhost:5005"
SONOS_URL = f"{SONOS_BASE_URL}/{SONOS_ROOM}"

# Monotonic so uptime is unaffected by the clock being adjusted under us.
SERVER_START = time.monotonic()

# node-sonos-http-api serves the entire queue from /queue. On a long queue that
# is several megabytes and takes over 6 seconds -- longer than the request
# timeout -- so getqueue used to time out every time and report an empty queue.
# /queue/{limit} answers in milliseconds.
QUEUE_DISPLAY_LIMIT = 50

# Claude setup
ANTHROPIC_API_KEY = config.get('anthropic_api_key', '')

# The SDK handles retries (429 and 5xx) and connection errors with backoff.
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

CLAUDE_MODEL = "claude-sonnet-5"

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

# Cap on stored sessions -- each login adds one and nothing removes them, so
# without this a long-running server leaks memory one token at a time.
MAX_SESSIONS = 100

# Paths reachable without credentials. Everything else is denied by default,
# so a new endpoint is protected unless it is deliberately added here.
PUBLIC_PATHS = {'', '/index', '/ui', '/login'}

# Store last search results (per session for web, global for CLI)
search_results = {'global': []}


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
            raise cherrypy.HTTPError(
                502, f"Spotify authorisation failed ({exc}). Re-run auth.py to refresh .cache."
            )
        except SpotifyException as exc:
            status = getattr(exc, 'http_status', None)
            detail = getattr(exc, 'msg', None) or str(exc)
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

    return wrapper

def get_results(session_id='global'):
    """Get search results for a session"""
    return search_results.get(session_id, [])


def set_results(results, session_id='global'):
    """Store search results for a session"""
    search_results[session_id] = results


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
            max_tokens=512,
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
        print(f"Claude API error {exc.status_code}: {exc.message}")
        return {"action": "chat", "message": "Sorry, I had trouble understanding that. Try again!"}
    except anthropic.APIConnectionError as exc:
        print(f"Claude unreachable: {exc}")
        return {"action": "chat", "message": "I can't reach my brain right now. Use the buttons instead!"}

    if response.stop_reason == "refusal":
        return {"action": "chat", "message": "I'd rather not answer that one!"}

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        # max_tokens truncation is the realistic cause of an empty response.
        print(f"Claude returned no text (stop_reason={response.stop_reason})")
        return {"action": "chat", "message": "Sorry, I had trouble understanding that. Try again!"}

    return json.loads(text)


class DJServer:

    # Auth is on for every handler; PUBLIC_PATHS carves out the exceptions.
    # Error pages render as JSON so clients never have to parse HTML.
    _cp_config = {
        'tools.djauth.on': True,
        'error_page.400': _json_error_page,
        'error_page.401': _json_error_page,
        'error_page.404': _json_error_page,
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

        return '''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>DJ Assistant</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            max-width: 500px;
            margin: 0 auto;
            padding: 20px;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: white;
        }
        h1 { text-align: center; font-size: 1.8em; margin-bottom: 5px; }
        .subtitle { text-align: center; color: #888; margin-bottom: 20px; font-size: 14px; }
        .input-container { display: flex; gap: 10px; margin-bottom: 15px; }
        input[type="text"] {
            flex: 1;
            padding: 15px;
            font-size: 16px;
            border: none;
            border-radius: 10px;
            background: rgba(255,255,255,0.1);
            color: white;
        }
        input[type="text"]::placeholder { color: #888; }
        button {
            padding: 15px 20px;
            font-size: 16px;
            border: none;
            border-radius: 10px;
            background: #1db954;
            color: white;
            cursor: pointer;
            font-weight: bold;
        }
        button:hover { background: #1ed760; }
        button:disabled { background: #555; cursor: not-allowed; }
        .status {
            background: rgba(255,255,255,0.1);
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 15px;
            min-height: 50px;
            line-height: 1.5;
        }
        .results {
            background: rgba(255,255,255,0.05);
            border-radius: 10px;
            overflow: hidden;
        }
        .result-item {
            padding: 12px 15px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            display: flex;
            gap: 10px;
            align-items: center;
        }
        .result-item:last-child { border-bottom: none; }
        .result-num { 
            background: rgba(255,255,255,0.2); 
            border-radius: 50%; 
            width: 28px; 
            height: 28px; 
            display: flex; 
            align-items: center; 
            justify-content: center;
            font-size: 14px;
            flex-shrink: 0;
        }
        .result-info { flex: 1; min-width: 0; }
        .song-name { font-weight: bold; margin-bottom: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .artist-name { color: #888; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .result-actions {
            display: flex;
            gap: 6px;
            flex-shrink: 0;
        }
        .action-btn {
            padding: 8px 12px;
            font-size: 11px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-weight: bold;
            transition: transform 0.1s;
        }
        .action-btn:hover { transform: scale(1.05); }
        .action-btn:active { transform: scale(0.95); }
        .btn-play {
            background: #1db954;
            color: white;
        }
        .btn-next {
            background: #f59e0b;
            color: white;
        }
        .btn-queue {
            background: #6366f1;
            color: white;
        }
        .quick-buttons { 
            display: grid; 
            grid-template-columns: repeat(4, 1fr); 
            gap: 8px; 
            margin-bottom: 15px; 
        }
        .quick-btn {
            padding: 12px 8px;
            font-size: 12px;
            background: rgba(255,255,255,0.1);
            border: none;
            border-radius: 8px;
            color: white;
            cursor: pointer;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 4px;
        }
        .quick-btn:hover { background: rgba(255,255,255,0.2); }
        .quick-btn .icon { font-size: 18px; }
        .nowplaying {
            background: rgba(29, 185, 84, 0.2);
            border: 1px solid rgba(29, 185, 84, 0.3);
            padding: 12px 15px;
            border-radius: 10px;
            margin-bottom: 15px;
            font-size: 14px;
        }
        .nowplaying .label { color: #1db954; font-size: 11px; text-transform: uppercase; margin-bottom: 5px; }
        .loading { opacity: 0.6; }
        .toast {
            position: fixed;
            bottom: 20px;
            left: 50%;
            transform: translateX(-50%);
            background: #333;
            color: white;
            padding: 12px 24px;
            border-radius: 8px;
            font-size: 14px;
            opacity: 0;
            transition: opacity 0.3s;
            z-index: 1000;
        }
        .toast.show { opacity: 1; }
    </style>
</head>
<body>
    <h1>🎵 DJ Assistant</h1>
    <p class="subtitle">Ask me to play something!</p>
    
    <div class="nowplaying" id="nowplaying">
        <div class="label">Now Playing</div>
        <div id="nowplaying-text">Loading...</div>
    </div>
    
    <div class="input-container">
        <input type="text" id="message" placeholder="e.g., play some jazz..." autofocus>
        <button onclick="sendChat()" id="sendBtn">Send</button>
    </div>
    
    <div class="quick-buttons">
        <button class="quick-btn" onclick="quickCmd('pause')">
            <span class="icon">⏸</span>
            <span>Pause</span>
        </button>
        <button class="quick-btn" onclick="quickCmd('resume')">
            <span class="icon">▶️</span>
            <span>Play</span>
        </button>
        <button class="quick-btn" onclick="quickCmd('skip')">
            <span class="icon">⏭</span>
            <span>Skip</span>
        </button>
        <button class="quick-btn" onclick="loadPlaylists()">
            <span class="icon">📚</span>
            <span>Playlists</span>
        </button>
        <button class="quick-btn" onclick="quickCmd('volume', 'up')">
            <span class="icon">🔊</span>
            <span>Vol +</span>
        </button>
        <button class="quick-btn" onclick="quickCmd('volume', 'down')">
            <span class="icon">🔉</span>
            <span>Vol -</span>
        </button>
        <button class="quick-btn" onclick="quickCmd('getqueue')">
            <span class="icon">📋</span>
            <span>Queue</span>
        </button>
        <button class="quick-btn" onclick="quickCmd('clearqueue')">
            <span class="icon">🗑</span>
            <span>Clear</span>
        </button>
    </div>
    
    <div class="status" id="status">🎧 What do you want to hear?</div>
    
    <div class="results" id="results"></div>
    
    <div class="toast" id="toast"></div>

    <script>
        refreshNowPlaying();
        setInterval(refreshNowPlaying, 30000);
        
        function showToast(message) {
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 2500);
        }
        
        function refreshNowPlaying() {
            fetch('/nowplaying')
                .then(r => r.json())
                .then(data => {
                    const el = document.getElementById('nowplaying-text');
                    if (data.title && data.title !== 'Nothing playing') {
                        el.innerHTML = '<strong>' + data.title + '</strong> by ' + data.artist;
                        loadAlbumTracks();
                    } else {
                        el.textContent = 'Nothing playing';
                    }
                })
                .catch(() => {
                    document.getElementById('nowplaying-text').textContent = 'Unable to fetch';
                });
        }
        
        document.getElementById('message').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') sendChat();
        });
        
        function loadAlbumTracks() {
            fetch('/album_tracks?based_on=nowplaying')
                .then(r => r.json())
                .then(data => {
                    if (data.error) return;
                    showAlbum(data);
                })
                .catch(() => {});
        }
        
        function showAlbum(data) {
            let html = '<div style="padding: 15px; display: flex; gap: 15px; align-items: flex-start; border-bottom: 1px solid rgba(255,255,255,0.1);">';
            if (data.artwork) {
                html += '<img src="' + data.artwork + '" style="width: 100px; height: 100px; border-radius: 8px;">';
            }
            html += '<div><div style="font-weight: bold; margin-bottom: 4px;">' + data.album + '</div>';
            html += '<div style="color: #888; font-size: 13px;">' + data.artist + ' • ' + data.year + '</div></div></div>';
            
            html += data.tracks.map(item => 
                '<div class="result-item">' +
                '<div class="result-num">' + item.num + '</div>' +
                '<div class="result-info">' +
                '<div class="song-name">' + item.name + '</div>' +
                '</div>' +
                '<div class="result-actions">' +
                '<button class="action-btn btn-play" onclick="trackAction(' + item.num + ', &quot;play&quot;)">▶️</button>' +
                '<button class="action-btn btn-next" onclick="trackAction(' + item.num + ', &quot;next&quot;)">⏭️</button>' +
                '<button class="action-btn btn-queue" onclick="trackAction(' + item.num + ', &quot;queue&quot;)">+</button>' +
                '</div>' +
                '</div>'
            ).join('');
            
            document.getElementById('results').innerHTML = html;
            document.getElementById('status').innerHTML = '💿 ' + data.album;
        }

        function sendChat() {
            const input = document.getElementById('message');
            const message = input.value.trim();
            if (!message) return;
            
            document.getElementById('status').innerHTML = '💭 Thinking...';
            document.getElementById('status').classList.add('loading');
            
            fetch('/chat?message=' + encodeURIComponent(message))
                .then(r => r.json())
                .then(data => {
                    document.getElementById('status').classList.remove('loading');
                    document.getElementById('status').innerHTML = data.message || '✓ Done';
                    
                    if (data.results) {
                        showResults(data.results);
                    } else {
                        document.getElementById('results').innerHTML = '';
                    }
                    
                    if (data.action !== 'search') {
                        refreshNowPlaying();
                    }
                })
                .catch(err => {
                    document.getElementById('status').classList.remove('loading');
                    document.getElementById('status').innerHTML = '❌ Error: ' + err.message;
                });
            
            input.value = '';
        }
        
        function showResults(results) {
            const html = results.map(item => 
                '<div class="result-item">' +
                '<div class="result-num">' + item.num + '</div>' +
                '<div class="result-info">' +
                '<div class="song-name">' + item.name + '</div>' +
                '<div class="artist-name">' + item.artist + (item.album ? ' • ' + item.album : '') + '</div>' +
                '</div>' +
                '<div class="result-actions">' +
                '<button class="action-btn btn-play" onclick="trackAction(' + item.num + ', &quot;play&quot;)">▶️</button>' +
                '<button class="action-btn btn-next" onclick="trackAction(' + item.num + ', &quot;next&quot;)">⏭️</button>' +
                '<button class="action-btn btn-queue" onclick="trackAction(' + item.num + ', &quot;queue&quot;)">+</button>' +
                '</div>' +
                '</div>'
            ).join('');
            document.getElementById('results').innerHTML = html;
        }
        
        function trackAction(num, action) {
            const endpoint = '/' + action + '?num=' + num;
            const messages = {
                'play': '▶️ Playing now...',
                'next': '⏭️ Playing next...',
                'queue': '➕ Adding to queue...'
            };
            
            showToast(messages[action]);
            
            fetch(endpoint)
                .then(r => r.json())
                .then(data => {
                    if (data.item) {
                        const actionText = {
                            'play': '▶️ Now playing: ',
                            'next': '⏭️ Up next: ',
                            'queue': '➕ Queued: '
                        };
                        document.getElementById('status').innerHTML = actionText[action] + data.item.name;
                        if (action === 'play') {
                            refreshNowPlaying();
                        }
                    } else {
                        document.getElementById('status').innerHTML = '❌ ' + (data.error || 'Failed');
                    }
                });
        }
        
        function quickCmd(cmd, arg) {
            let url = '/' + cmd;
            if (cmd === 'volume') {
                url = '/volume?change=' + (arg === 'up' ? '%2B10' : '-10');
            }
            
            fetch(url)
                .then(r => r.json())
                .then(data => {
                    if (cmd === 'getqueue') {
                        showQueue(data.queue || []);
                    } else {
                        showToast('✓ ' + (data.status || 'Done'));
                        refreshNowPlaying();
                    }
                });
        }
        
        function showQueue(queue) {
            if (!queue.length) {
                document.getElementById('status').innerHTML = '📭 Queue is empty';
                document.getElementById('results').innerHTML = '';
                return;
            }
            document.getElementById('status').innerHTML = '📋 Current queue (' + queue.length + ' tracks)';
            const html = queue.slice(0, 10).map((item, i) => 
                '<div class="result-item">' +
                '<div class="result-num">' + (i + 1) + '</div>' +
                '<div class="result-info">' +
                '<div class="song-name">' + item.title + '</div>' +
                '<div class="artist-name">' + item.artist + '</div>' +
                '</div></div>'
            ).join('');
            document.getElementById('results').innerHTML = html;
        }
        
        function loadPlaylists() {
            document.getElementById('status').innerHTML = '📚 Loading playlists...';
            fetch('/my/playlists?limit=50')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('status').innerHTML = '📚 Your Playlists (' + data.your_playlists.length + ')';
                    showPlaylists(data.your_playlists);
                })
                .catch(err => {
                    document.getElementById('status').innerHTML = '❌ Error loading playlists';
                });
        }
        
        function showPlaylists(playlists) {
            const html = playlists.map(item => 
                '<div class="result-item">' +
                '<div class="result-num">' + item.num + '</div>' +
                '<div class="result-info">' +
                '<div class="song-name">' + item.name + '</div>' +
                '<div class="artist-name">' + item.tracks + ' tracks</div>' +
                '</div>' +
                '<div class="result-actions">' +
                '<button class="action-btn btn-play" onclick="playPlaylist(' + item.num + ')">▶️</button>' +
                '</div>' +
                '</div>'
            ).join('');
            document.getElementById('results').innerHTML = html;
        }
        
        function playPlaylist(num) {
            showToast('▶️ Starting playlist...');
            fetch('/play?num=' + num)
                .then(r => r.json())
                .then(data => {
                    if (data.status) {
                        document.getElementById('status').innerHTML = '▶️ Playing playlist';
                        refreshNowPlaying();
                    } else {
                        document.getElementById('status').innerHTML = '❌ ' + (data.error || 'Failed');
                    }
                });
        }
    </script>
</body>
</html>'''

    @cherrypy.expose
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
            cherrypy.response.cookie['dj_auth']['max-age'] = 86400 * 7
            cherrypy.response.cookie['dj_auth']['httponly'] = True
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

        try:
            response = requests.get(f"{SONOS_BASE_URL}/zones", timeout=3)
            checks["sonos"] = (
                "ok" if response.status_code == 200
                else f"error: HTTP {response.status_code}"
            )
        except requests.exceptions.RequestException as exc:
            # Class name only -- the full message can carry internal hostnames.
            checks["sonos"] = f"error: {exc.__class__.__name__}"

        try:
            sp.me()
            checks["spotify"] = "ok"
        except SpotifyBaseException as exc:
            checks["spotify"] = f"error: {exc.__class__.__name__}"
        except requests.exceptions.RequestException as exc:
            checks["spotify"] = f"error: {exc.__class__.__name__}"

        checks["uptime_seconds"] = round(time.monotonic() - SERVER_START)

        # 503 so a monitor can alert on the status code alone.
        if checks["sonos"] != "ok" or checks["spotify"] != "ok":
            cherrypy.response.status = 503

        return checks

    # ==================== CHAT (Natural Language) ====================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def chat(self, message=None, session_id='global'):
        if not message:
            return {"error": "No message provided", "message": "Please say something!"}
        
        # Get Claude's interpretation
        claude_response = call_claude(message, session_id)
        
        if not claude_response:
            return {"error": "Claude not configured", "message": "Natural language not available. Use direct commands."}
        
        action = claude_response.get('action', 'chat')
        friendly_message = claude_response.get('message', '')
        result = {"message": friendly_message, "action": action}
        
        try:
            if action == 'search':
                query = claude_response.get('query', '')
                search_result = self._do_search(q=query, session_id=session_id)
                result['results'] = search_result.get('results', [])
                result['message'] = friendly_message + f" Found {len(result['results'])} tracks."
            
            elif action == 'play':
                num = claude_response.get('num', 1)
                play_result = self._do_play(num=num, session_id=session_id)
                if play_result.get('item'):
                    result['message'] = f"▶️ Now playing: {play_result['item']['name']}"
            
            elif action == 'queue':
                num = claude_response.get('num', 1)
                queue_result = self._do_queue(num=num, session_id=session_id)
                if queue_result.get('item'):
                    result['message'] = f"➕ Queued: {queue_result['item']['name']}"
            
            elif action == 'next':
                num = claude_response.get('num', 1)
                next_result = self._do_next(num=num, session_id=session_id)
                if next_result.get('item'):
                    result['message'] = f"⏭️ Playing next: {next_result['item']['name']}"
            
            elif action == 'pause':
                self._do_pause()
            
            elif action == 'resume':
                self._do_resume()
            
            elif action == 'skip':
                self._do_skip()
            
            elif action == 'previous':
                self._do_previous()
            
            elif action == 'volume':
                level = claude_response.get('level')
                change = claude_response.get('change')
                self._do_volume(level=level, change=change)
            
            elif action == 'nowplaying':
                np = self._do_nowplaying()
                if np.get('title'):
                    result['message'] = f"🎵 {np['title']} by {np['artist']}"
                else:
                    result['message'] = "🔇 Nothing playing"
            
            elif action == 'showqueue':
                q = self._do_getqueue()
                queue_list = q.get('queue', [])
                if queue_list:
                    result['message'] = f"📋 Queue has {len(queue_list)} tracks"
                    result['queue'] = queue_list[:10]
                else:
                    result['message'] = "📭 Queue is empty"
            
            elif action == 'clear':
                self._do_clearqueue()
                result['message'] = "🗑️ Queue cleared!"
            
        except Exception as e:
            result['message'] = f"Error: {str(e)}"
        
        return result

    # ==================== INTERNAL METHODS ====================

    def _sonos_request(self, endpoint, timeout=5):
        """Make a request to the Sonos HTTP API with error handling.

        Returns parsed JSON on success, or {"ok": True} if the response
        has no JSON body.  On failure returns {"error": "...", "endpoint": endpoint}
        and sets the response status to 502.

        The error dict is returned rather than raised so callers can decide
        what to do with it -- chat() turns it into a friendly sentence, and
        the _do_* helpers propagate it. The 502 is set here, at the single
        point where an upstream failure is detected, so no caller can forget.
        """
        url = f"{SONOS_URL}/{endpoint.lstrip('/')}"
        try:
            response = requests.get(url, timeout=timeout)
        except requests.exceptions.Timeout:
            return self._sonos_error("Sonos request timed out", endpoint)
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
        cherrypy.response.status = 502
        return {"error": message, "endpoint": endpoint}

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
    def _do_search(self, q, type="track", limit=5, session_id='global'):
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

        set_results(output, session_id)
        return {"query": q, "type": type, "results": output}

    def _do_play(self, num=None, uri=None, session_id='global'):
        if uri:
            result = self._sonos_request(f"spotify/now/{_validate_uri(uri)}")
            if "error" in result:
                return result
            return {"status": "playing", "uri": uri}

        if num:
            item = self._get_result_item(num, session_id)
            result = self._sonos_request(f"spotify/now/{_validate_uri(item['uri'])}")
            if "error" in result:
                return result
            return {"status": "playing", "item": item}

        _bad_request("Provide num or uri")

    def _do_queue(self, num=None, uri=None, session_id='global'):
        if uri:
            result = self._sonos_request(f"spotify/queue/{_validate_uri(uri)}")
            if "error" in result:
                return result
            return {"status": "queued", "uri": uri}

        if num:
            item = self._get_result_item(num, session_id)
            result = self._sonos_request(f"spotify/queue/{_validate_uri(item['uri'])}")
            if "error" in result:
                return result
            return {"status": "queued", "item": item}

        _bad_request("Provide num or uri")

    def _do_next(self, num=None, uri=None, session_id='global'):
        if uri:
            result = self._sonos_request(f"spotify/next/{_validate_uri(uri)}")
            if "error" in result:
                return result
            return {"status": "playing next", "uri": uri}

        if num:
            item = self._get_result_item(num, session_id)
            result = self._sonos_request(f"spotify/next/{_validate_uri(item['uri'])}")
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
            result = self._sonos_request(f"volume/{change}")
            if "error" in result:
                return result
            return {"status": "volume adjusted", "change": change}
        else:
            result = self._sonos_request("state")
            if "error" in result:
                return result
            return {"volume": result.get('volume', 'unknown')}

    def _do_nowplaying(self):
        result = self._sonos_request("state")
        if "error" in result:
            return {
                "title": "Nothing playing",
                "artist": "",
                "album": "",
                "volume": 0,
                "playbackState": "unknown",
                "error": result["error"]
            }
        track = result.get('currentTrack', {})
        return {
            "title": track.get('title', 'Nothing playing'),
            "artist": track.get('artist', ''),
            "album": track.get('album', ''),
            "volume": result.get('volume', 0),
            "playbackState": result.get('playbackState', 'unknown')
        }

    def _do_getqueue(self):
        """Return the first QUEUE_DISPLAY_LIMIT tracks in the queue.

        `limit` is echoed back so a client can tell a full queue of exactly
        that many tracks from a truncated one, rather than presenting the cap
        as the real total.
        """
        result = self._sonos_request(f"queue/{QUEUE_DISPLAY_LIMIT}")
        if "error" in result:
            return {"queue": [], "error": result["error"]}
        return {"queue": result, "limit": QUEUE_DISPLAY_LIMIT}

    def _do_clearqueue(self):
        result = self._sonos_request("clearqueue")
        if "error" in result:
            return result
        return {"status": "queue cleared"}

    # ==================== PUBLIC API ENDPOINTS ====================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def search(self, q=None, type="track", limit=5):
        if not q:
            _bad_request("No query provided. Use /search?q=your+search+terms")
        return self._do_search(q=q, type=type, limit=limit)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def play(self, num=None, uri=None):
        return self._do_play(num=num, uri=uri)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def queue(self, num=None, uri=None):
        return self._do_queue(num=num, uri=uri)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def next(self, num=None, uri=None):
        return self._do_next(num=num, uri=uri)

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

        if 'spotify' not in track_uri:
            return {"error": "Current track is not from Spotify"}

        decoded = urllib.parse.unquote(track_uri)
        if 'track:' in decoded:
            track_id = decoded.split('track:')[1].split('?')[0]
            sp.current_user_saved_tracks_add(tracks=[track_id])
            return {"status": "liked", "track": state.get('currentTrack', {}).get('title')}

        return {"error": "Could not parse track URI"}

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
    @_handles_spotify_errors
    def create_playlist(self, name=None):
        if not name:
            _bad_request("Provide playlist name: /create_playlist?name=My%20Playlist")
        
        user_id = sp.current_user()['id']
        playlist = sp.user_playlist_create(user_id, name, public=False)
        return {
            "status": "created",
            "name": playlist['name'],
            "uri": playlist['uri'],
            "id": playlist['id']
        }
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
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

        if 'spotify' not in track_uri:
            return {"error": "Current track is not from Spotify"}

        decoded = urllib.parse.unquote(track_uri)
        if 'track:' not in decoded:
            return {"error": "Can't parse track URI"}

        track_id = decoded.split('track:')[1].split('?')[0]
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
    def album_tracks(self, based_on=None):
        """Get all tracks from the current song's album"""
        
        if based_on != "nowplaying":
            return {"error": "Use /album_tracks?based_on=nowplaying"}
        
        # Get currently playing track
        state = self._sonos_request("state")
        if "error" in state:
            return state
        track_uri = state.get('currentTrack', {}).get('uri', '')

        if 'spotify' not in track_uri:
            return {"error": "Current track is not from Spotify"}
        
        decoded = urllib.parse.unquote(track_uri)
        if 'track:' not in decoded:
            return {"error": "Can't parse track URI"}
        
        track_id = decoded.split('track:')[1].split('?')[0]
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
    cherrypy.config.update({
        'server.socket_host': '0.0.0.0',
        'server.socket_port': 5006
    })
    cherrypy.quickstart(DJServer())