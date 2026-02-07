# DJ Assistant

A natural language DJ assistant that controls Spotify playback on Sonos speakers. Features a web UI, Claude AI integration, and CLI access.

**Live at: https://dj.guzzle.town/ui**

## Features

- 🎵 **Natural Language Control** - "play some jazz", "queue that Beatles song", "skip this"
- 🌐 **Web UI** - Mobile-friendly interface for party guests
- 🤖 **Claude AI** - Understands context and conversational requests
- 🔍 **Spotify Search** - Search tracks, albums, artists, playlists
- 📋 **Queue Management** - Add to queue, play next, clear queue, view queue
- 🎚️ **Playback Controls** - Play, pause, skip, previous, volume
- ❤️ **Library Access** - Browse playlists, liked songs
- 💻 **CLI** - Full command-line control via `dj` command

## Architecture

```
Internet (https://dj.guzzle.town)
            ↓
    Cloudflare Tunnel
            ↓
┌─────────────────────────────────────┐
│          Raspberry Pi               │
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

Visit **https://dj.guzzle.town/ui** (or http://lennypi.local:5006/ui on local network)

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

## Requirements

- Raspberry Pi (tested on Pi 4)
- Sonos speaker on your network
- Spotify Premium account
- Anthropic API key (for Claude integration)
- Cloudflare account (for public access)

## Installation

### 1. Install dependencies

```bash
sudo apt update
sudo apt install -y nodejs npm python3-pip

pip install spotipy cherrypy anthropic requests --break-system-packages
```

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
git clone https://github.com/YOUR_USERNAME/spotify-server.git
cd spotify-server
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
pip install spotipy
python auth.py  # Opens browser for OAuth
scp .cache pi@lennypi:~/spotify-server/
```

### 6. Set up services

**Sonos API:**
```bash
sudo nano /etc/systemd/system/sonos-api.service
```
```ini
[Unit]
Description=Sonos HTTP API
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/node-sonos-http-api
ExecStart=/usr/bin/node server.js
Restart=always

[Install]
WantedBy=multi-user.target
```

**DJ Server:**
```bash
sudo nano /etc/systemd/system/spotify-api.service
```
```ini
[Unit]
Description=DJ Server
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/spotify-server
ExecStart=/usr/bin/python /home/pi/spotify-server/server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable sonos-api spotify-api
sudo systemctl start sonos-api spotify-api
```

### 7. Set up CLI aliases

Add to `~/.bashrc`:
```bash
source ~/spotify-server/dj_aliases.sh
```

Reload:
```bash
source ~/.bashrc
```

### 8. Set up Cloudflare Tunnel (for public access)

1. Create account at https://cloudflare.com
2. Add your domain and update nameservers
3. Go to Zero Trust → Networks → Tunnels → Create tunnel
4. Install cloudflared on Pi:
```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o cloudflared
chmod +x cloudflared
sudo mv cloudflared /usr/local/bin/
```
5. Run the install command Cloudflare provides
6. Add public hostname: `dj.yourdomain.com` → `http://localhost:5006`

## Troubleshooting

### Check services
```bash
sudo systemctl status sonos-api
sudo systemctl status spotify-api
sudo systemctl status cloudflared
```

### View logs
```bash
journalctl -u spotify-api -f
journalctl -u cloudflared -f
```

### Restart services
```bash
sudo systemctl restart sonos-api spotify-api cloudflared
```

### Test locally
```bash
curl http://localhost:5006/nowplaying
curl "http://localhost:5006/search?q=beatles"
```

### Token expired
Re-run auth.py on your Mac and copy the new `.cache` file to the Pi.

## Project Structure

```
~/spotify-server/
├── server.py          # Main DJ server (port 5006)
├── config.json        # Spotify + Anthropic credentials
├── dj_aliases.sh      # CLI aliases
├── .cache             # Spotify OAuth token
└── README.md

~/node-sonos-http-api/
└── (Sonos control server, port 5005)
```

## License

MIT