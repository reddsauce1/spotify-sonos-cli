# Spotify-Server Hardening Tasks

## Phase 1: Safety Net
- [x] extract a _sonos_request(endpoint, timeout=5) helper method on DJServer with try/except, timeout, status code checking, and structured error returns. Replace all 15+ bare requests.get() calls to localhost:5005 in _do_play, _do_queue, _do_next, _do_pause, _do_resume, _do_skip, _do_previous, _do_volume, _do_nowplaying, _do_getqueue, _do_clearqueue
- [ ] wrap all sp.*() Spotify API calls in try/except for SpotifyException and SpotifyOauthError, return JSON error responses instead of crashing. Affects _do_search, my, like, create_playlist, add_to_playlist, recommend, album_tracks
- [ ] fix Claude API response parsing in call_claude(): validate response structure before indexing result['content'][0]['text'], handle non-JSON Claude output gracefully, add retry on 429 rate limit
- [ ] extract _get_result_item(self, num, session_id) helper to validate int conversion and bounds checking, replace the 4 duplicated blocks in _do_play, _do_queue, _do_next, and add_to_playlist
- [ ] extract _parse_track_id(self, uri) helper to parse Spotify track ID from URI, replace the 3 duplicated blocks in like, recommend, and album_tracks

## Phase 2: Security
- [ ] add a _require_auth(self) method or CherryPy before_handler tool that checks the dj_auth cookie. Apply to all exposed API endpoints: search, play, queue, next, pause, resume, skip, previous, volume, nowplaying, getqueue, clearqueue, my, like, help, create_playlist, add_to_playlist, recommend, album_tracks. Return 401 JSON error if not authenticated
- [ ] replace plaintext password in cookie: generate a random session token on login with secrets.token_hex(), store in a server-side set, put the token (not the password) in the cookie. Validate by checking token membership
- [ ] add input validation: wrap int(num) in try/except ValueError in all endpoints that accept num parameter, validate Spotify URI format (must start with spotify:) before passing to Sonos, return proper 400 JSON errors

## Phase 3: Structure
- [ ] extract the ~500-line HTML/CSS/JS block from the ui() method (lines 222-657) into a new file static/index.html. Configure CherryPy to serve static files from the static/ directory. The ui() method should check auth then serve the file
- [ ] consolidate all hardcoded magic numbers into a DEFAULTS dict at top of server.py (claude_timeout, claude_max_tokens, claude_model, search_limit, queue_display_limit, cookie_max_age, server_port). Load overrides from config.json. Use these values everywhere instead of literals
- [ ] add config.json validation on startup: check that client_id, client_secret exist and are non-empty strings. Print clear error message and exit(1) if validation fails. Make anthropic_api_key, sonos_room, ui_password optional with defaults
- [ ] add session expiry to search_results: store (timestamp, results) tuples, add a cleanup function that removes entries older than 1 hour, call cleanup on every new search, cap total sessions at 100
- [ ] replace all print() calls with Python logging module. Create a logger with structured format including timestamp and level. Configure for systemd journal output

## Phase 4: Polish
- [ ] add an escapeHtml(str) JavaScript function to the web UI that escapes &<>"' characters. Replace all innerHTML assignments that insert dynamic data (track names, artist names, album names, artwork URLs) with either textContent or escaped innerHTML
- [ ] return proper HTTP status codes: 400 for bad input (missing params, invalid num), 401 for unauthenticated requests, 502 for Sonos/Spotify upstream failures. Use cherrypy.response.status to set codes before returning JSON
- [ ] add a /health endpoint that checks Sonos connectivity (GET to localhost:5005/zones with 3s timeout), checks Spotify token validity (sp.me() in try/except), and returns JSON {sonos: ok/error, spotify: ok/error, uptime_seconds: N}
- [ ] replace the raw requests.post() call to Anthropic API in call_claude() with the anthropic Python SDK (import anthropic, client = anthropic.Anthropic()). Use client.messages.create() which handles retries, error types, and API versioning automatically
