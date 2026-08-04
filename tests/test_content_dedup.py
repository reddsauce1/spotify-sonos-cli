"""Tests for collapsing repeated content loads.

The failure being prevented: a playlist add that timed out at the caller but
succeeded on the speaker ~46s later, so every retry queued another copy. One
morning that left 8,950 tracks in a queue that had held 85.

The tension worth holding in mind is that the dedupe must not eat the
scheduler's retry. A timeout is ambiguous and must not be repeated; a refused
connection never reached the speaker and must be repeatable immediately.
"""
from unittest.mock import patch

import pytest


PLAYLIST = "spotify:playlist:4MNWVZkgnOs5ytslcvVGG3"
ALBUM = "spotify:album:1SN6N3fNkTefBwqrPfC5jr"
TRACK = "spotify:track:70b5Sq3ePOu3Gqg0hjlOtR"

QUEUED = {"status": "queued"}
TIMEOUT = {"error": "Sonos request timed out", "endpoint": "x"}
REFUSED = {"error": "Cannot reach Sonos API (node-sonos-http-api)", "endpoint": "x"}


class TestTheRepeatIsCollapsed:
    def test_a_second_identical_load_does_not_reach_sonos(self, dj, server_mod):
        with patch.object(dj, "_sonos_request", return_value=QUEUED) as sonos:
            dj._content_load("queue", PLAYLIST)
            second = dj._content_load("queue", PLAYLIST)
        sonos.assert_called_once()
        assert second["deduped"] is True

    def test_a_load_already_in_flight_is_reported_not_repeated(self, dj, server_mod):
        """The 46-second window where the first add is still running is
        precisely when an impatient second click lands."""
        server_mod._content_loads[("queue", PLAYLIST)] = {
            'at': server_mod.time.monotonic(), 'finished': None,
            'result': None, 'ambiguous': False,
        }
        with patch.object(dj, "_sonos_request") as sonos:
            result = dj._content_load("queue", PLAYLIST)
        sonos.assert_not_called()
        assert result["status"] == "already loading"

    def test_a_timeout_is_not_repeated(self, dj, server_mod):
        """The whole point: it may have landed anyway."""
        with patch.object(dj, "_sonos_request", return_value=TIMEOUT) as sonos:
            dj._content_load("queue", PLAYLIST)
            second = dj._content_load("queue", PLAYLIST)
        sonos.assert_called_once()
        assert second["deduped"] is True
        assert "may still be running" in second["error"]

    def test_albums_are_deduped_too(self, dj, server_mod):
        with patch.object(dj, "_sonos_request", return_value=QUEUED) as sonos:
            dj._content_load("queue", ALBUM)
            dj._content_load("queue", ALBUM)
        sonos.assert_called_once()


class TestWhatMustStillGetThrough:
    def test_a_refused_connection_can_be_retried_at_once(self, dj, server_mod):
        """Nothing reached the speaker, so there is nothing to duplicate --
        and the scheduler's retry depends on this."""
        with patch.object(dj, "_sonos_request", return_value=REFUSED) as sonos:
            dj._content_load("queue", PLAYLIST)
            dj._content_load("queue", PLAYLIST)
        assert sonos.call_count == 2

    def test_tracks_are_never_deduped(self, dj, server_mod):
        """A track add takes ~50ms and never times out. Refusing a second copy
        of the same song would cost something and prevent nothing."""
        with patch.object(dj, "_sonos_request", return_value=QUEUED) as sonos:
            dj._content_load("queue", TRACK)
            dj._content_load("queue", TRACK)
        assert sonos.call_count == 2

    def test_force_bypasses_the_dedupe(self, dj, server_mod):
        with patch.object(dj, "_sonos_request", return_value=QUEUED) as sonos:
            dj._content_load("queue", PLAYLIST)
            dj._content_load("queue", PLAYLIST, force=True)
        assert sonos.call_count == 2

    def test_different_playlists_do_not_collide(self, dj, server_mod):
        other = "spotify:playlist:08ID8VQlcnxWNbUrCAz2cq"
        with patch.object(dj, "_sonos_request", return_value=QUEUED) as sonos:
            dj._content_load("queue", PLAYLIST)
            dj._content_load("queue", other)
        assert sonos.call_count == 2

    def test_queueing_then_playing_the_same_uri_is_not_a_repeat(self, dj, server_mod):
        """Different actions are different intentions."""
        with patch.object(dj, "_sonos_request", return_value=QUEUED) as sonos:
            dj._content_load("queue", PLAYLIST)
            dj._content_load("now", PLAYLIST)
        assert sonos.call_count == 2

    def test_the_window_expires(self, dj, server_mod):
        with patch.object(dj, "_sonos_request", return_value=QUEUED) as sonos:
            dj._content_load("queue", PLAYLIST)
            later = server_mod.time.monotonic() + server_mod.CONTENT_DEDUP_SECONDS + 1
            with patch.object(server_mod.time, "monotonic", return_value=later):
                dj._content_load("queue", PLAYLIST)
        assert sonos.call_count == 2


class TestThroughThePublicEndpoint:
    def test_queue_collapses_a_double_click(self, dj, server_mod):
        with patch.object(dj, "_sonos_request", return_value=QUEUED) as sonos:
            dj.queue(uri=PLAYLIST)
            dj.queue(uri=PLAYLIST)
        sonos.assert_called_once()

    def test_force_is_readable_from_the_query_string(self, dj, server_mod):
        with patch.object(dj, "_sonos_request", return_value=QUEUED) as sonos:
            dj.queue(uri=PLAYLIST)
            dj.queue(uri=PLAYLIST, force="1")
        assert sonos.call_count == 2

    @pytest.mark.parametrize("value,expected", [
        (None, False), ("", True), ("1", True), ("true", True),
        ("yes", True), ("on", True), ("0", False), ("no", False),
    ])
    def test_the_flag_reading(self, server_mod, value, expected):
        assert server_mod._truthy(value) is expected
