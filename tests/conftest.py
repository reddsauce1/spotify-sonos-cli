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


@pytest.fixture(autouse=True)
def _never_touch_real_data(monkeypatch, tmp_path):
    """Point the persisted-state paths at a temp dir for every test.

    Three modules each carried their own copy of this, so it protected only
    the tests that remembered to ask. It is a property of the whole suite --
    a test must not be able to rewrite the schedules that actually fire in the
    morning, whether or not its author thought about it -- so it belongs here
    and applies unconditionally.
    """
    monkeypatch.setattr(server_module, "SCHEDULES_PATH",
                        str(tmp_path / "schedules.json"))
    monkeypatch.setattr(server_module, "STATIONS_PATH",
                        str(tmp_path / "stations.json"))
    monkeypatch.setattr(server_module, "_schedules", [])
    monkeypatch.setattr(server_module, "_stations", [])
    # In-flight claims key off (entry id, step index), and the helpers default
    # to id "sch_test" -- so without this a claim left by one test blocks the
    # identically-named step in the next one.
    monkeypatch.setattr(server_module, "_steps_in_flight", set())
    yield


@pytest.fixture
def save_schedule(dj, monkeypatch):
    """Create or replace a routine the way the browser does.

    schedule_save reads and parses the raw body rather than taking form
    parameters, because a routine carries a list of steps. Feeding it real
    bytes rather than a pre-parsed dict means the tests exercise the size
    check and the parse guards too. Several test modules need to stand a
    routine up before testing something else, so the plumbing lives here
    rather than being repeated in each of them.
    """
    def _save(**body):
        monkeypatch.setattr(cherrypy.request, "body",
                            io.BytesIO(json.dumps(body).encode()), raising=False)
        return dj.schedule_save()
    return _save
