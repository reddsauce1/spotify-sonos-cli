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

SONOS_URL = f"http://localhost:5005/{config['sonos_room']}"

# Store last search results
last_results = []


class SpotifyServer:
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def index(self):
        return {
            "status": "Spotify server running",
            "endpoints": [
                "/search?q=<query>&type=<track|album|artist|playlist>",
                "/my/playlists",
                "/my/liked",
                "/my/recent",
                "/my/top",
                "/play?num=<num>",
                "/queue?num=<num>",
                "/like",
                "/help"
            ]
        }
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def search(self, q=None, type="track", limit=5):
        global last_results
        
        if not q:
            return {"error": "No query provided. Use /search?q=your+search+terms"}
        
        results = sp.search(q=q, type=type, limit=int(limit))
        
        last_results = []
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
                last_results.append(item)
                output.append(item)
        
        elif type == "playlist":
            for i, playlist in enumerate(results['playlists']['items'], 1):
                item = {
                    "num": i,
                    "name": playlist['name'],
                    "owner": playlist['owner']['display_name'],
                    "tracks": playlist['tracks']['total'],
                    "uri": playlist['uri']
                }
                last_results.append(item)
                output.append(item)
        
        elif type == "album":
            for i, album in enumerate(results['albums']['items'], 1):
                item = {
                    "num": i,
                    "name": album['name'],
                    "artist": album['artists'][0]['name'],
                    "year": album['release_date'][:4] if album.get('release_date') else "Unknown",
                    "uri": album['uri']
                }
                last_results.append(item)
                output.append(item)
        
        elif type == "artist":
            for i, artist in enumerate(results['artists']['items'], 1):
                item = {
                    "num": i,
                    "name": artist['name'],
                    "genres": ", ".join(artist['genres'][:3]) if artist['genres'] else "Unknown",
                    "followers": artist['followers']['total'],
                    "uri": artist['uri']
                }
                last_results.append(item)
                output.append(item)
        
        return {"query": q, "type": type, "results": output}
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def tracks(self, num=None, uri=None):
        global last_results
        
        # Get album URI from last results or direct
        if num and not uri:
            num = int(num)
            if num < 1 or num > len(last_results):
                return {"error": f"Invalid selection. Choose 1-{len(last_results)}"}
            item = last_results[num - 1]
            uri = item['uri']
        
        if not uri:
            return {"error": "Provide album num from search results or uri"}
        
        # Get album ID from URI
        album_id = uri.split(':')[-1]
        
        # Fetch album tracks
        album = sp.album(album_id)
        tracks = sp.album_tracks(album_id)
        
        last_results = []
        output = []
        
        for i, track in enumerate(tracks['items'], 1):
            entry = {
                "num": i,
                "name": track['name'],
                "duration": f"{track['duration_ms'] // 60000}:{(track['duration_ms'] // 1000) % 60:02d}",
                "uri": track['uri']
            }
            last_results.append(entry)
            output.append(entry)
        
        return {
            "album": album['name'],
            "artist": album['artists'][0]['name'],
            "tracks": output
        }
        
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def my(self, action=None, limit=20, offset=0):
        global last_results
        
        limit = int(limit)
        offset = int(offset)
        
        if action == "playlists":
            results = sp.current_user_playlists(limit=limit, offset=offset)
            last_results = []
            output = []
            
            for i, playlist in enumerate(results['items'], 1):
                item = {
                    "num": i + offset,
                    "name": playlist['name'],
                    "tracks": playlist['tracks']['total'],
                    "uri": playlist['uri']
                }
                last_results.append(item)
                output.append(item)
            
            return {
                "your_playlists": output,
                "total": results['total'],
                "showing": f"{offset + 1}-{offset + len(output)}",
                "has_more": offset + len(output) < results['total']
            }
        
        elif action == "liked":
            results = sp.current_user_saved_tracks(limit=limit, offset=offset)
            last_results = []
            output = []
            
            for i, item in enumerate(results['items'], 1):
                track = item['track']
                entry = {
                    "num": i + offset,
                    "name": track['name'],
                    "artist": track['artists'][0]['name'],
                    "album": track['album']['name'],
                    "uri": track['uri']
                }
                last_results.append(entry)
                output.append(entry)
            
            return {
                "your_liked_songs": output,
                "total": results['total'],
                "showing": f"{offset + 1}-{offset + len(output)}",
                "has_more": offset + len(output) < results['total']
            }
        
        elif action == "recent":
            results = sp.current_user_recently_played(limit=limit)
            last_results = []
            output = []
            
            for i, item in enumerate(results['items'], 1):
                track = item['track']
                entry = {
                    "num": i,
                    "name": track['name'],
                    "artist": track['artists'][0]['name'],
                    "uri": track['uri']
                }
                last_results.append(entry)
                output.append(entry)
            
            return {"recently_played": output}
        
        elif action == "top":
            results = sp.current_user_top_tracks(limit=limit, offset=offset)
            last_results = []
            output = []
            
            for i, track in enumerate(results['items'], 1):
                entry = {
                    "num": i + offset,
                    "name": track['name'],
                    "artist": track['artists'][0]['name'],
                    "uri": track['uri']
                }
                last_results.append(entry)
                output.append(entry)
            
            return {
                "your_top_tracks": output,
                "total": results['total'],
                "showing": f"{offset + 1}-{offset + len(output)}",
                "has_more": offset + len(output) < results['total']
            }
        
        return {
            "error": "Unknown action",
            "available": ["/my/playlists", "/my/liked", "/my/recent", "/my/top"]
        }
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def artist(self, id=None, action=None, num=None):
        global last_results
        
        if num and not id:
            num = int(num)
            if num < 1 or num > len(last_results):
                return {"error": f"Invalid selection. Choose 1-{len(last_results)}"}
            item = last_results[num - 1]
            id = item['uri'].split(':')[-1]
        
        if not id:
            return {"error": "Provide artist id or num from search results"}
        
        if action == "top":
            results = sp.artist_top_tracks(id)
            last_results = []
            output = []
            
            for i, track in enumerate(results['tracks'], 1):
                entry = {
                    "num": i,
                    "name": track['name'],
                    "album": track['album']['name'],
                    "uri": track['uri']
                }
                last_results.append(entry)
                output.append(entry)
            
            return {"top_tracks": output}
        
        elif action == "albums":
            results = sp.artist_albums(id, album_type='album', limit=10)
            last_results = []
            output = []
            
            for i, album in enumerate(results['items'], 1):
                entry = {
                    "num": i,
                    "name": album['name'],
                    "year": album['release_date'][:4] if album.get('release_date') else "Unknown",
                    "uri": album['uri']
                }
                last_results.append(entry)
                output.append(entry)
            
            return {"albums": output}
        
        return {
            "error": "Unknown action",
            "available": ["/artist/<num>/top", "/artist/<num>/albums"]
        }
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def play(self, num=None, uri=None):
        global last_results
        
        if uri:
            requests.get(f"{SONOS_URL}/spotify/now/{uri}")
            return {"status": "playing", "uri": uri}
        
        if num:
            num = int(num)
            if num < 1 or num > len(last_results):
                return {"error": f"Invalid selection. Choose 1-{len(last_results)}"}
            
            item = last_results[num - 1]
            requests.get(f"{SONOS_URL}/spotify/now/{item['uri']}")
            return {"status": "playing", "item": item}
        
        return {"error": "Provide num (from search results) or uri"}
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def queue(self, num=None, uri=None):
        global last_results
        
        if uri:
            requests.get(f"{SONOS_URL}/spotify/queue/{uri}")
            return {"status": "queued", "uri": uri}
        
        if num:
            num = int(num)
            if num < 1 or num > len(last_results):
                return {"error": f"Invalid selection. Choose 1-{len(last_results)}"}
            
            item = last_results[num - 1]
            requests.get(f"{SONOS_URL}/spotify/queue/{item['uri']}")
            return {"status": "queued", "item": item}
        
        return {"error": "Provide num (from search results) or uri"}
    
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
            "search": {
                "/search?q=<query>": "Search tracks (default)",
                "/search?q=<query>&type=album": "Search albums",
                "/search?q=<query>&type=artist": "Search artists",
                "/search?q=<query>&type=playlist": "Search playlists"
            },
            "your_library": {
                "/my/playlists": "Your playlists",
                "/my/liked": "Your liked songs",
                "/my/recent": "Recently played",
                "/my/top": "Your top tracks"
            },
            "artist_info": {
                "/artist/<num>/top": "Top tracks for artist from search",
                "/artist/<num>/albums": "Albums for artist from search"
            },
            "playback": {
                "/play?num=<num>": "Play result number",
                "/play?uri=<spotify:uri>": "Play URI directly",
                "/queue?num=<num>": "Add to queue",
                "/like": "Like current track"
            }
        }


if __name__ == '__main__':
    cherrypy.config.update({
        'server.socket_host': '0.0.0.0',
        'server.socket_port': 5006
    })
    cherrypy.quickstart(SpotifyServer())