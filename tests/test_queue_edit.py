"""Tests for queue reordering and removal.

Sonos queue indices are 1-based and shift as tracks finish or other clients
edit, so an index alone is not a safe address for a mutation. Every edit
carries the title the caller saw, and is refused if the queue moved.
"""
from unittest.mock import patch

import pytest
import requests

from paths import QUEUEEDIT_JS


QUEUE = [{"title": "Shampoo", "artist": "A"},
         {"title": "Hey", "artist": "B"},
         {"title": "Hours Last Stand", "artist": "C"}]


def _window(entries):
    """Stub _sonos_get_queue: honour limit/offset like the real thing."""
    def inner(limit, offset=0):
        return entries[offset:offset + limit]
    return inner


class TestMove:
    def test_moves_through_the_sonos_action(self, dj, server_mod):
        with patch.object(server_mod, "_sonos_get_queue", side_effect=_window(QUEUE)):
            with patch.object(dj, "_sonos_request", return_value={"ok": True}) as req:
                result = dj.queue_move(index=1, to=3, title="Shampoo")
        req.assert_called_once_with("queuemove/1/3")
        assert result["status"] == "moved"

    def test_same_position_is_a_no_op(self, dj, server_mod):
        with patch.object(dj, "_sonos_request") as req:
            assert dj.queue_move(index=2, to=2, title="Hey")["status"] == "unchanged"
        req.assert_not_called()

    def test_upstream_failure_is_returned(self, dj, server_mod):
        with patch.object(server_mod, "_sonos_get_queue", side_effect=_window(QUEUE)):
            with patch.object(dj, "_sonos_request",
                              return_value={"error": "down", "endpoint": "queuemove/1/3"}):
                assert "error" in dj.queue_move(index=1, to=3, title="Shampoo")

    @pytest.mark.parametrize("bad", ["0", "-1", "abc", ""])
    def test_bad_indices_rejected(self, dj, server_mod, bad):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            dj.queue_move(index=bad, to=2, title="x")
        assert exc.value.status == 400


class TestDriftGuard:
    def test_refuses_when_the_title_no_longer_matches(self, dj, server_mod):
        """The queue advanced or another client edited it since the drag
        began, so this index now points at a different track."""
        with patch.object(server_mod, "_sonos_get_queue", side_effect=_window(QUEUE)):
            with patch.object(dj, "_sonos_request") as req:
                with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                    dj.queue_move(index=1, to=3, title="Something Else")
        assert exc.value.status == 409
        req.assert_not_called()

    def test_refuses_a_remove_that_drifted(self, dj, server_mod):
        with patch.object(server_mod, "_sonos_get_queue", side_effect=_window(QUEUE)):
            with patch.object(dj, "_sonos_request") as req:
                with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                    dj.queue_remove(index=2, title="Not Hey")
        assert exc.value.status == 409
        req.assert_not_called()

    def test_index_past_the_end_is_400(self, dj, server_mod):
        with patch.object(server_mod, "_sonos_get_queue", side_effect=_window(QUEUE)):
            with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                dj.queue_remove(index=99, title="x")
        assert exc.value.status == 400

    def test_whitespace_does_not_trip_the_guard(self, dj, server_mod):
        with patch.object(server_mod, "_sonos_get_queue", side_effect=_window(QUEUE)):
            with patch.object(dj, "_sonos_request", return_value={"ok": True}):
                assert dj.queue_move(index=1, to=2, title="  Shampoo  ")["status"] == "moved"

    def test_no_title_supplied_skips_the_check(self, dj, server_mod):
        """The CLI has no title to offer; it opts out deliberately."""
        with patch.object(server_mod, "_sonos_get_queue", side_effect=_window(QUEUE)):
            with patch.object(dj, "_sonos_request", return_value={"ok": True}):
                assert dj.queue_move(index=1, to=2)["status"] == "moved"


class TestRemove:
    def test_removes_through_the_sonos_action(self, dj, server_mod):
        with patch.object(server_mod, "_sonos_get_queue", side_effect=_window(QUEUE)):
            with patch.object(dj, "_sonos_request", return_value={"ok": True}) as req:
                result = dj.queue_remove(index=2, title="Hey")
        req.assert_called_once_with("queueremove/2")
        assert result["title"] == "Hey"


class TestWindow:
    def test_returns_a_slice_and_the_position(self, dj, server_mod):
        with patch.object(server_mod, "_sonos_get_queue", side_effect=_window(QUEUE)):
            with patch.object(dj, "_sonos_request", return_value={"trackNo": 2}):
                result = dj.queue_window(offset=1, limit=2)
        assert [t["title"] for t in result["queue"]] == ["Hey", "Hours Last Stand"]
        assert result["track_no"] == 2

    def test_transport_failure_is_502_not_a_crash(self, dj, server_mod):
        with patch.object(server_mod, "_sonos_get_queue",
                          side_effect=requests.exceptions.ConnectionError("down")):
            result = dj.queue_window()
        assert "error" in result
        assert server_mod.cherrypy.response.status == 502

    def test_limit_is_bounded(self, dj, server_mod):
        with pytest.raises(server_mod.cherrypy.HTTPError):
            dj.queue_window(limit=99999)


class TestEndpointDiscipline:
    def test_mutations_are_post_only(self, server_mod):
        for name in ("queue_move", "queue_remove"):
            conf = getattr(getattr(server_mod.DJServer, name), "_cp_config", {})
            assert conf.get("tools.allow.methods") == ["POST"], name

    def test_not_public(self, server_mod):
        for path in ("/queue_move", "/queue_remove", "/queue_window"):
            assert path not in server_mod.PUBLIC_PATHS


class TestSonosActionPlugin:
    """The reorder/remove actions live in a plugin file because macOS grants
    Local Network access per process and the Python server does not have it."""

    def test_plugin_is_in_the_repo(self):
        import os
        assert os.path.isfile(QUEUEEDIT_JS), \
            "the plugin must be version-controlled here"
        source = open(QUEUEEDIT_JS).read()
        assert "registerAction('queuemove'" in source
        assert "registerAction('queueremove'" in source

    def test_plugin_accounts_for_the_insert_before_gap(self):
        """Moving a track down has to add one, because InsertBefore counts
        positions in the pre-move numbering."""
        source = open(QUEUEEDIT_JS).read()
        assert "to > from ? to + 1 : to" in source
