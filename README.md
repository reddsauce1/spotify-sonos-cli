# DJ Assistant

A natural language DJ assistant that controls Spotify playback on Sonos speakers. Features a web UI, Claude AI integration, and CLI access.


## Features

- 🎵 **Natural Language Control** - "play some jazz", "queue that Beatles song", "skip this"
- 🌐 **Web UI** - Mobile-friendly interface for party guests
- 📚 **Browse Playlists** - View and play your Spotify playlists
- 💿 **Album View** - See full album with artwork when a song plays
- 🎤 **Artist Top Tracks** - Discover more from the current artist
- ➕ **Create Playlists** - Create new playlists and add songs
- 🤖 **Claude AI** - Understands context and conversational requests
- 🔍 **Spotify Search** - Search tracks, albums, artists, playlists
- 📋 **Queue Management** - Add to queue, play next, clear queue, view queue
- 🎚️ **Playback Controls** - Play, pause, skip, previous, volume
- ❤️ **Library Access** - Browse playlists, liked songs
- ⏰ **Schedules** - Wake up to music; any action on a recurring time
- 💻 **CLI** - Full command-line control via `dj` command

## Architecture

```
Internet (url hidden)
            ↓
    Cloudflare Tunnel
            ↓
┌─────────────────────────────────────┐
│      Host (macOS, via launchd)      │
├─────────────────────────────────────┤
│  server.py (port 5006)              │
│  ├── Web UI (/ui)                   │
│  ├── Chat endpoint (/chat)          │
│  ├── Claude AI integration          │
│  └── Spotify API                    │
│            ↓                        │
│  node-sonos-http-api (port 5005)    │
│            ↓                        │
│      Sonos Speakers                 │
└─────────────────────────────────────┘
```

## Web UI


- Type natural language requests: "play something chill", "add number 3 to queue"
- Click search results to queue them
- Quick buttons for pause, play, skip, volume, etc.
- Shows currently playing track

## CLI Usage

```bash
# Search
dj search bohemian rhapsody
dj search beatles

# Play from search results
dj play 1                    # play result #1 immediately
dj queue 2                   # add #2 to end of queue (alias: dj q)
dj next 3                    # play #3 after current song (alias: dj n)

# Playback controls
dj pause
dj resume                    # alias: dj r
dj skip                      # alias: dj s
dj prev

# Volume
dj vol                       # show current volume
dj vol 50                    # set to 50
dj vol up                    # +10
dj vol down                  # -10

# Queue management
dj np                        # now playing
dj showqueue                 # view queue (alias: dj sq)
dj clear                     # clear queue

# Library
dj like                      # like current song
dj playlists                 # your playlists
dj liked                     # your liked songs

# Help
dj help
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `/health` | Sonos + Spotify reachability; 503 if either is down |
| `/schedules` | List scheduled actions |
| `/schedule_add` | Create one (POST) |
| `/schedule_delete` | Remove by id (POST) |
| `/schedule_toggle` | Enable/disable by id (POST) |
| `/schedule_run` | Run one immediately, for testing (POST) |
| `/ui` | Web interface |
| `/chat?message=<text>` | Natural language (Claude AI) |
| `/search?q=<query>` | Search Spotify |
| `/play?num=<n>` | Play search result |
| `/queue?num=<n>` | Add to end of queue |
| `/next?num=<n>` | Add to play next |
| `/pause` | Pause playback |
| `/resume` | Resume playback |
| `/skip` | Skip track |
| `/previous` | Previous track |
| `/volume?level=<0-100>` | Set volume |
| `/volume?change=<+/-10>` | Adjust volume |
| `/nowplaying` | Current track info |
| `/getqueue` | View queue |
| `/clearqueue` | Clear queue |
| `/my/playlists` | Your playlists |
| `/my/liked` | Your liked songs |
| `/like` | Like current track |
| `/recommend?based_on=nowplaying` | Top tracks from current artist |
| `/album_tracks?based_on=nowplaying` | Album tracks for current song |
| `/create_playlist?name=<name>` | Create new playlist |
| `/add_to_playlist?playlist_id=<id>&num=<n>` | Add track to playlist |

## Requirements

- A machine that stays on: macOS (launchd, described here) or Linux (systemd)
- Python 3.11+ and Node.js
- Sonos speaker on your network
- Spotify Premium account
- Anthropic API key (for Claude integration)
- Cloudflare account (for public access)

## Installation

### 1. Install dependencies

macOS:

```bash
brew install node python jq
python3 -m venv venv
./venv/bin/pip install spotipy cherrypy anthropic requests
```

Debian/Raspberry Pi:

```bash
sudo apt update && sudo apt install -y nodejs npm python3-venv jq
python3 -m venv venv
./venv/bin/pip install spotipy cherrypy anthropic requests
```

`jq` is needed by `dj_aliases.sh`.

### 2. Install node-sonos-http-api

```bash
cd ~
git clone https://github.com/jishi/node-sonos-http-api.git
cd node-sonos-http-api
npm install
```

### 3. Clone this repo

```bash
cd ~
git clone https://github.com/reddsauce1/spotify-sonos-cli.git
cd spotify-sonos-cli
```

### 4. Configure

```bash
cp config.example.json config.json
nano config.json
```

```json
{
    "client_id": "YOUR_SPOTIFY_CLIENT_ID",
    "client_secret": "YOUR_SPOTIFY_CLIENT_SECRET",
    "sonos_room": "Dining%20Room",
    "anthropic_api_key": "YOUR_ANTHROPIC_API_KEY"
}
```

- Get Spotify credentials at https://developer.spotify.com/dashboard
- Get Anthropic API key at https://console.anthropic.com
- Find your Sonos room name: `curl http://localhost:5005/zones`

### 5. Authenticate with Spotify

First-time auth requires a browser. On your Mac/PC:

```bash
./venv/bin/python auth.py   # opens a browser, writes .cache
```

Run this on the machine that will host the server. If you authenticate
elsewhere, copy `.cache` across (`scp .cache user@host:~/spotify-server/`).

`.cache` holds a long-lived refresh token in plaintext — treat it like a
password. To **rotate** it, revoke the app first at
https://www.spotify.com/account/apps/ and then re-run `auth.py`; re-running
alone issues a new token but leaves the old one valid.

### 6. Set up services

Two services need to stay running: `node-sonos-http-api` (port 5005) and this
server (port 5006).

**macOS (launchd)** — create `~/Library/LaunchAgents/com.you.spotify-server.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.you.spotify-server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/full/path/to/spotify-server/venv/bin/python3</string>
        <string>server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/full/path/to/spotify-server</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/full/path/to/spotify-server/logs/spotify-server.log</string>
    <key>StandardErrorPath</key>
    <string>/full/path/to/spotify-server/logs/spotify-server.error.log</string>
</dict>
</plist>
```

Make an equivalent plist for node-sonos-http-api, then:

```bash
mkdir -p logs
launchctl load ~/Library/LaunchAgents/com.you.spotify-server.plist
```

`KeepAlive` restarts the process if it exits, so `kill` is a safe way to
restart it. To apply code changes:

```bash
launchctl kickstart -k gui/$(id -u)/com.you.spotify-server
```

**Linux (systemd):**

```ini
# /etc/systemd/system/spotify-api.service
[Unit]
Description=DJ Server
After=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/spotify-server
ExecStart=/home/YOUR_USER/spotify-server/venv/bin/python server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sonos-api spotify-api
```

On startup the server logs one line stating what it believes — room, model,
and whether auth, the CLI token and Claude are configured:

```
01/Aug/2026:16:21:58 INFO    dj: Starting DJ server: room=Dining%20Room model=claude-sonnet-5 auth=on cli_token=set claude=configured
```

### 7. Set up CLI aliases

Add to your shell's rc file (`~/.zshrc` on modern macOS, `~/.bashrc` on Linux):

```bash
source ~/spotify-server/dj_aliases.sh
```

Then reload it (`source ~/.zshrc`). The aliases read `cli_token` from
`config.json` at load time, so re-source after rotating it.

### 8. Set up Cloudflare Tunnel (for public access)

1. Create account at https://cloudflare.com
2. Add your domain and update nameservers
3. Go to Zero Trust → Networks → Tunnels → Create tunnel
4. Install cloudflared:
```bash
brew install cloudflared                       # macOS
# Linux/Pi: download the build matching your architecture from
# https://github.com/cloudflare/cloudflared/releases/latest
```
5. Run the install command Cloudflare provides
6. Add public hostname: `dj.yourdomain.com` → `http://localhost:5006`

## Troubleshooting

### Check services

```bash
launchctl list | grep -i 'spotify\|sonos\|cloudflare'   # macOS
sudo systemctl status sonos-api spotify-api cloudflared  # Linux
```

Or ask the server itself — this reports whether the upstreams are actually
reachable, and returns 503 if either is down:

```bash
curl -H "X-DJ-Token: $(jq -r .cli_token config.json)" http://localhost:5006/health
# {"sonos": "ok", "spotify": "ok", "uptime_seconds": 1042}
```

### View logs

```bash
tail -f logs/spotify-server.log          # macOS: launchd redirects here
journalctl -u spotify-api -f             # Linux
```

Application messages are prefixed `dj:` and interleave with the request log:

```bash
grep ' dj: ' logs/spotify-server.log            # app events only
grep -E 'WARNING|ERROR' logs/spotify-server.log # problems only
```

### Restart services

```bash
launchctl kickstart -k gui/$(id -u)/com.you.spotify-server   # macOS
sudo systemctl restart sonos-api spotify-api cloudflared     # Linux
```

### Test locally

Every endpoint except `/`, `/ui` and `/login` needs credentials:

```bash
TOKEN=$(jq -r .cli_token config.json)
curl -H "X-DJ-Token: $TOKEN" http://localhost:5006/nowplaying
curl -H "X-DJ-Token: $TOKEN" "http://localhost:5006/search?q=beatles"
```

A bare `curl http://localhost:5006/nowplaying` returns
`{"error": "Authentication required"}` — that is expected, not a fault.

### `dj` commands return 401

The CLI reads `cli_token` from `config.json` when `dj_aliases.sh` is sourced.
After changing it, re-source the file (or open a new shell). If the server was
restarted but the token was only just added to `config.json`, restart the
server too.

### Token expired
Re-run auth.py on your Mac and copy the new `.cache` file to the Pi.

## Development

### Running tests

```bash
pip install pytest
python -m pytest -q
```

The suite mocks Spotify, Sonos and CherryPy, so it needs no network, no
credentials and no running server. `conftest.py` feeds `server.py` a fake
config at import time.

## Authentication

Every API endpoint requires credentials; only `/`, `/ui` and `/login` are
public. Set `ui_password` in `config.json` to enable this — if it is empty the
server runs completely open.

There are two ways to authenticate:

- **Browser** — `/login` exchanges the password for a random session token
  stored server-side and set as an httponly cookie. Restarting the server
  invalidates all sessions.
- **CLI / scripts** — send the `cli_token` from `config.json` as an
  `X-DJ-Token` header. `dj_aliases.sh` does this for you.

Note that auth is *not* based on source IP. `cloudflared` connects over
loopback, so tunnelled internet traffic is indistinguishable from a local
request by address alone — exempting localhost would expose everything.

## Schedules

Open **⏰ Scheduled actions** in the web UI to run any DJ action on a recurring
time — most usefully waking up to a playlist.

Each entry has a time (24-hour, local), the days it applies to (none selected
means every day), an action, and optionally a volume. For `play`, **the volume
is set before playback starts**, so a morning alarm cannot blast at whatever
level last night ended on. The ⚡ button runs an entry immediately, so you can
check a playlist URI works without waiting until 7am.

Schedules persist to `schedules.json` and survive restarts. A CherryPy Monitor
checks every 20 seconds and matches on the exact minute, which means a server
that was down at 07:00 and comes back at 09:30 will **not** fire the alarm late.

Times are local wall-clock, so the DST consequences are the usual ones: an
entry inside the skipped hour on the spring-forward day does not fire, and on
fall-back the repeated hour cannot fire it twice.

To get a playlist URI: right-click a playlist in Spotify → Share → Copy Spotify
URI, or use `dj playlists` and take the `uri` field.

## Project Structure

```
spotify-server/
├── server.py             # DJ server (port 5006)
├── auth.py               # One-off Spotify OAuth flow -> .cache
├── static/index.html     # Web UI markup, CSS and JS
├── dj_aliases.sh         # `dj` CLI (needs jq)
├── config.json           # Credentials + settings   (gitignored)
├── .cache                # Spotify OAuth token      (gitignored)
├── schedules.json        # Scheduled actions        (gitignored)
├── logs/                 # launchd stdout/stderr    (gitignored)
├── conftest.py           # Test fixtures; mocks Spotify and config
└── test_*.py             # Test suite -- no network or credentials needed

~/node-sonos-http-api/
└── (Sonos control server, port 5005)
```

Settings live in `DEFAULTS` at the top of `server.py` (timeouts, limits, model,
port) and any of them can be overridden by adding the same key to
`config.json`.

## License

MIT