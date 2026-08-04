"""Tests for the push path: Sonos webhook in, server-sent events out.

This replaced a 10-second poll that was roughly 90% of all traffic. The
properties that matter are the ones that protect the rest of the server: a
browser that stopped reading must not grow a queue without bound, and the
number of open streams must stay under the worker pool, because every stream
holds a CherryPy thread for as long as it is connected.
"""
import io
import json
import queue
from unittest.mock import patch

import cherrypy
import pytest


NOWPLAYING = {"title": "Shakin' All Over", "artist": "Eilen Jewell", "volume": 12}


@pytest.fixture(scope="module")
def markup():
    from paths import INDEX_HTML
    with open(INDEX_HTML) as f:
        return f.read()


@pytest.fixture
def post_event(dj, monkeypatch):
    """Deliver a webhook the way node-sonos-http-api does."""
    def _post(**body):
        monkeypatch.setattr(cherrypy.request, "body",
                            io.BytesIO(json.dumps(body).encode()), raising=False)
        with patch.object(dj, "_do_nowplaying", return_value=dict(NOWPLAYING)):
            return dj.sonos_event()
    return _post


class TestTheWebhook:
    def test_a_transport_change_is_broadcast(self, dj, server_mod, post_event):
        client = queue.Queue(maxsize=32)
        server_mod._stream_clients.append(client)
        result = post_event(type="transport-state", data={})
        assert result["clients"] == 1
        assert "Shakin" in client.get_nowait()

    def test_topology_change_is_ignored(self, dj, server_mod, post_event):
        """It fires on grouping and on discovery settling and says nothing
        about the track -- not worth waking every browser for."""
        client = queue.Queue(maxsize=32)
        server_mod._stream_clients.append(client)
        result = post_event(type="topology-change", data={})
        assert result["status"] == "ignored"
        assert client.empty()

    def test_volume_and_mute_changes_are_broadcast(self, dj, server_mod, post_event):
        for kind in ("volume-change", "mute-change"):
            assert post_event(type=kind, data={})["status"] == "broadcast"

    def test_the_payload_is_the_nowplaying_shape(self, dj, server_mod, post_event):
        """Rebuilt through _do_nowplaying rather than translated from the event
        body, so there is only one mapping to keep in step with the UI."""
        client = queue.Queue(maxsize=32)
        server_mod._stream_clients.append(client)
        post_event(type="transport-state", data={"irrelevant": True})
        payload = json.loads(client.get_nowait().removeprefix("data: ").strip())
        assert payload["title"] == NOWPLAYING["title"]
        assert payload["event"] == "transport-state"

    def test_a_non_json_body_is_a_400(self, dj, monkeypatch):
        monkeypatch.setattr(cherrypy.request, "body",
                            io.BytesIO(b"not json at all"), raising=False)
        with pytest.raises(cherrypy.HTTPError):
            dj.sonos_event()

    def test_it_counts_events(self, dj, server_mod, post_event):
        post_event(type="transport-state", data={})
        assert server_mod._metrics["events_received"] == 1

    def test_it_is_not_public(self, server_mod):
        """It is reachable through the tunnel like everything else."""
        assert "/sonos_event" not in server_mod.PUBLIC_PATHS


class TestBroadcastProtectsTheServer:
    def test_a_client_that_stopped_reading_does_not_block_the_webhook(self, server_mod):
        """A laptop that slept with the tab open. Its events are dropped; the
        delivery to everyone else must still happen."""
        stalled = queue.Queue(maxsize=1)
        stalled.put_nowait("already full")
        healthy = queue.Queue(maxsize=32)
        server_mod._stream_clients.extend([stalled, healthy])

        delivered = server_mod._broadcast({"title": "x"})

        assert delivered == 1
        assert not healthy.empty()

    def test_broadcasting_to_nobody_is_fine(self, server_mod):
        assert server_mod._broadcast({"title": "x"}) == 0

    def test_the_payload_is_sse_framed(self, server_mod):
        client = queue.Queue(maxsize=32)
        server_mod._stream_clients.append(client)
        server_mod._broadcast({"title": "x"})
        message = client.get_nowait()
        assert message.startswith("data: ")
        assert message.endswith("\n\n")


class TestTheClientCap:
    def test_it_refuses_past_the_cap(self, dj, server_mod):
        """Every stream holds a worker thread. Exhausting the pool would stall
        every request, not just the streams."""
        server_mod._stream_clients.extend(
            queue.Queue() for _ in range(server_mod.MAX_STREAM_CLIENTS))
        with pytest.raises(cherrypy.HTTPError) as excinfo:
            dj.stream()
        assert excinfo.value.status == 503

    def test_the_cap_is_below_the_worker_pool(self, server_mod):
        """Otherwise the streams alone can starve the server of workers."""
        assert server_mod.MAX_STREAM_CLIENTS < server_mod.DEFAULTS['server_thread_pool']

    def test_streaming_is_switched_on_for_the_handler(self, server_mod):
        """Without response.stream CherryPy buffers the whole generator, and
        the browser gets nothing until the connection closes."""
        assert server_mod.DJServer.stream._cp_config['response.stream'] is True


class TestTheBrowserSide:
    def test_it_opens_a_stream(self, markup):
        assert "new EventSource('/stream')" in markup

    def test_polling_survives_as_a_fallback(self, markup):
        """An EventSource can fail for reasons the page cannot fix. A silent
        player is worse than a chatty one."""
        assert "startFallbackPolling" in markup
        assert "setInterval(refreshNowPlaying, 10000)" in markup

    def test_the_fallback_stops_once_the_stream_opens(self, markup):
        assert "source.onopen = () => stopFallbackPolling()" in markup

    def test_an_error_restarts_polling(self, markup):
        assert "source.onerror" in markup

    def test_both_paths_paint_through_one_function(self, markup):
        """The polled response and the pushed event must not grow separate
        painters."""
        assert "paintNowPlaying(JSON.parse(event.data))" in markup
        assert "then(paintNowPlaying)" in markup
