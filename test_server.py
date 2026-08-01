"""Tests for DJServer._sonos_request helper and all _do_* methods that use it."""
import pytest
import requests
from unittest.mock import patch, MagicMock


# ======================== _sonos_request TESTS ========================


class TestSonosRequest:
    """Tests for DJServer._sonos_request helper method."""

    def test_happy_path_json_200(self, dj):
        """200 response with valid JSON body returns parsed dict."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"currentTrack": {"title": "Song"}}

        with patch("requests.get", return_value=mock_resp) as mock_get:
            result = dj._sonos_request("state")

        assert result == {"currentTrack": {"title": "Song"}}
        mock_get.assert_called_once()

    def test_non_json_200_returns_ok(self, dj):
        """200 response where .json() raises ValueError returns {"ok": True}."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("No JSON")

        with patch("requests.get", return_value=mock_resp):
            result = dj._sonos_request("pause")

        assert result == {"ok": True}

    def test_timeout_returns_error(self, dj):
        """requests.exceptions.Timeout returns structured error."""
        with patch("requests.get", side_effect=requests.exceptions.Timeout("timed out")):
            result = dj._sonos_request("play")

        assert result == {"error": "Sonos request timed out", "endpoint": "play"}

    def test_connection_error_returns_error(self, dj):
        """requests.exceptions.ConnectionError returns structured error."""
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("refused")):
            result = dj._sonos_request("play")

        assert result == {
            "error": "Cannot reach Sonos API (node-sonos-http-api)",
            "endpoint": "play",
        }

    def test_generic_request_exception_returns_error(self, dj):
        """Generic RequestException returns structured error with message."""
        with patch(
            "requests.get",
            side_effect=requests.exceptions.RequestException("something bad"),
        ):
            result = dj._sonos_request("play")

        assert result["error"] == "Sonos request failed: something bad"
        assert result["endpoint"] == "play"

    def test_non_200_status_returns_error(self, dj):
        """Non-200 status code returns HTTP error dict."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("requests.get", return_value=mock_resp):
            result = dj._sonos_request("play")

        assert result == {"error": "Sonos returned HTTP 500", "endpoint": "play"}

    def test_default_timeout_is_5(self, dj):
        """Default timeout of 5 seconds is passed to requests.get."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}

        with patch("requests.get", return_value=mock_resp) as mock_get:
            dj._sonos_request("state")

        _, kwargs = mock_get.call_args
        assert kwargs["timeout"] == 5

    def test_custom_timeout_forwarded(self, dj):
        """Custom timeout kwarg is forwarded to requests.get."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}

        with patch("requests.get", return_value=mock_resp) as mock_get:
            dj._sonos_request("state", timeout=10)

        _, kwargs = mock_get.call_args
        assert kwargs["timeout"] == 10

    def test_url_construction_no_double_slash(self, dj, server_mod):
        """Endpoint with leading slash does not produce double-slash in URL."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}

        with patch("requests.get", return_value=mock_resp) as mock_get:
            dj._sonos_request("/state")

        url = mock_get.call_args[0][0]
        assert "//" not in url.replace("http://", "")


# ======================== _do_play TESTS ========================


class TestDoPlay:
    """Tests for DJServer._do_play."""

    def test_play_by_uri_success(self, dj):
        """Play by URI delegates to _sonos_request and returns status."""
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as mock_sr:
            result = dj._do_play(uri="spotify:track:abc123")

        mock_sr.assert_called_once_with("spotify/now/spotify:track:abc123")
        assert result == {"status": "playing", "uri": "spotify:track:abc123"}

    def test_play_by_uri_error_propagates(self, dj):
        """Play by URI propagates error from _sonos_request."""
        err = {"error": "Sonos request timed out", "endpoint": "spotify/now/x"}
        with patch.object(dj, "_sonos_request", return_value=err):
            result = dj._do_play(uri="x")

        assert "error" in result

    def test_play_by_num_success(self, dj, server_mod):
        """Play by num looks up search results and plays the track."""
        server_mod.set_results(
            [{"num": 1, "name": "Song A", "artist": "Art", "uri": "spotify:track:aaa"}],
            "global",
        )
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as mock_sr:
            result = dj._do_play(num=1)

        mock_sr.assert_called_once_with("spotify/now/spotify:track:aaa")
        assert result["status"] == "playing"
        assert result["item"]["name"] == "Song A"

    def test_play_by_num_error_propagates(self, dj, server_mod):
        """Play by num propagates _sonos_request error."""
        server_mod.set_results(
            [{"num": 1, "name": "Song A", "artist": "Art", "uri": "spotify:track:aaa"}],
            "global",
        )
        err = {"error": "timeout", "endpoint": "x"}
        with patch.object(dj, "_sonos_request", return_value=err):
            result = dj._do_play(num=1)

        assert result == err

    def test_play_num_out_of_range(self, dj, server_mod):
        """Num out of range returns error."""
        server_mod.set_results(
            [{"num": 1, "name": "Song", "artist": "A", "uri": "u"}], "global"
        )
        result = dj._do_play(num=5)
        assert "error" in result
        assert "Invalid selection" in result["error"]

    def test_play_no_args(self, dj):
        """No num or uri returns error."""
        result = dj._do_play()
        assert result == {"error": "Provide num or uri"}


# ======================== _do_queue TESTS ========================


class TestDoQueue:
    """Tests for DJServer._do_queue."""

    def test_queue_by_num_success(self, dj, server_mod):
        server_mod.set_results(
            [{"num": 1, "name": "Track", "artist": "A", "uri": "spotify:track:q1"}],
            "global",
        )
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as mock_sr:
            result = dj._do_queue(num=1)

        mock_sr.assert_called_once_with("spotify/queue/spotify:track:q1")
        assert result["status"] == "queued"
        assert result["item"]["uri"] == "spotify:track:q1"

    def test_queue_by_uri_success(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as mock_sr:
            result = dj._do_queue(uri="spotify:track:xyz")

        mock_sr.assert_called_once_with("spotify/queue/spotify:track:xyz")
        assert result == {"status": "queued", "uri": "spotify:track:xyz"}

    def test_queue_error_propagates(self, dj, server_mod):
        server_mod.set_results(
            [{"num": 1, "name": "T", "artist": "A", "uri": "u"}], "global"
        )
        err = {"error": "timeout", "endpoint": "x"}
        with patch.object(dj, "_sonos_request", return_value=err):
            result = dj._do_queue(num=1)
        assert result == err

    def test_queue_no_args(self, dj):
        result = dj._do_queue()
        assert result == {"error": "Provide num or uri"}


# ======================== _do_next TESTS ========================


class TestDoNext:
    """Tests for DJServer._do_next."""

    def test_next_by_num_success(self, dj, server_mod):
        server_mod.set_results(
            [{"num": 1, "name": "Nxt", "artist": "B", "uri": "spotify:track:n1"}],
            "global",
        )
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as mock_sr:
            result = dj._do_next(num=1)

        mock_sr.assert_called_once_with("spotify/next/spotify:track:n1")
        assert result["status"] == "playing next"

    def test_next_by_uri_success(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as mock_sr:
            result = dj._do_next(uri="spotify:track:n2")

        mock_sr.assert_called_once_with("spotify/next/spotify:track:n2")
        assert result == {"status": "playing next", "uri": "spotify:track:n2"}

    def test_next_error_propagates(self, dj, server_mod):
        server_mod.set_results(
            [{"num": 1, "name": "T", "artist": "A", "uri": "u"}], "global"
        )
        err = {"error": "conn", "endpoint": "x"}
        with patch.object(dj, "_sonos_request", return_value=err):
            result = dj._do_next(num=1)
        assert result == err

    def test_next_no_args(self, dj):
        result = dj._do_next()
        assert result == {"error": "Provide num or uri"}


# ======================== _do_pause TESTS ========================


class TestDoPause:
    def test_pause_success(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as mock_sr:
            result = dj._do_pause()
        mock_sr.assert_called_once_with("pause")
        assert result == {"status": "paused"}

    def test_pause_error(self, dj):
        err = {"error": "timeout", "endpoint": "pause"}
        with patch.object(dj, "_sonos_request", return_value=err):
            result = dj._do_pause()
        assert result == err


# ======================== _do_resume TESTS ========================


class TestDoResume:
    def test_resume_success(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as mock_sr:
            result = dj._do_resume()
        mock_sr.assert_called_once_with("play")
        assert result == {"status": "playing"}

    def test_resume_error(self, dj):
        err = {"error": "conn", "endpoint": "play"}
        with patch.object(dj, "_sonos_request", return_value=err):
            result = dj._do_resume()
        assert result == err


# ======================== _do_skip TESTS ========================


class TestDoSkip:
    def test_skip_success(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as mock_sr:
            result = dj._do_skip()
        mock_sr.assert_called_once_with("next")
        assert result == {"status": "skipped"}

    def test_skip_error(self, dj):
        err = {"error": "500", "endpoint": "next"}
        with patch.object(dj, "_sonos_request", return_value=err):
            result = dj._do_skip()
        assert result == err


# ======================== _do_previous TESTS ========================


class TestDoPrevious:
    def test_previous_success(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as mock_sr:
            result = dj._do_previous()
        mock_sr.assert_called_once_with("previous")
        assert result == {"status": "previous"}

    def test_previous_error(self, dj):
        err = {"error": "timeout", "endpoint": "previous"}
        with patch.object(dj, "_sonos_request", return_value=err):
            result = dj._do_previous()
        assert result == err


# ======================== _do_volume TESTS ========================


class TestDoVolume:
    def test_volume_set_level(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as mock_sr:
            result = dj._do_volume(level=50)
        mock_sr.assert_called_once_with("volume/50")
        assert result == {"status": "volume set", "level": 50}

    def test_volume_change(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as mock_sr:
            result = dj._do_volume(change="+10")
        mock_sr.assert_called_once_with("volume/+10")
        assert result == {"status": "volume adjusted", "change": "+10"}

    def test_volume_get_state(self, dj):
        with patch.object(
            dj, "_sonos_request", return_value={"volume": 42}
        ) as mock_sr:
            result = dj._do_volume()
        mock_sr.assert_called_once_with("state")
        assert result == {"volume": 42}

    def test_volume_error(self, dj):
        err = {"error": "timeout", "endpoint": "volume/50"}
        with patch.object(dj, "_sonos_request", return_value=err):
            result = dj._do_volume(level=50)
        assert result == err


# ======================== _do_nowplaying TESTS ========================


class TestDoNowPlaying:
    def test_nowplaying_success(self, dj):
        state = {
            "currentTrack": {"title": "Hey Jude", "artist": "Beatles", "album": "1"},
            "volume": 30,
            "playbackState": "PLAYING",
        }
        with patch.object(dj, "_sonos_request", return_value=state) as mock_sr:
            result = dj._do_nowplaying()
        mock_sr.assert_called_once_with("state")
        assert result["title"] == "Hey Jude"
        assert result["artist"] == "Beatles"
        assert result["volume"] == 30
        assert result["playbackState"] == "PLAYING"

    def test_nowplaying_error_fallback(self, dj):
        err = {"error": "Cannot reach Sonos API (node-sonos-http-api)", "endpoint": "state"}
        with patch.object(dj, "_sonos_request", return_value=err):
            result = dj._do_nowplaying()
        assert result["title"] == "Nothing playing"
        assert result["artist"] == ""
        assert "error" in result


# ======================== _do_getqueue TESTS ========================


class TestDoGetQueue:
    def test_getqueue_success(self, dj):
        queue_data = [{"title": "Track 1"}, {"title": "Track 2"}]
        with patch.object(dj, "_sonos_request", return_value=queue_data) as mock_sr:
            result = dj._do_getqueue()
        mock_sr.assert_called_once_with("queue")
        assert result == {"queue": queue_data}

    def test_getqueue_error(self, dj):
        err = {"error": "timeout", "endpoint": "queue"}
        with patch.object(dj, "_sonos_request", return_value=err):
            result = dj._do_getqueue()
        assert result == {"queue": [], "error": "timeout"}


# ======================== _do_clearqueue TESTS ========================


class TestDoClearQueue:
    def test_clearqueue_success(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as mock_sr:
            result = dj._do_clearqueue()
        mock_sr.assert_called_once_with("clearqueue")
        assert result == {"status": "queue cleared"}

    def test_clearqueue_error(self, dj):
        err = {"error": "500", "endpoint": "clearqueue"}
        with patch.object(dj, "_sonos_request", return_value=err):
            result = dj._do_clearqueue()
        assert result == err
