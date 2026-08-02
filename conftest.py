"""Conftest: patches module-level Spotify/config so server.py can be imported in tests."""
import builtins
import io
import sys
import json
from unittest.mock import patch, MagicMock

import cherrypy
import pytest


# Fake config that server.py expects
_FAKE_CONFIG = {
    "client_id": "fake_client_id",
    "client_secret": "fake_client_secret",
    "sonos_room": "TestRoom",
    "anthropic_api_key": "fake_key",
    "ui_password": "",
}


@pytest.fixture(autouse=True)
def _reset_cherrypy_response():
    """Reset the response status between tests.

    cherrypy.response is a thread-local shared by every test in the run, so a
    handler that sets 503 leaves it set for whatever executes next. Without
    this, a test asserting on a status code passes or fails depending on the
    order tests happen to run in.
    """
    cherrypy.response.status = 200
    yield


_real_open = builtins.open


def _fake_open(path, *args, **kwargs):
    """Serve a fake config.json; pass every other path through to real open().

    Deliberately a plain function rather than a MagicMock. Patching
    builtins.open with a mock means every open() performed while importing
    cherrypy and spotipy gets recorded into the mock's call tree, and
    _increment_mock_call walking that tree turns the import into an
    effectively infinite hang.
    """
    if str(path).endswith("config.json"):
        return io.StringIO(json.dumps(_FAKE_CONFIG))
    return _real_open(path, *args, **kwargs)


def _import_server():
    """Import server.py with all external dependencies mocked."""
    # Remove cached module if present so patches take effect
    sys.modules.pop("server", None)

    with patch("builtins.open", _fake_open):
        with patch("spotipy.Spotify"):
            with patch("spotipy.oauth2.SpotifyOAuth"):
                import server
    return server


# Import once at conftest load time so all tests share the module
server_module = _import_server()


@pytest.fixture
def dj():
    """Return a fresh DJServer instance for testing."""
    return server_module.DJServer()


@pytest.fixture
def server_mod():
    """Return the server module for accessing module-level functions/constants."""
    return server_module


@pytest.fixture
def save_schedule(dj, monkeypatch):
    """Create or replace a routine the way the browser does.

    schedule_save reads a JSON body off cherrypy.request rather than taking
    form parameters, because a routine carries a list of steps. Several test
    modules need to stand a routine up before testing something else, so the
    plumbing lives here rather than being repeated in each of them.
    """
    def _save(**body):
        monkeypatch.setattr(cherrypy.request, "json", body, raising=False)
        return dj.schedule_save()
    return _save
