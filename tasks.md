# Spotify-Server Hardening Tasks

Ordered by risk, not by category. The server is internet-facing via Cloudflare
Tunnel, so anything reachable without a cookie is reachable by anyone.

## Phase 0: Close the door

`UI_PASSWORD` is currently checked in exactly one place — `ui()`, server.py:158.
The other 21 exposed endpoints have no auth at all, including `/chat`, which
bills the Anthropic API key on every call. Do this phase before anything else.

- [x] add a _require_auth(self) method or CherryPy before_handler tool that checks the dj_auth cookie. Apply to all exposed API endpoints: search, play, queue, next, pause, resume, skip, previous, volume, nowplaying, getqueue, clearqueue, my, like, help, create_playlist, add_to_playlist, recommend, album_tracks. Return 401 JSON error if not authenticated
- [x] replace plaintext password in cookie: generate a random session token on login with secrets.token_hex(), store in a server-side set, put the token (not the password) in the cookie. Validate by checking token membership
- [x] add input validation: wrap int(num) in try/except ValueError in all endpoints that accept num parameter, validate Spotify URI format (must start with spotify:) before passing to Sonos, return proper 400 JSON errors
- [ ] rotate the Spotify OAuth token in .cache — it is stored in plaintext and the refresh token is long-lived, granting full library read/write. Re-run auth on a trusted machine and replace the file

## Phase 1: Make failures visible

Crash-safety plus the ability to tell something broke before a party does.

- [x] install pytest into venv and run the existing suite (test_server.py, test_sonos_request.py, conftest.py — ~970 lines that have never been executed on this machine). Fix or delete whatever fails. Every task below should land with a test
- [x] wrap all sp.*() Spotify API calls in try/except for SpotifyException and SpotifyOauthError, return JSON error responses instead of crashing. Affects _do_search, my, like, create_playlist, add_to_playlist, recommend, album_tracks
- [x] fix Claude API response parsing in call_claude(): validate response structure before indexing result['content'][0]['text'], handle non-JSON Claude output gracefully, add retry on 429 rate limit
- [x] add a /health endpoint that checks Sonos connectivity (GET to localhost:5005/zones with 3s timeout), checks Spotify token validity (sp.me() in try/except), and returns JSON {sonos: ok/error, spotify: ok/error, uptime_seconds: N}
- [x] return proper HTTP status codes: 400 for bad input (missing params, invalid num), 401 for unauthenticated requests, 502 for Sonos/Spotify upstream failures. Use cherrypy.response.status to set codes before returning JSON

## Phase 2: Structural debt

The first two items are a single edit — extract the HTML and escape it in the
same pass rather than touching that markup twice.

- [x] extract the ~500-line HTML/CSS/JS block from the ui() method (lines 222-657) into a new file static/index.html. Configure CherryPy to serve static files from the static/ directory. The ui() method should check auth then serve the file
- [x] add an escapeHtml(str) JavaScript function to the web UI that escapes &<>"' characters. Replace all innerHTML assignments that insert dynamic data (track names, artist names, album names, artwork URLs) with either textContent or escaped innerHTML
- [x] extract _get_result_item(self, num, session_id) helper to validate int conversion and bounds checking, replace the 4 duplicated blocks in _do_play, _do_queue, _do_next, and add_to_playlist (done alongside input validation -- the helper *is* the validation)
- [x] extract _parse_track_id(self, uri) helper to parse Spotify track ID from URI, replace the 3 duplicated blocks in like, recommend, and album_tracks
- [x] consolidate all hardcoded magic numbers into a DEFAULTS dict at top of server.py (claude_timeout, claude_max_tokens, claude_model, search_limit, queue_display_limit, cookie_max_age, server_port). Load overrides from config.json. Use these values everywhere instead of literals
- [x] add config.json validation on startup: check that client_id, client_secret exist and are non-empty strings. Print clear error message and exit(1) if validation fails. Make anthropic_api_key, sonos_room, ui_password optional with defaults
- [x] add session expiry to search_results: store (timestamp, results) tuples, add a cleanup function that removes entries older than 1 hour, call cleanup on every new search, cap total sessions at 100
- [x] replace all print() calls with Python logging module. Create a logger with structured format including timestamp and level. Configure for systemd journal output
- [x] replace the raw requests.post() call to Anthropic API in call_claude() with the anthropic Python SDK (import anthropic, client = anthropic.Anthropic()). Use client.messages.create() which handles retries, error types, and API versioning automatically

## Phase 3: Features

Deliberately empty. Once the server is authenticated, tested and observable,
adding features stops being risky — fill this in then.

## Done

- [x] extract a _sonos_request(endpoint, timeout=5) helper method on DJServer with try/except, timeout, status code checking, and structured error returns. Replace all 15+ bare requests.get() calls to localhost:5005 in _do_play, _do_queue, _do_next, _do_pause, _do_resume, _do_skip, _do_previous, _do_volume, _do_nowplaying, _do_getqueue, _do_clearqueue
- [x] playlist building from the UI: add any search result to a playlist (or a
      new one), and pick playlists/stations from a dropdown when building a
      schedule step instead of pasting URIs
- [x] saved stations: Song Radio URIs 404 on the Web API but play via Sonos,
      and cannot be generated in-app since the recommendations endpoints were
      withdrawn -- so paste one once, name it, and reuse it
- [x] weekly calendar view of what is scheduled
