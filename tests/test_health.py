"""Tests for /health.

The point of this endpoint is to be right when things are broken, so most of
these drive it with a failing upstream rather than a working one.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests
from spotipy.exceptions import SpotifyException


def _sonos_ok(status_code=200, zones=None):
    """A /zones response. The default carries one real-shaped zone, because a
    200 with an empty list is a distinct failure the endpoint has to catch."""
    response = MagicMock(status_code=status_code)
    response.json.return_value = (
        [{"coordinator": {"roomName": "Dining Room"}}] if zones is None else zones
    )
    return response


class TestAllHealthy:
    def test_reports_ok_and_leaves_status_200(self, dj, server_mod):
        resp = server_mod.cherrypy.response
        with patch.object(server_mod.requests, "get", return_value=_sonos_ok()):
            with patch.object(server_mod, "sp") as mock_sp:
                mock_sp.me.return_value = {"id": "someone"}
                result = dj.health()

        assert result["sonos"] == "ok"
        assert result["spotify"] == "ok"
        assert resp.status != 503

    def test_queries_the_unscoped_zones_endpoint(self, dj, server_mod):
        """/zones is API-wide. Prefixing the room name would 404 and make a
        healthy Sonos look broken."""
        with patch.object(server_mod.requests, "get", return_value=_sonos_ok()) as mock_get:
            with patch.object(server_mod, "sp"):
                dj.health()

        url = mock_get.call_args[0][0]
        assert url == "http://localhost:5005/zones"
        assert server_mod.SONOS_ROOM not in url
        assert mock_get.call_args.kwargs["timeout"] == 3

    def test_uptime_is_a_non_negative_number(self, dj, server_mod):
        with patch.object(server_mod.requests, "get", return_value=_sonos_ok()):
            with patch.object(server_mod, "sp"):
                result = dj.health()
        assert isinstance(result["uptime_seconds"], int)
        assert result["uptime_seconds"] >= 0


class TestSonosDown:
    @pytest.mark.parametrize("exc", [
        requests.exceptions.ConnectionError("refused"),
        requests.exceptions.Timeout("timed out"),
    ])
    def test_unreachable_is_reported_not_raised(self, dj, server_mod, exc):
        resp = server_mod.cherrypy.response
        with patch.object(server_mod.requests, "get", side_effect=exc):
            with patch.object(server_mod, "sp"):
                result = dj.health()

        assert result["sonos"].startswith("error")
        assert result["spotify"] == "ok"
        assert resp.status == 503

    def test_non_200_counts_as_unhealthy(self, dj, server_mod):
        with patch.object(server_mod.requests, "get", return_value=_sonos_ok(500)):
            with patch.object(server_mod, "sp"):
                result = dj.health()
        assert result["sonos"].startswith("error")
        assert "500" in result["sonos"]

    def test_error_text_does_not_leak_the_exception_message(self, dj, server_mod):
        """Messages can carry internal hostnames; the class name is enough."""
        with patch.object(server_mod.requests, "get",
                          side_effect=requests.exceptions.ConnectionError("secret-host:5005 refused")):
            with patch.object(server_mod, "sp"):
                result = dj.health()
        assert "secret-host" not in result["sonos"]


class TestSpotifyDown:
    def test_expired_token_is_reported_not_a_502(self, dj, server_mod):
        """_handles_spotify_errors would abort with 502 here. /health must
        still return a body saying which side is broken."""
        resp = server_mod.cherrypy.response
        with patch.object(server_mod.requests, "get", return_value=_sonos_ok()):
            with patch.object(server_mod, "sp") as mock_sp:
                mock_sp.me.side_effect = SpotifyException(401, -1, "token expired")
                result = dj.health()

        assert result["spotify"].startswith("error")
        assert result["sonos"] == "ok"
        assert resp.status == 503

    def test_network_failure_is_reported(self, dj, server_mod):
        with patch.object(server_mod.requests, "get", return_value=_sonos_ok()):
            with patch.object(server_mod, "sp") as mock_sp:
                mock_sp.me.side_effect = requests.exceptions.ConnectionError("dns")
                result = dj.health()
        assert result["spotify"].startswith("error")


class TestBothDown:
    def test_reports_both_and_still_returns_uptime(self, dj, server_mod):
        resp = server_mod.cherrypy.response
        with patch.object(server_mod.requests, "get",
                          side_effect=requests.exceptions.ConnectionError("no sonos")):
            with patch.object(server_mod, "sp") as mock_sp:
                mock_sp.me.side_effect = SpotifyException(500, -1, "spotify down")
                result = dj.health()

        assert result["sonos"].startswith("error")
        assert result["spotify"].startswith("error")
        assert "uptime_seconds" in result
        assert resp.status == 503


class TestNotPublic:
    def test_health_is_not_in_public_paths(self, server_mod):
        """It makes a live Spotify call -- an open /health lets anyone burn
        the account's rate limit."""
        assert "/health" not in server_mod.PUBLIC_PATHS

    def test_health_is_not_wrapped_in_the_spotify_decorator(self, server_mod):
        """Wrapping it would turn a Spotify outage into a 502 with no body,
        defeating the endpoint."""
        assert getattr(server_mod.DJServer.health, "__wrapped__", None) is None


class TestSonosUpButNotReady:
    """node-sonos-http-api answers before SSDP discovery has found anything,
    so a 200 is not the same as a working system."""

    def test_an_empty_zone_list_is_not_healthy(self, dj, server_mod):
        resp = server_mod.cherrypy.response
        with patch.object(server_mod.requests, "get", return_value=_sonos_ok(zones=[])):
            with patch.object(server_mod, "sp"):
                result = dj.health()
        assert result["sonos"] == "error: no zones discovered"
        assert resp.status == 503

    def test_an_unparseable_body_is_not_healthy(self, dj, server_mod):
        response = MagicMock(status_code=200)
        response.json.side_effect = ValueError("not json")
        with patch.object(server_mod.requests, "get", return_value=response):
            with patch.object(server_mod, "sp"):
                result = dj.health()
        assert result["sonos"] == "error: unparseable zone list"


class TestScheduleFailuresAreVisible:
    """A routine that quietly did nothing should be findable without reading
    the log."""

    def _health(self, dj, server_mod):
        with patch.object(server_mod.requests, "get", return_value=_sonos_ok()):
            with patch.object(server_mod, "sp"):
                return dj.health()

    def test_zero_when_everything_ran(self, dj, server_mod):
        server_mod._schedules.append(
            {"id": "s", "steps": [{"action": "pause", "last_fired": "2026-08-04"}]})
        assert self._health(dj, server_mod)["schedule_steps_failed"] == 0

    def test_counts_steps_that_failed(self, dj, server_mod):
        server_mod._schedules.append({"id": "s", "steps": [
            {"action": "pause", "last_error": {"message": "timed out"}},
            {"action": "play", "last_fired": "2026-08-04"},
        ]})
        assert self._health(dj, server_mod)["schedule_steps_failed"] == 1

    def test_a_failed_routine_does_not_turn_health_red(self, dj, server_mod):
        """Reachability is what 503 means. A stale failure from last week must
        not keep the endpoint alarming forever."""
        resp = server_mod.cherrypy.response
        server_mod._schedules.append(
            {"id": "s", "steps": [{"action": "pause", "last_error": {"message": "x"}}]})
        self._health(dj, server_mod)
        assert resp.status != 503
