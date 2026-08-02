"""Tests that Sonos upstream failures surface as HTTP 502, not 200.

_sonos_request returns an error dict rather than raising, so before this the
caller got HTTP 200 with a body it might never inspect -- a dead Sonos looked
like a successful pause to anything checking status codes.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests


def _response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_data
    return resp


class TestFailuresSet502:
    @pytest.mark.parametrize("exc", [
        requests.exceptions.Timeout("timed out"),
        requests.exceptions.ConnectionError("refused"),
        requests.exceptions.RequestException("boom"),
    ])
    def test_transport_failures(self, dj, server_mod, exc):
        with patch.object(server_mod.requests, "get", side_effect=exc):
            result = dj._sonos_request("state")

        assert "error" in result
        assert server_mod.cherrypy.response.status == 502

    def test_non_200_from_sonos(self, dj, server_mod):
        with patch.object(server_mod.requests, "get", return_value=_response(500)):
            result = dj._sonos_request("state")

        assert "error" in result
        assert server_mod.cherrypy.response.status == 502

    def test_error_dict_shape_is_unchanged(self, dj, server_mod):
        """Callers and 53 existing tests depend on this contract."""
        with patch.object(server_mod.requests, "get",
                          side_effect=requests.exceptions.Timeout("t")):
            result = dj._sonos_request("pause")

        assert set(result) == {"error", "endpoint"}
        assert result["endpoint"] == "pause"


class TestSuccessLeavesStatusAlone:
    def test_json_body(self, dj, server_mod):
        with patch.object(server_mod.requests, "get",
                          return_value=_response(200, {"volume": 12})):
            result = dj._sonos_request("state")

        assert result == {"volume": 12}
        assert server_mod.cherrypy.response.status != 502

    def test_empty_body_is_success_not_failure(self, dj, server_mod):
        """A 200 with no JSON is how Sonos acknowledges pause/play -- it must
        not be mistaken for an upstream error."""
        with patch.object(server_mod.requests, "get", return_value=_response(200)):
            result = dj._sonos_request("pause")

        assert result == {"ok": True}
        assert server_mod.cherrypy.response.status != 502


class TestPropagatesThroughHandlers:
    @pytest.mark.parametrize("method", [
        "_do_pause", "_do_resume", "_do_skip", "_do_previous",
        "_do_nowplaying", "_do_getqueue", "_do_clearqueue",
    ])
    def test_playback_handlers_report_502(self, dj, server_mod, method):
        with patch.object(server_mod.requests, "get",
                          side_effect=requests.exceptions.ConnectionError("down")):
            result = getattr(dj, method)()

        assert "error" in result
        assert server_mod.cherrypy.response.status == 502

    def test_play_by_uri_reports_502(self, dj, server_mod):
        with patch.object(server_mod.requests, "get",
                          side_effect=requests.exceptions.ConnectionError("down")):
            result = dj._do_play(uri="spotify:track:aaa")

        assert "error" in result
        assert server_mod.cherrypy.response.status == 502

    def test_volume_set_reports_502(self, dj, server_mod):
        with patch.object(server_mod.requests, "get",
                          side_effect=requests.exceptions.ConnectionError("down")):
            result = dj._do_volume(level=50)

        assert "error" in result
        assert server_mod.cherrypy.response.status == 502


class TestBadInputStaysA400:
    def test_invalid_uri_is_400_not_502(self, dj, server_mod):
        """The caller's mistake must not be blamed on the upstream."""
        with patch.object(server_mod, "requests") as mock_requests:
            with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                dj._do_play(uri="../../Bedroom/pause")

        assert exc.value.status == 400
        mock_requests.get.assert_not_called()

    def test_invalid_volume_is_400_not_502(self, dj, server_mod):
        with patch.object(server_mod, "requests") as mock_requests:
            with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                dj._do_volume(level="abc")

        assert exc.value.status == 400
        mock_requests.get.assert_not_called()


class TestQueuePosition:
    """track_no is what separates 'already played' from 'still to come'."""

    def test_reports_the_current_position(self, dj, server_mod):
        with patch.object(dj, "_sonos_request",
                          side_effect=[[{"title": "a"}], {"trackNo": 7}]):
            assert dj._do_getqueue()["track_no"] == 7

    def test_absent_when_state_is_unavailable(self, dj, server_mod):
        """A failed state call must not lose the queue we did fetch."""
        with patch.object(dj, "_sonos_request",
                          side_effect=[[{"title": "a"}], {"error": "down", "endpoint": "state"}]):
            result = dj._do_getqueue()
        assert result["queue"] == [{"title": "a"}]
        assert "track_no" not in result


class TestQueueEditErrorsAreJson:
    """The 409 from the drift guard is the response the UI most needs to
    parse; without an error_page handler CherryPy renders it as HTML."""

    def test_409_has_a_handler(self, server_mod):
        assert 'error_page.409' in server_mod.DJServer._cp_config

    @pytest.mark.parametrize("code", [400, 401, 404, 409, 429, 500, 502])
    def test_every_status_the_app_raises_renders_as_json(self, server_mod, code):
        assert f'error_page.{code}' in server_mod.DJServer._cp_config
