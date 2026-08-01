"""Tests for _handles_spotify_errors, which maps spotipy failures to HTTP status.

Before this decorator existed any sp.*() failure -- an expired token, a rate
limit, a deleted playlist -- propagated out of the handler as an unhandled
exception and CherryPy served an HTML 500 that no client here can parse.
"""
import pytest
import requests
from unittest.mock import patch

from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOauthError


def _raising(exc):
    """A decorated function that always raises `exc`."""
    import server

    @server._handles_spotify_errors
    def boom():
        raise exc

    return boom


class TestStatusMapping:
    def test_success_passes_through_untouched(self, server_mod):
        @server_mod._handles_spotify_errors
        def ok():
            return {"status": "fine"}

        assert ok() == {"status": "fine"}

    def test_rate_limit_surfaces_as_429(self, server_mod):
        """429 is passed through so clients can back off rather than hammer."""
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            _raising(SpotifyException(429, -1, "rate limited"))()
        assert exc.value.status == 429

    def test_not_found_surfaces_as_404(self, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            _raising(SpotifyException(404, -1, "no such playlist"))()
        assert exc.value.status == 404

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_failures_become_502_not_401(self, server_mod, status):
        """A Spotify token problem must not look like the caller being
        unauthenticated -- that would send users to the login page for a
        fault on the server side."""
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            _raising(SpotifyException(status, -1, "token expired"))()
        assert exc.value.status == 502
        assert "auth.py" in str(exc.value)

    @pytest.mark.parametrize("status", [500, 502, 503, None])
    def test_other_spotify_errors_become_502(self, server_mod, status):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            _raising(SpotifyException(status, -1, "upstream boom"))()
        assert exc.value.status == 502

    def test_oauth_error_becomes_502(self, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            _raising(SpotifyOauthError("refresh failed"))()
        assert exc.value.status == 502
        assert "auth.py" in str(exc.value)

    def test_network_failure_becomes_502(self, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            _raising(requests.exceptions.ConnectionError("dns died"))()
        assert exc.value.status == 502

    def test_unrelated_exception_is_not_swallowed(self, server_mod):
        """A genuine bug must still surface rather than be relabelled 502."""
        with pytest.raises(ZeroDivisionError):
            _raising(ZeroDivisionError("real bug"))()

    def test_preserves_function_metadata(self, server_mod):
        """functools.wraps keeps CherryPy's introspection of handlers working."""
        @server_mod._handles_spotify_errors
        def named_handler():
            return None

        assert named_handler.__name__ == "named_handler"


class TestDecoratorIsApplied:
    """The mapping is worthless if a handler that calls Spotify is missed."""

    @pytest.mark.parametrize("name", [
        "_do_search", "my", "like", "create_playlist",
        "add_to_playlist", "recommend", "album_tracks",
    ])
    def test_spotify_touching_handlers_are_wrapped(self, server_mod, name):
        fn = getattr(server_mod.DJServer, name)
        assert getattr(fn, "__wrapped__", None) is not None, (
            f"{name} calls Spotify but is not wrapped in _handles_spotify_errors"
        )


class TestEndToEndThroughHandlers:
    """Drive real handlers with a failing Spotify client."""

    def test_search_maps_rate_limit(self, dj, server_mod):
        with patch.object(server_mod, "sp") as mock_sp:
            mock_sp.search.side_effect = SpotifyException(429, -1, "slow down")
            with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                dj._do_search(q="beatles")
        assert exc.value.status == 429

    def test_like_maps_expired_token(self, dj, server_mod):
        with patch.object(server_mod, "sp") as mock_sp:
            mock_sp.current_user_saved_tracks_add.side_effect = SpotifyException(
                401, -1, "token expired"
            )
            with patch.object(dj, "_sonos_request", return_value={
                "currentTrack": {"uri": "spotify:track:aaa", "title": "T"}
            }):
                with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                    dj.like()
        assert exc.value.status == 502

    def test_create_playlist_maps_network_failure(self, dj, server_mod):
        with patch.object(server_mod, "sp") as mock_sp:
            mock_sp.current_user.side_effect = requests.exceptions.ConnectionError("down")
            with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                dj.create_playlist(name="Party")
        assert exc.value.status == 502
