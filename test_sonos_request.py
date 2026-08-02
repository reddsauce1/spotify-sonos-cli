"""Tests for DJServer._sonos_request helper and all refactored Sonos methods.

server.py is imported by conftest.py with spotipy and the config file already
mocked, so this module just uses that instance. It used to install its own
cherrypy/spotipy stubs into sys.modules, which leaked into every test module
collected after it -- exception classes imported elsewhere no longer matched
the ones server.py had bound, so `except SpotifyException` silently stopped
matching.
"""

import unittest
from unittest.mock import MagicMock, patch

import requests

import server

# Grab a DJServer instance for testing
dj = server.DJServer()

# Resolve against the cherrypy that server.py is actually bound to, so these
# assertions cannot drift from the class the code raises.
BadRequest = server.cherrypy.HTTPError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code=200, json_data=None, text="OK", raises_json=False):
    """Create a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if raises_json:
        resp.json.side_effect = ValueError("No JSON")
    elif json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("No JSON")
    return resp


# ===================================================================
# Tests for _sonos_request
# ===================================================================

class TestSonosRequest(unittest.TestCase):
    """Tests for the _sonos_request helper method."""

    @patch("server.requests.get")
    def test_success_with_json(self, mock_get):
        mock_get.return_value = _mock_response(200, json_data={"volume": 42})
        result = dj._sonos_request("state")
        self.assertEqual(result, {"volume": 42})
        mock_get.assert_called_once()
        # Verify URL construction
        url_called = mock_get.call_args[0][0]
        self.assertIn("/state", url_called)

    @patch("server.requests.get")
    def test_success_no_json_body(self, mock_get):
        mock_get.return_value = _mock_response(200, raises_json=True)
        result = dj._sonos_request("pause")
        self.assertEqual(result, {"ok": True})

    @patch("server.requests.get")
    def test_timeout_error(self, mock_get):
        mock_get.side_effect = requests.exceptions.Timeout("timed out")
        result = dj._sonos_request("state")
        self.assertIn("error", result)
        self.assertIn("timed out", result["error"])
        self.assertEqual(result["endpoint"], "state")

    @patch("server.requests.get")
    def test_connection_error(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("refused")
        result = dj._sonos_request("state")
        self.assertIn("error", result)
        self.assertIn("Cannot reach Sonos API", result["error"])
        self.assertEqual(result["endpoint"], "state")

    @patch("server.requests.get")
    def test_generic_request_exception(self, mock_get):
        mock_get.side_effect = requests.exceptions.RequestException("something broke")
        result = dj._sonos_request("state")
        self.assertIn("error", result)
        self.assertIn("Sonos request failed", result["error"])

    @patch("server.requests.get")
    def test_non_200_status(self, mock_get):
        mock_get.return_value = _mock_response(500)
        result = dj._sonos_request("state")
        self.assertIn("error", result)
        self.assertIn("HTTP 500", result["error"])

    @patch("server.requests.get")
    def test_custom_timeout(self, mock_get):
        mock_get.return_value = _mock_response(200, json_data={})
        dj._sonos_request("state", timeout=10)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["timeout"], 10)

    @patch("server.requests.get")
    def test_leading_slash_stripped(self, mock_get):
        mock_get.return_value = _mock_response(200, json_data={})
        dj._sonos_request("/state")
        url_called = mock_get.call_args[0][0]
        self.assertNotIn("//state", url_called)
        self.assertIn("/state", url_called)


# ===================================================================
# Tests for _do_pause, _do_resume, _do_skip, _do_previous
# ===================================================================

class TestSimplePlayback(unittest.TestCase):
    """Tests for simple playback control methods."""

    @patch.object(dj, "_sonos_request")
    def test_pause_success(self, mock_req):
        mock_req.return_value = {"ok": True}
        result = dj._do_pause()
        self.assertEqual(result, {"status": "paused"})
        mock_req.assert_called_once_with("pause")

    @patch.object(dj, "_sonos_request")
    def test_pause_error(self, mock_req):
        mock_req.return_value = {"error": "Sonos request timed out", "endpoint": "pause"}
        result = dj._do_pause()
        self.assertIn("error", result)
        self.assertIn("timed out", result["error"])

    @patch.object(dj, "_sonos_request")
    def test_resume_success(self, mock_req):
        mock_req.return_value = {"ok": True}
        result = dj._do_resume()
        self.assertEqual(result, {"status": "playing"})
        mock_req.assert_called_once_with("play")

    @patch.object(dj, "_sonos_request")
    def test_resume_error(self, mock_req):
        mock_req.return_value = {"error": "Cannot reach Sonos API (node-sonos-http-api)", "endpoint": "play"}
        result = dj._do_resume()
        self.assertIn("error", result)

    @patch.object(dj, "_sonos_request")
    def test_skip_success(self, mock_req):
        mock_req.return_value = {"ok": True}
        result = dj._do_skip()
        self.assertEqual(result, {"status": "skipped"})
        mock_req.assert_called_once_with("next")

    @patch.object(dj, "_sonos_request")
    def test_skip_error(self, mock_req):
        mock_req.return_value = {"error": "Sonos returned HTTP 500", "endpoint": "next"}
        result = dj._do_skip()
        self.assertIn("error", result)

    @patch.object(dj, "_sonos_request")
    def test_previous_success(self, mock_req):
        mock_req.return_value = {"ok": True}
        result = dj._do_previous()
        self.assertEqual(result, {"status": "previous"})
        mock_req.assert_called_once_with("previous")

    @patch.object(dj, "_sonos_request")
    def test_previous_error(self, mock_req):
        mock_req.return_value = {"error": "Sonos request failed: oops", "endpoint": "previous"}
        result = dj._do_previous()
        self.assertIn("error", result)


# ===================================================================
# Tests for _do_play
# ===================================================================

class TestDoPlay(unittest.TestCase):
    """Tests for _do_play method."""

    @patch.object(dj, "_sonos_request")
    def test_play_by_uri_success(self, mock_req):
        mock_req.return_value = {"ok": True}
        result = dj._do_play(uri="spotify:track:123")
        self.assertEqual(result, {"status": "playing", "uri": "spotify:track:123"})
        mock_req.assert_called_once_with("spotify/now/spotify:track:123")

    @patch.object(dj, "_sonos_request")
    def test_play_by_uri_error(self, mock_req):
        mock_req.return_value = {"error": "Sonos request timed out", "endpoint": "spotify/now/x"}
        result = dj._do_play(uri="spotify:track:aaa")
        self.assertIn("error", result)

    def test_play_rejects_traversal_uri(self):
        """A uri that could escape the room prefix never reaches Sonos."""
        with patch.object(dj, "_sonos_request") as mock_req:
            with self.assertRaises(BadRequest) as ctx:
                dj._do_play(uri="../../Bedroom/pause")
        self.assertEqual(ctx.exception.status, 400)
        mock_req.assert_not_called()

    @patch.object(dj, "_sonos_request")
    def test_play_by_num_success(self, mock_req):
        mock_req.return_value = {"ok": True}
        server.set_results([
            {"num": 1, "name": "Song A", "artist": "Art", "uri": "spotify:track:aaa"},
        ], "global")
        result = dj._do_play(num=1)
        self.assertEqual(result["status"], "playing")
        self.assertEqual(result["item"]["name"], "Song A")

    @patch.object(dj, "_sonos_request")
    def test_play_by_num_error(self, mock_req):
        mock_req.return_value = {"error": "Cannot reach Sonos API (node-sonos-http-api)", "endpoint": "x"}
        server.set_results([
            {"num": 1, "name": "Song A", "artist": "Art", "uri": "spotify:track:aaa"},
        ], "global")
        result = dj._do_play(num=1)
        self.assertIn("error", result)

    def test_play_invalid_num(self):
        server.set_results([], "global")
        with self.assertRaises(BadRequest) as ctx:
            dj._do_play(num=5)
        self.assertEqual(ctx.exception.status, 400)

    def test_play_no_args(self):
        with self.assertRaises(BadRequest) as ctx:
            dj._do_play()
        self.assertEqual(ctx.exception.status, 400)


# ===================================================================
# Tests for _do_queue
# ===================================================================

class TestDoQueue(unittest.TestCase):
    """Tests for _do_queue method."""

    @patch.object(dj, "_sonos_request")
    def test_queue_by_uri_success(self, mock_req):
        mock_req.return_value = {"ok": True}
        result = dj._do_queue(uri="spotify:track:456")
        self.assertEqual(result, {"status": "queued", "uri": "spotify:track:456"})

    @patch.object(dj, "_sonos_request")
    def test_queue_by_uri_error(self, mock_req):
        mock_req.return_value = {"error": "Sonos request timed out", "endpoint": "x"}
        result = dj._do_queue(uri="spotify:track:aaa")
        self.assertIn("error", result)

    @patch.object(dj, "_sonos_request")
    def test_queue_by_num_success(self, mock_req):
        mock_req.return_value = {"ok": True}
        server.set_results([
            {"num": 1, "name": "Song B", "artist": "Art", "uri": "spotify:track:bbb"},
        ], "global")
        result = dj._do_queue(num=1)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["item"]["name"], "Song B")

    def test_queue_no_args(self):
        with self.assertRaises(BadRequest) as ctx:
            dj._do_queue()
        self.assertEqual(ctx.exception.status, 400)


# ===================================================================
# Tests for _do_next
# ===================================================================

class TestDoNext(unittest.TestCase):
    """Tests for _do_next method."""

    @patch.object(dj, "_sonos_request")
    def test_next_by_uri_success(self, mock_req):
        mock_req.return_value = {"ok": True}
        result = dj._do_next(uri="spotify:track:789")
        self.assertEqual(result, {"status": "playing next", "uri": "spotify:track:789"})

    @patch.object(dj, "_sonos_request")
    def test_next_by_uri_error(self, mock_req):
        mock_req.return_value = {"error": "Sonos request timed out", "endpoint": "x"}
        result = dj._do_next(uri="spotify:track:aaa")
        self.assertIn("error", result)

    @patch.object(dj, "_sonos_request")
    def test_next_by_num_success(self, mock_req):
        mock_req.return_value = {"ok": True}
        server.set_results([
            {"num": 1, "name": "Song C", "artist": "Art", "uri": "spotify:track:ccc"},
        ], "global")
        result = dj._do_next(num=1)
        self.assertEqual(result["status"], "playing next")
        self.assertEqual(result["item"]["name"], "Song C")

    def test_next_no_args(self):
        with self.assertRaises(BadRequest) as ctx:
            dj._do_next()
        self.assertEqual(ctx.exception.status, 400)


# ===================================================================
# Tests for _do_volume
# ===================================================================

class TestDoVolume(unittest.TestCase):
    """Tests for _do_volume method."""

    @patch.object(dj, "_sonos_request")
    def test_volume_set_success(self, mock_req):
        mock_req.return_value = {"ok": True}
        result = dj._do_volume(level=50)
        self.assertEqual(result, {"status": "volume set", "level": 50})
        mock_req.assert_called_once_with("volume/50")

    @patch.object(dj, "_sonos_request")
    def test_volume_set_error(self, mock_req):
        mock_req.return_value = {"error": "Sonos request timed out", "endpoint": "volume/50"}
        result = dj._do_volume(level=50)
        self.assertIn("error", result)

    @patch.object(dj, "_sonos_request")
    def test_volume_adjust_success(self, mock_req):
        mock_req.side_effect = [{"ok": True}, {"volume": 30}]
        result = dj._do_volume(change="+10")
        self.assertEqual(
            result, {"status": "volume adjusted", "change": "+10", "level": 30})

    @patch.object(dj, "_sonos_request")
    def test_volume_adjust_omits_level_when_state_has_none(self, mock_req):
        """A null level is worse than no level -- it reads as a real value."""
        mock_req.side_effect = [{"ok": True}, {"playbackState": "PLAYING"}]
        result = dj._do_volume(change="+10")
        self.assertNotIn("level", result)

    @patch.object(dj, "_sonos_request")
    def test_volume_adjust_error(self, mock_req):
        mock_req.return_value = {"error": "Sonos returned HTTP 500", "endpoint": "volume/+10"}
        result = dj._do_volume(change="+10")
        self.assertIn("error", result)

    @patch.object(dj, "_sonos_request")
    def test_volume_query_success(self, mock_req):
        mock_req.return_value = {"volume": 35}
        result = dj._do_volume()
        self.assertEqual(result, {"volume": 35})

    @patch.object(dj, "_sonos_request")
    def test_volume_query_error(self, mock_req):
        mock_req.return_value = {"error": "Cannot reach Sonos API (node-sonos-http-api)", "endpoint": "state"}
        result = dj._do_volume()
        self.assertIn("error", result)


# ===================================================================
# Tests for _do_nowplaying
# ===================================================================

class TestDoNowplaying(unittest.TestCase):
    """Tests for _do_nowplaying method."""

    @patch.object(dj, "_sonos_request")
    def test_nowplaying_success(self, mock_req):
        mock_req.return_value = {
            "currentTrack": {
                "title": "Bohemian Rhapsody",
                "artist": "Queen",
                "album": "A Night at the Opera",
            },
            "volume": 40,
            "playbackState": "PLAYING",
        }
        result = dj._do_nowplaying()
        self.assertEqual(result["title"], "Bohemian Rhapsody")
        self.assertEqual(result["artist"], "Queen")
        self.assertEqual(result["album"], "A Night at the Opera")
        self.assertEqual(result["volume"], 40)
        self.assertEqual(result["playbackState"], "PLAYING")

    @patch.object(dj, "_sonos_request")
    def test_nowplaying_error(self, mock_req):
        mock_req.return_value = {"error": "Sonos request timed out", "endpoint": "state"}
        result = dj._do_nowplaying()
        self.assertEqual(result["title"], "Nothing playing")
        self.assertEqual(result["artist"], "")
        self.assertEqual(result["volume"], 0)
        self.assertEqual(result["playbackState"], "unknown")
        self.assertIn("error", result)

    @patch.object(dj, "_sonos_request")
    def test_nowplaying_no_current_track(self, mock_req):
        mock_req.return_value = {"volume": 10, "playbackState": "STOPPED"}
        result = dj._do_nowplaying()
        self.assertEqual(result["title"], "Nothing playing")
        self.assertEqual(result["volume"], 10)


# ===================================================================
# Tests for _do_getqueue
# ===================================================================

class TestDoGetqueue(unittest.TestCase):
    """Tests for _do_getqueue method."""

    @patch.object(dj, "_sonos_request")
    def test_getqueue_success(self, mock_req):
        queue_data = [{"title": "Track1"}, {"title": "Track2"}]
        mock_req.side_effect = [queue_data, {"trackNo": 3}]
        result = dj._do_getqueue()
        self.assertEqual(result, {"queue": queue_data, "limit": 50, "track_no": 3})
        self.assertEqual(mock_req.call_args_list[0][0][0], "queue/50")

    @patch.object(dj, "_sonos_request")
    def test_getqueue_error(self, mock_req):
        mock_req.return_value = {"error": "Cannot reach Sonos API (node-sonos-http-api)", "endpoint": "queue"}
        result = dj._do_getqueue()
        self.assertEqual(result["queue"], [])
        self.assertIn("error", result)


# ===================================================================
# Tests for _do_clearqueue
# ===================================================================

class TestDoClearqueue(unittest.TestCase):
    """Tests for _do_clearqueue method."""

    @patch.object(dj, "_sonos_request")
    def test_clearqueue_success(self, mock_req):
        mock_req.return_value = {"ok": True}
        result = dj._do_clearqueue()
        self.assertEqual(result, {"status": "queue cleared"})

    @patch.object(dj, "_sonos_request")
    def test_clearqueue_error(self, mock_req):
        mock_req.return_value = {"error": "Sonos returned HTTP 500", "endpoint": "clearqueue"}
        result = dj._do_clearqueue()
        self.assertIn("error", result)


# ===================================================================
# Tests for like (Sonos call portion)
# ===================================================================

class TestLike(unittest.TestCase):
    """Tests for like method's Sonos interaction."""

    @patch.object(dj, "_sonos_request")
    def test_like_sonos_error(self, mock_req):
        mock_req.return_value = {"error": "Cannot reach Sonos API (node-sonos-http-api)", "endpoint": "state"}
        result = dj.like()
        self.assertIn("error", result)
        self.assertIn("Cannot reach Sonos API", result["error"])

    @patch.object(dj, "_sonos_request")
    def test_like_non_spotify_track(self, mock_req):
        mock_req.return_value = {
            "currentTrack": {"uri": "x-rincon:something", "title": "Radio"},
            "volume": 30,
        }
        result = dj.like()
        self.assertIn("error", result)
        self.assertIn("not from Spotify", result["error"])


# ===================================================================
# Tests for recommend (Sonos call portion)
# ===================================================================

class TestRecommend(unittest.TestCase):
    """Tests for recommend method's Sonos interaction."""

    @patch.object(dj, "_sonos_request")
    def test_recommend_sonos_error(self, mock_req):
        mock_req.return_value = {"error": "Sonos request timed out", "endpoint": "state"}
        result = dj.recommend(based_on="nowplaying")
        self.assertIn("error", result)
        self.assertIn("timed out", result["error"])

    def test_recommend_bad_based_on(self):
        result = dj.recommend(based_on="other")
        self.assertIn("error", result)
        self.assertIn("nowplaying", result["error"])

    @patch.object(dj, "_sonos_request")
    def test_recommend_non_spotify(self, mock_req):
        mock_req.return_value = {
            "currentTrack": {"uri": "x-rincon:radio"},
        }
        result = dj.recommend(based_on="nowplaying")
        self.assertIn("error", result)
        self.assertIn("not from Spotify", result["error"])


# ===================================================================
# Tests for album_tracks (Sonos call portion)
# ===================================================================

class TestAlbumTracks(unittest.TestCase):
    """Tests for album_tracks method's Sonos interaction."""

    @patch.object(dj, "_sonos_request")
    def test_album_tracks_sonos_error(self, mock_req):
        mock_req.return_value = {"error": "Cannot reach Sonos API (node-sonos-http-api)", "endpoint": "state"}
        result = dj.album_tracks(based_on="nowplaying")
        self.assertIn("error", result)

    def test_album_tracks_bad_based_on(self):
        result = dj.album_tracks(based_on="other")
        self.assertIn("error", result)
        self.assertIn("nowplaying", result["error"])

    @patch.object(dj, "_sonos_request")
    def test_album_tracks_non_spotify(self, mock_req):
        mock_req.return_value = {
            "currentTrack": {"uri": "x-rincon:radio"},
        }
        result = dj.album_tracks(based_on="nowplaying")
        self.assertIn("error", result)
        self.assertIn("not from Spotify", result["error"])


# ===================================================================
# Integration-style: verify no bare requests.get to SONOS_URL remain
# ===================================================================

class TestNoBareRequestsGet(unittest.TestCase):
    """Verify that server.py has no remaining bare requests.get calls to SONOS_URL."""

    def test_no_bare_sonos_calls(self):
        import inspect
        source = inspect.getsource(server.DJServer)
        # There should be no requests.get(f"{SONOS_URL} patterns left
        self.assertNotIn('requests.get(f"{SONOS_URL}', source)
        self.assertNotIn("requests.get(f'{SONOS_URL}", source)


if __name__ == "__main__":
    unittest.main()
