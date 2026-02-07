import cherrypy
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import requests
import json
import os
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
SONOS_URL = f"http://localhost:5005/{SONOS_ROOM}"

# Claude setup
ANTHROPIC_API_KEY = config.get('anthropic_api_key', '')

# Store last search results (per session for web, global for CLI)
search_results = {'global': []}


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

    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01"
    }
    
    data = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 300,
        "system": system_prompt,
        "messages": [{"role": "user", "content": message}]
    }
    
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=data,
            timeout=15
        )
        result = response.json()
        text = result['content'][0]['text']
        return json.loads(text)
    except Exception as e:
        print(f"Claude error: {e}")
        return {"action": "chat", "message": "Sorry, I had trouble understanding that. Try again!"}


class DJServer:

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
            cursor: pointer;
            display: flex;
            gap: 10px;
        }
        .result-item:hover { background: rgba(255,255,255,0.1); }
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
        .result-info { flex: 1; }
        .song-name { font-weight: bold; margin-bottom: 3px; }
        .artist-name { color: #888; font-size: 13px; }
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
        <button class="quick-btn" onclick="refreshNowPlaying()">
            <span class="icon">🔄</span>
            <span>Refresh</span>
        </button>
    </div>
    
    <div class="status" id="status">🎧 What do you want to hear?</div>
    
    <div class="results" id="results"></div>

    <script>
        // Refresh now playing on load and every 30 seconds
        refreshNowPlaying();
        setInterval(refreshNowPlaying, 30000);
        
        function refreshNowPlaying() {
            fetch('/nowplaying')
                .then(r => r.json())
                .then(data => {
                    const el = document.getElementById('nowplaying-text');
                    if (data.title && data.title !== 'Nothing playing') {
                        el.innerHTML = '<strong>' + data.title + '</strong> by ' + data.artist;
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
                '<div class="result-item" onclick="queueTrack(' + item.num + ')">' +
                '<div class="result-num">' + item.num + '</div>' +
                '<div class="result-info">' +
                '<div class="song-name">' + item.name + '</div>' +
                '<div class="artist-name">' + item.artist + (item.album ? ' • ' + item.album : '') + '</div>' +
                '</div></div>'
            ).join('');
            document.getElementById('results').innerHTML = html;
        }
        
        function queueTrack(num) {
            document.getElementById('status').innerHTML = '➕ Adding to queue...';
            fetch('/queue?num=' + num)
                .then(r => r.json())
                .then(data => {
                    if (data.item) {
                        document.getElementById('status').innerHTML = '✓ Queued: ' + data.item.name;
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
                        document.getElementById('status').innerHTML = '✓ ' + (data.status || 'Done');
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
    </script>
</body>
</html>'''

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

    def _do_search(self, q, type="track", limit=5, session_id='global'):
        results = sp.search(q=q, type=type, limit=int(limit))
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
            requests.get(f"{SONOS_URL}/spotify/now/{uri}")
            return {"status": "playing", "uri": uri}

        if num:
            num = int(num)
            results = get_results(session_id)
            if num < 1 or num > len(results):
                return {"error": f"Invalid selection. Choose 1-{len(results)}"}
            item = results[num - 1]
            requests.get(f"{SONOS_URL}/spotify/now/{item['uri']}")
            return {"status": "playing", "item": item}

        return {"error": "Provide num or uri"}

    def _do_queue(self, num=None, uri=None, session_id='global'):
        if uri:
            requests.get(f"{SONOS_URL}/spotify/queue/{uri}")
            return {"status": "queued", "uri": uri}

        if num:
            num = int(num)
            results = get_results(session_id)
            if num < 1 or num > len(results):
                return {"error": f"Invalid selection. Choose 1-{len(results)}"}
            item = results[num - 1]
            requests.get(f"{SONOS_URL}/spotify/queue/{item['uri']}")
            return {"status": "queued", "item": item}

        return {"error": "Provide num or uri"}

    def _do_next(self, num=None, uri=None, session_id='global'):
        if uri:
            requests.get(f"{SONOS_URL}/spotify/next/{uri}")
            return {"status": "playing next", "uri": uri}

        if num:
            num = int(num)
            results = get_results(session_id)
            if num < 1 or num > len(results):
                return {"error": f"Invalid selection. Choose 1-{len(results)}"}
            item = results[num - 1]
            requests.get(f"{SONOS_URL}/spotify/next/{item['uri']}")
            return {"status": "playing next", "item": item}

        return {"error": "Provide num or uri"}

    def _do_pause(self):
        requests.get(f"{SONOS_URL}/pause")
        return {"status": "paused"}

    def _do_resume(self):
        requests.get(f"{SONOS_URL}/play")
        return {"status": "playing"}

    def _do_skip(self):
        requests.get(f"{SONOS_URL}/next")
        return {"status": "skipped"}

    def _do_previous(self):
        requests.get(f"{SONOS_URL}/previous")
        return {"status": "previous"}

    def _do_volume(self, level=None, change=None):
        if level:
            requests.get(f"{SONOS_URL}/volume/{level}")
            return {"status": "volume set", "level": level}
        elif change:
            requests.get(f"{SONOS_URL}/volume/{change}")
            return {"status": "volume adjusted", "change": change}
        else:
            state = requests.get(f"{SONOS_URL}/state").json()
            return {"volume": state.get('volume', 'unknown')}

    def _do_nowplaying(self):
        state = requests.get(f"{SONOS_URL}/state").json()
        track = state.get('currentTrack', {})
        return {
            "title": track.get('title', 'Nothing playing'),
            "artist": track.get('artist', ''),
            "album": track.get('album', ''),
            "volume": state.get('volume', 0),
            "playbackState": state.get('playbackState', 'unknown')
        }

    def _do_getqueue(self):
        response = requests.get(f"{SONOS_URL}/queue")
        return {"queue": response.json()}

    def _do_clearqueue(self):
        requests.get(f"{SONOS_URL}/clearqueue")
        return {"status": "queue cleared"}

    # ==================== PUBLIC API ENDPOINTS ====================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def search(self, q=None, type="track", limit=5):
        if not q:
            return {"error": "No query provided. Use /search?q=your+search+terms"}
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
    def my(self, action=None, limit=20, offset=0):
        limit = int(limit)
        offset = int(offset)

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
    def like(self):
        state = requests.get(f"{SONOS_URL}/state").json()
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


if __name__ == '__main__':
    cherrypy.config.update({
        'server.socket_host': '0.0.0.0',
        'server.socket_port': 5006
    })
    cherrypy.quickstart(DJServer())