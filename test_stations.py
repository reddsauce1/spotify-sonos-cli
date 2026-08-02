"""Tests for saved stations.

Spotify's Song Radio is an algorithmic playlist (37i9dQZF1E8...). Those URIs
404 on the Web API but Sonos resolves them, so they play; what is impossible
is deriving one for a track, because the recommendations and related-artists
endpoints were withdrawn from third-party apps in Nov 2024. Stations exist so
a URI copied out of Spotify once becomes reusable.
"""
import json
import os

import pytest


RADIO_URI = "spotify:playlist:37i9dQZF1E8KIGiyBtlkTg"


@pytest.fixture(autouse=True)
def _isolate(server_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "STATIONS_PATH", str(tmp_path / "stations.json"))
    monkeypatch.setattr(server_mod, "_stations", [])
    yield


class TestCrud:
    def test_add_then_list(self, dj, server_mod):
        added = dj.station_add(name="Kim Fowley Radio", uri=RADIO_URI)
        assert added["station"]["uri"] == RADIO_URI
        assert dj.stations()["stations"][0]["name"] == "Kim Fowley Radio"

    def test_delete(self, dj, server_mod):
        sid = dj.station_add(name="x", uri=RADIO_URI)["station"]["id"]
        dj.station_delete(id=sid)
        assert dj.stations()["stations"] == []

    def test_delete_unknown_is_400(self, dj, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            dj.station_delete(id="stn_nope")
        assert exc.value.status == 400

    def test_cap(self, dj, server_mod, monkeypatch):
        monkeypatch.setattr(server_mod, "MAX_STATIONS", 2)
        dj.station_add(name="a", uri=RADIO_URI)
        dj.station_add(name="b", uri=RADIO_URI)
        with pytest.raises(server_mod.cherrypy.HTTPError):
            dj.station_add(name="c", uri=RADIO_URI)


class TestValidation:
    def test_a_radio_playlist_uri_is_accepted(self, dj, server_mod):
        """These 404 on the Web API but play through Sonos, so the server must
        not reject them for being unresolvable."""
        assert dj.station_add(name="radio", uri=RADIO_URI)["station"]["uri"] == RADIO_URI

    @pytest.mark.parametrize("bad", [
        "../../Bedroom/pause",
        "http://evil.example/x",
        "not-a-uri",
        "",
        None,
    ])
    def test_bad_uris_rejected(self, dj, server_mod, bad):
        """The uri reaches the Sonos request path, so it is validated here for
        the same reason it is validated on /play."""
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            dj.station_add(name="x", uri=bad)
        assert exc.value.status == 400

    def test_name_is_required(self, dj, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError):
            dj.station_add(name="  ", uri=RADIO_URI)

    def test_name_is_bounded(self, dj, server_mod):
        entry = dj.station_add(name="z" * 500, uri=RADIO_URI)["station"]
        assert len(entry["name"]) <= 80


class TestPersistence:
    def test_round_trip(self, dj, server_mod):
        dj.station_add(name="Kim Fowley Radio", uri=RADIO_URI)
        assert server_mod._load_stations()[0]["name"] == "Kim Fowley Radio"

    def test_missing_file_is_not_fatal(self, server_mod):
        assert server_mod._load_stations() == []

    def test_corrupt_file_is_not_fatal(self, server_mod, caplog):
        import logging
        with open(server_mod.STATIONS_PATH, "w") as f:
            f.write("{{{ not json")
        with caplog.at_level(logging.INFO, logger="dj"):
            assert server_mod._load_stations() == []
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_write_is_atomic(self, dj, server_mod):
        dj.station_add(name="x", uri=RADIO_URI)
        assert not os.path.exists(server_mod.STATIONS_PATH + ".tmp")
        assert json.load(open(server_mod.STATIONS_PATH))


class TestEndpointDiscipline:
    def test_mutations_are_post_only(self, server_mod):
        for name in ("station_add", "station_delete"):
            conf = getattr(getattr(server_mod.DJServer, name), "_cp_config", {})
            assert conf.get("tools.allow.methods") == ["POST"], name

    def test_playlist_mutations_are_post_only(self, server_mod):
        """These change Spotify state, so a GET would let a stray link or a
        prefetch mutate a playlist."""
        for name in ("create_playlist", "add_to_playlist"):
            conf = getattr(getattr(server_mod.DJServer, name), "_cp_config", {})
            assert conf.get("tools.allow.methods") == ["POST"], name

    def test_stations_are_not_public(self, server_mod):
        for path in ("/stations", "/station_add", "/station_delete"):
            assert path not in server_mod.PUBLIC_PATHS


class TestUsableAsAScheduleStep:
    def test_a_station_uri_is_valid_in_a_step(self, server_mod, tmp_path,
                                              monkeypatch, save_schedule):
        """The point of saving them: they become selectable as schedule steps.
        A radio playlist 404s on the Web API, so a step must not require the
        uri to be resolvable."""
        monkeypatch.setattr(server_mod, "SCHEDULES_PATH", str(tmp_path / "s.json"))
        monkeypatch.setattr(server_mod, "_schedules", [])
        result = save_schedule(time="07:00", days=[], label="radio wake",
                               steps=[{"offset": 0, "action": "play", "uri": RADIO_URI}])
        assert result["schedule"]["steps"][0]["uri"] == RADIO_URI
