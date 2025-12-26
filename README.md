# Spotify Sonos CLI

Control Spotify playback on Sonos speakers from the command line using a Raspberry Pi.

## Features

- Search tracks, albums, artists, playlists
- Browse your library (playlists, liked songs, recently played, top tracks)
- Play music on Sonos speakers
- Like the currently playing track
- View album track listings
- Scheduled playback via cron
- Full Sonos control (volume, shuffle, pause, etc.)

## Architecture
```
Terminal Commands
       ↓
spotify-server (port 5006) ←→ Spotify API
       ↓
node-sonos-http-api (port 5005)
       ↓
Sonos Speakers
```

## Requirements

- Raspberry Pi (or any Linux machine)
- Sonos speaker on your network
- Spotify Premium account
- Node.js

## Setup

### 1. Install Node.js
```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt-get install -y nodejs
```

### 2. Install node-sonos-http-api
```bash
cd ~
git clone https://github.com/jishi/node-sonos-http-api.git
cd node-sonos-http-api
npm install
```

Test it:
```bash
npm start
```

You should see it discover your Sonos speakers.

### 3. Set up Sonos API as a service
```bash
sudo nano /etc/systemd/system/sonos-api.service
```
```
[Unit]
Description=Sonos HTTP API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/node-sonos-http-api
ExecStart=/usr/bin/node server.js
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable sonos-api
sudo systemctl start sonos-api
```

### 4. Install Spotify server
```bash
cd ~
git clone https://github.com/YOUR_USERNAME/spotify-sonos-cli.git
cd spotify-sonos-cli
pip install spotipy cherrypy --break-system-packages
```

### 5. Configure Spotify credentials

Create a Spotify app at https://developer.spotify.com/dashboard

Add `http://127.0.0.1:8888/callback` as a redirect URI.
```bash
cp config.example.json config.json
nano config.json
```
```json
{
    "client_id": "YOUR_SPOTIFY_CLIENT_ID",
    "client_secret": "YOUR_SPOTIFY_CLIENT_SECRET",
    "sonos_room": "Dining%20Room"
}
```

Replace `Dining%20Room` with your Sonos speaker name (use `%20` for spaces).

### 6. Authenticate with Spotify

First-time auth must be done on a machine with a browser.

On your Mac/PC:
```bash
pip install spotipy
```

Create `auth.py`:
```python
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import os

if os.path.exists(".cache"):
    os.remove(".cache")

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id="YOUR_CLIENT_ID",
    client_secret="YOUR_CLIENT_SECRET",
    redirect_uri="http://127.0.0.1:8888/callback",
    scope="user-library-read user-library-modify playlist-read-private playlist-modify-public playlist-modify-private user-read-recently-played user-top-read",
    cache_path=".cache"
))

print(f"Logged in as: {sp.current_user()['display_name']}")
```

Run it, authorize in browser, then copy the `.cache` file to your Pi:
```bash
scp .cache pi@your-pi-hostname:~/spotify-sonos-cli/
```

### 7. Set up Spotify server as a service
```bash
sudo nano /etc/systemd/system/spotify-api.service
```
```
[Unit]
Description=Spotify API Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/spotify-sonos-cli
ExecStart=/usr/bin/python /home/pi/spotify-sonos-cli/server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable spotify-api
sudo systemctl start spotify-api
```

### 8. Set up shell aliases

Add to your `~/.bashrc`:
```bash
source ~/spotify-sonos-cli/aliases.sh
```

Reload:
```bash
source ~/.bashrc
```

## Usage

### Spotify Commands (sp)
```bash
# Search
sp search bohemian rhapsody    # search tracks
sp album abbey road            # search albums
sp artist beatles              # search artists
sp playlist chill vibes        # search playlists

# Album tracks
sp tracks 1                    # show tracks for album #1 from search

# Playback
sp play 3                      # play result #3
sp queue 2                     # add result #2 to queue

# Your library
sp my playlists                # your playlists
sp my playlists 20             # page 2 (offset 20)
sp my liked                    # liked songs
sp my liked 20                 # page 2
sp my recent                   # recently played
sp my top                      # your top tracks

# Other
sp like                        # like current song
sp help                        # show all commands
```

### Sonos Commands (sonos)
```bash
# Playback
sonos play
sonos pause
sonos next
sonos previous

# Volume
sonos volume/30                # set to 30
sonos volume/+10               # increase by 10
sonos volume/-10               # decrease by 10
sonos mute
sonos unmute

# Play modes
sonos shuffle/on
sonos shuffle/off
sonos repeat/all
sonos repeat/one
sonos repeat/none

# Info
sonos state                    # current playback info
sonos help                     # show all commands
```

### Now Playing
```bash
nowplaying                     # show current track info
```

## Scheduled Playback

Use cron to schedule music:
```bash
crontab -e
```

Example - play a playlist at 6:20am every day:
```
20 6 * * * curl "http://localhost:5005/Dining\%20Room/shuffle/on"
20 6 * * * sleep 2 && curl "http://localhost:5005/Dining\%20Room/volume/20"
20 6 * * * sleep 4 && curl "http://localhost:5005/Dining\%20Room/spotify/now/spotify:playlist:YOUR_PLAYLIST_ID"
15 8 * * * curl "http://localhost:5005/Dining\%20Room/pause"
```

Note: Escape `%` as `\%` in crontab.

## Troubleshooting

### Check if services are running
```bash
sudo systemctl status sonos-api
sudo systemctl status spotify-api
```

### View logs
```bash
journalctl -u sonos-api | tail -30
journalctl -u spotify-api | tail -30
```

### Restart services
```bash
sudo systemctl restart sonos-api
sudo systemctl restart spotify-api
```

### Token expired
If you get authentication errors, re-run the auth script on your Mac and copy the new `.cache` file to the Pi.

### Find your Sonos speaker name
```bash
curl "http://localhost:5005/zones"
```

## Uninstall
```bash
# Stop and remove services
sudo systemctl stop sonos-api spotify-api
sudo systemctl disable sonos-api spotify-api
sudo rm /etc/systemd/system/sonos-api.service
sudo rm /etc/systemd/system/spotify-api.service
sudo systemctl daemon-reload

# Remove cron jobs
crontab -e  # delete the lines

# Remove projects
rm -rf ~/node-sonos-http-api
rm -rf ~/spotify-sonos-cli

# Remove aliases from ~/.bashrc
```

## License

MIT
