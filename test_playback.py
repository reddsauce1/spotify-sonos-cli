"""Tests for _sonos_request and every handler built on it.

This is the merge of what were test_server.py and test_sonos_request.py, which
covered the same handlers twice under different names -- 22 test names appeared
in both -- and disagreed on style, the second being the only unittest module
left in the suite.

Where the two overlapped the stronger assertion was kept: comparing the whole
result dict and the exact Sonos path called, rather than checking that the word
"error" appears somewhere. The cases only the other file had -- no current
track, the Sonos-facing half of like/recommend/album_tracks, and the check that
no bare requests.get survives -- are folded in below.
"""
import inspect
from unittest.mock import MagicMock, patch

import cherrypy
import pytest
import requests

# Bad input aborts with a 400 rather than returning an error dict, so these
# assert on the raised HTTPError.
BadRequest = cherrypy.HTTPError

ONE_RESULT = [{"num": 1, "name": "Song A", "artist": "Art", "uri": "spotify:track:aaa"}]


def _response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is None:
        resp.json.side_effect = ValueError("No JSON")
    else:
        resp.json.return_value = json_data
    return resp


# ======================== _sonos_request ========================


class TestSonosRequest:
    def test_happy_path_json_200(self, dj):
        with patch("requests.get", return_value=_response(200, {"currentTrack": {"title": "Song"}})) as get:
            result = dj._sonos_request("state")
        assert result == {"currentTrack": {"title": "Song"}}
        get.assert_called_once()

    def test_non_json_200_returns_ok(self, dj):
        """How Sonos acknowledges pause/play: 200 with an empty body."""
        with patch("requests.get", return_value=_response(200)):
            assert dj._sonos_request("pause") == {"ok": True}

    def test_timeout_returns_error(self, dj):
        with patch("requests.get", side_effect=requests.exceptions.Timeout("timed out")):
            result = dj._sonos_request("play")
        assert result == {"error": "Sonos request timed out", "endpoint": "play"}

    def test_connection_error_returns_error(self, dj):
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError("refused")):
            result = dj._sonos_request("play")
        assert result == {
            "error": "Cannot reach Sonos API (node-sonos-http-api)",
            "endpoint": "play",
        }

    def test_generic_request_exception_returns_error(self, dj):
        with patch("requests.get",
                   side_effect=requests.exceptions.RequestException("something bad")):
            result = dj._sonos_request("play")
        assert result == {"error": "Sonos request failed: something bad", "endpoint": "play"}

    def test_non_200_status_returns_error(self, dj):
        with patch("requests.get", return_value=_response(500)):
            result = dj._sonos_request("play")
        assert result == {"error": "Sonos returned HTTP 500", "endpoint": "play"}

    def test_default_timeout_is_5(self, dj):
        with patch("requests.get", return_value=_response(200, {})) as get:
            dj._sonos_request("state")
        assert get.call_args.kwargs["timeout"] == 5

    def test_custom_timeout_forwarded(self, dj):
        with patch("requests.get", return_value=_response(200, {})) as get:
            dj._sonos_request("state", timeout=10)
        assert get.call_args.kwargs["timeout"] == 10

    def test_url_construction_no_double_slash(self, dj):
        """A leading slash on the endpoint must not produce //."""
        with patch("requests.get", return_value=_response(200, {})) as get:
            dj._sonos_request("/state")
        url = get.call_args[0][0]
        assert "//" not in url.replace("http://", "")
        assert "/state" in url


# ======================== _do_play ========================


class TestDoPlay:
    def test_play_by_uri_success(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as sonos:
            result = dj._do_play(uri="spotify:track:abc123")
        sonos.assert_called_once_with("spotify/now/spotify:track:abc123")
        assert result == {"status": "playing", "uri": "spotify:track:abc123"}

    def test_play_by_uri_error_propagates(self, dj):
        err = {"error": "Sonos request timed out", "endpoint": "spotify/now/x"}
        with patch.object(dj, "_sonos_request", return_value=err):
            assert dj._do_play(uri="spotify:track:aaa") == err

    def test_play_rejects_traversal_uri(self, dj):
        """The uri is interpolated into the Sonos path, so one that could
        escape the room prefix must never reach it."""
        with patch.object(dj, "_sonos_request") as sonos:
            with pytest.raises(BadRequest) as exc:
                dj._do_play(uri="../../Bedroom/pause")
        assert exc.value.status == 400
        sonos.assert_not_called()

    def test_play_by_num_success(self, dj, server_mod):
        server_mod.set_results(ONE_RESULT, "global")
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as sonos:
            result = dj._do_play(num=1)
        sonos.assert_called_once_with("spotify/now/spotify:track:aaa")
        assert result["status"] == "playing"
        assert result["item"]["name"] == "Song A"

    def test_play_by_num_error_propagates(self, dj, server_mod):
        server_mod.set_results(ONE_RESULT, "global")
        err = {"error": "timeout", "endpoint": "x"}
        with patch.object(dj, "_sonos_request", return_value=err):
            assert dj._do_play(num=1) == err

    def test_play_num_out_of_range(self, dj, server_mod):
        server_mod.set_results(ONE_RESULT, "global")
        with pytest.raises(BadRequest) as exc:
            dj._do_play(num=5)
        assert exc.value.status == 400

    def test_play_num_before_any_search(self, dj, server_mod):
        """A separate branch from out-of-range, and the more likely mistake:
        asking for number 1 having never searched."""
        server_mod.set_results([], "global")
        with pytest.raises(BadRequest) as exc:
            dj._do_play(num=1)
        assert exc.value.status == 400
        assert "run a search first" in str(exc.value)

    def test_play_num_not_a_number(self, dj, server_mod):
        """A 400, not an uncaught ValueError."""
        server_mod.set_results(ONE_RESULT, "global")
        with pytest.raises(BadRequest) as exc:
            dj._do_play(num="abc")
        assert exc.value.status == 400

    def test_play_no_args(self, dj):
        with pytest.raises(BadRequest) as exc:
            dj._do_play()
        assert exc.value.status == 400


# ======================== _do_queue ========================


class TestDoQueue:
    def test_queue_by_num_success(self, dj, server_mod):
        server_mod.set_results(
            [{"num": 1, "name": "Track", "artist": "A", "uri": "spotify:track:q1"}], "global")
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as sonos:
            result = dj._do_queue(num=1)
        sonos.assert_called_once_with("spotify/queue/spotify:track:q1")
        assert result["status"] == "queued"
        assert result["item"]["uri"] == "spotify:track:q1"

    def test_queue_by_uri_success(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as sonos:
            result = dj._do_queue(uri="spotify:track:xyz")
        sonos.assert_called_once_with("spotify/queue/spotify:track:xyz")
        assert result == {"status": "queued", "uri": "spotify:track:xyz"}

    def test_queue_by_num_error_propagates(self, dj, server_mod):
        server_mod.set_results(ONE_RESULT, "global")
        err = {"error": "timeout", "endpoint": "x"}
        with patch.object(dj, "_sonos_request", return_value=err):
            assert dj._do_queue(num=1) == err

    def test_queue_by_uri_error_propagates(self, dj):
        err = {"error": "Sonos request timed out", "endpoint": "x"}
        with patch.object(dj, "_sonos_request", return_value=err):
            assert dj._do_queue(uri="spotify:track:aaa") == err

    def test_queue_no_args(self, dj):
        with pytest.raises(BadRequest) as exc:
            dj._do_queue()
        assert exc.value.status == 400


# ======================== _do_next ========================


class TestDoNext:
    def test_next_by_num_success(self, dj, server_mod):
        server_mod.set_results(
            [{"num": 1, "name": "Nxt", "artist": "B", "uri": "spotify:track:n1"}], "global")
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as sonos:
            result = dj._do_next(num=1)
        sonos.assert_called_once_with("spotify/next/spotify:track:n1")
        assert result["status"] == "playing next"

    def test_next_by_uri_success(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as sonos:
            result = dj._do_next(uri="spotify:track:n2")
        sonos.assert_called_once_with("spotify/next/spotify:track:n2")
        assert result == {"status": "playing next", "uri": "spotify:track:n2"}

    def test_next_by_num_error_propagates(self, dj, server_mod):
        server_mod.set_results(ONE_RESULT, "global")
        err = {"error": "conn", "endpoint": "x"}
        with patch.object(dj, "_sonos_request", return_value=err):
            assert dj._do_next(num=1) == err

    def test_next_by_uri_error_propagates(self, dj):
        err = {"error": "Sonos request timed out", "endpoint": "x"}
        with patch.object(dj, "_sonos_request", return_value=err):
            assert dj._do_next(uri="spotify:track:aaa") == err

    def test_next_no_args(self, dj):
        with pytest.raises(BadRequest) as exc:
            dj._do_next()
        assert exc.value.status == 400


# ======================== pause / resume / skip / previous ========================


class TestSimplePlayback:
    """Each maps to one Sonos endpoint and one status string. The endpoint
    name is asserted because play/next are easy to swap by accident: Sonos
    calls resume "play" and skip "next"."""

    CASES = [
        ("_do_pause", "pause", {"status": "paused"}),
        ("_do_resume", "play", {"status": "playing"}),
        ("_do_skip", "next", {"status": "skipped"}),
        ("_do_previous", "previous", {"status": "previous"}),
    ]

    @pytest.mark.parametrize("method,endpoint,expected", CASES)
    def test_success(self, dj, method, endpoint, expected):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as sonos:
            result = getattr(dj, method)()
        sonos.assert_called_once_with(endpoint)
        assert result == expected

    @pytest.mark.parametrize("method,endpoint,expected", CASES)
    def test_error_propagates(self, dj, method, endpoint, expected):
        err = {"error": "timeout", "endpoint": endpoint}
        with patch.object(dj, "_sonos_request", return_value=err):
            assert getattr(dj, method)() == err


# ======================== _do_volume ========================


class TestDoVolume:
    def test_volume_set_level(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as sonos:
            result = dj._do_volume(level=50)
        sonos.assert_called_once_with("volume/50")
        assert result == {"status": "volume set", "level": 50}

    def test_volume_set_error(self, dj):
        err = {"error": "timeout", "endpoint": "volume/50"}
        with patch.object(dj, "_sonos_request", return_value=err):
            assert dj._do_volume(level=50) == err

    def test_volume_change_reports_where_it_landed(self, dj):
        """Two calls: the change, then a state read. A caller nudging by one
        cannot see the result otherwise."""
        with patch.object(dj, "_sonos_request",
                          side_effect=[{"ok": True}, {"volume": 30}]) as sonos:
            result = dj._do_volume(change="+10")
        assert sonos.call_args_list[0].args == ("volume/+10",)
        assert result == {"status": "volume adjusted", "change": "+10", "level": 30}

    def test_volume_change_omits_level_when_state_has_none(self, dj):
        """A null level is worse than no level -- it reads as a real value."""
        with patch.object(dj, "_sonos_request",
                          side_effect=[{"ok": True}, {"playbackState": "PLAYING"}]):
            assert "level" not in dj._do_volume(change="+10")

    def test_volume_change_error(self, dj):
        err = {"error": "Sonos returned HTTP 500", "endpoint": "volume/+10"}
        with patch.object(dj, "_sonos_request", return_value=err):
            assert dj._do_volume(change="+10") == err

    def test_volume_query(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"volume": 42}) as sonos:
            result = dj._do_volume()
        sonos.assert_called_once_with("state")
        assert result == {"volume": 42}

    def test_volume_query_error(self, dj):
        err = {"error": "Cannot reach Sonos API (node-sonos-http-api)", "endpoint": "state"}
        with patch.object(dj, "_sonos_request", return_value=err):
            assert dj._do_volume() == err


# ======================== _do_nowplaying ========================


class TestDoNowPlaying:
    def test_success(self, dj):
        state = {
            "currentTrack": {"title": "Hey Jude", "artist": "Beatles", "album": "1"},
            "volume": 30,
            "playbackState": "PLAYING",
        }
        with patch.object(dj, "_sonos_request", return_value=state) as sonos:
            result = dj._do_nowplaying()
        sonos.assert_called_once_with("state")
        assert result["title"] == "Hey Jude"
        assert result["artist"] == "Beatles"
        assert result["album"] == "1"
        assert result["volume"] == 30
        assert result["playbackState"] == "PLAYING"

    def test_error_falls_back_without_losing_the_shape(self, dj):
        """Every field the UI reads must still be present, or the page renders
        undefined rather than "Nothing playing"."""
        err = {"error": "Cannot reach Sonos API (node-sonos-http-api)", "endpoint": "state"}
        with patch.object(dj, "_sonos_request", return_value=err):
            result = dj._do_nowplaying()
        assert result["title"] == "Nothing playing"
        assert result["artist"] == ""
        assert result["volume"] == 0
        assert result["playbackState"] == "unknown"
        assert "error" in result

    def test_stopped_with_no_current_track(self, dj):
        """An idle player returns state with no currentTrack at all."""
        with patch.object(dj, "_sonos_request",
                          return_value={"volume": 10, "playbackState": "STOPPED"}):
            result = dj._do_nowplaying()
        assert result["title"] == "Nothing playing"
        assert result["volume"] == 10
        assert "error" not in result


# ======================== _do_getqueue / _do_clearqueue ========================


class TestDoGetQueue:
    def test_success(self, dj):
        queue = [{"title": "Track 1"}, {"title": "Track 2"}]
        with patch.object(dj, "_sonos_request",
                          side_effect=[queue, {"trackNo": 3}]) as sonos:
            result = dj._do_getqueue()
        # Must request a bounded slice: the unbounded /queue takes longer than
        # the request timeout on a long queue, so it always timed out.
        assert sonos.call_args_list[0].args == ("queue/50",)
        assert result == {"queue": queue, "limit": 50, "track_no": 3}

    def test_error(self, dj):
        err = {"error": "timeout", "endpoint": "queue"}
        with patch.object(dj, "_sonos_request", return_value=err):
            assert dj._do_getqueue() == {"queue": [], "error": "timeout"}


class TestDoClearQueue:
    def test_success(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as sonos:
            result = dj._do_clearqueue()
        sonos.assert_called_once_with("clearqueue")
        assert result == {"status": "queue cleared"}

    def test_error(self, dj):
        err = {"error": "500", "endpoint": "clearqueue"}
        with patch.object(dj, "_sonos_request", return_value=err):
            assert dj._do_clearqueue() == err


# ======================== handlers that read what is playing ========================


class TestHandlersThatStartFromNowPlaying:
    """like, recommend and album_tracks all begin by asking Sonos what is
    playing, so they share two failure modes: Sonos being unreachable, and the
    current track not being a Spotify one (radio, line-in, TV)."""

    NON_SPOTIFY = {"currentTrack": {"uri": "x-rincon:radio", "title": "Radio"}, "volume": 30}
    SONOS_DOWN = {"error": "Cannot reach Sonos API (node-sonos-http-api)", "endpoint": "state"}

    def test_like_reports_sonos_being_down(self, dj):
        with patch.object(dj, "_sonos_request", return_value=self.SONOS_DOWN):
            result = dj.like()
        assert "Cannot reach Sonos API" in result["error"]

    def test_like_on_a_non_spotify_track(self, dj):
        with patch.object(dj, "_sonos_request", return_value=self.NON_SPOTIFY):
            assert "not from Spotify" in dj.like()["error"]

    def test_recommend_reports_sonos_being_down(self, dj):
        with patch.object(dj, "_sonos_request",
                          return_value={"error": "Sonos request timed out", "endpoint": "state"}):
            assert "timed out" in dj.recommend(based_on="nowplaying")["error"]

    def test_recommend_rejects_an_unsupported_based_on(self, dj):
        assert "nowplaying" in dj.recommend(based_on="other")["error"]

    def test_recommend_on_a_non_spotify_track(self, dj):
        with patch.object(dj, "_sonos_request", return_value=self.NON_SPOTIFY):
            assert "not from Spotify" in dj.recommend(based_on="nowplaying")["error"]

    def test_album_tracks_reports_sonos_being_down(self, dj):
        with patch.object(dj, "_sonos_request", return_value=self.SONOS_DOWN):
            assert "error" in dj.album_tracks(based_on="nowplaying")

    def test_album_tracks_rejects_an_unsupported_based_on(self, dj):
        assert "nowplaying" in dj.album_tracks(based_on="other")["error"]

    def test_album_tracks_on_a_non_spotify_track(self, dj):
        with patch.object(dj, "_sonos_request", return_value=self.NON_SPOTIFY):
            assert "not from Spotify" in dj.album_tracks(based_on="nowplaying")["error"]


# ======================== discipline ========================


class TestEveryCallGoesThroughTheHelper:
    """A bare requests.get would skip the timeout, the error translation and
    the 502, so a dead Sonos would surface as an HTTP 200."""

    def test_no_bare_sonos_calls_remain(self, server_mod):
        source = inspect.getsource(server_mod.DJServer)
        assert 'requests.get(f"{SONOS_URL}' not in source
        assert "requests.get(f'{SONOS_URL}" not in source
