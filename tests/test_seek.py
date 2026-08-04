"""Tests for the seek bar.

Position is polled every ten seconds, so most of the difficulty is on the
client: the bar has to advance between polls without stuttering, and must not
be yanked out from under a finger mid-drag.
"""
import json
import os
import shutil
import subprocess

import pytest
from unittest.mock import patch

from paths import INDEX_HTML as INDEX




@pytest.fixture(scope="module")
def markup():
    with open(INDEX) as f:
        return f.read()


class TestServer:
    def test_nowplaying_reports_position_and_length(self, dj, server_mod):
        state = {"currentTrack": {"title": "T", "duration": 157},
                 "elapsedTime": 7, "volume": 12, "playbackState": "PLAYING"}
        with patch.object(dj, "_sonos_request", return_value=state):
            result = dj._do_nowplaying()
        assert result["elapsed"] == 7
        assert result["duration"] == 157

    def test_error_response_keeps_the_same_shape(self, dj, server_mod):
        """A client reading data.duration must not get undefined when Sonos is
        down -- that turns into NaN in the bar."""
        with patch.object(dj, "_sonos_request",
                          return_value={"error": "down", "endpoint": "state"}):
            result = dj._do_nowplaying()
        for key in ("elapsed", "duration", "artwork", "uri"):
            assert key in result, key

    def test_seek_sends_bare_seconds(self, dj, server_mod):
        """Sonos returns 500 for HH:MM:SS on timeseek; it wants an integer."""
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as req:
            result = dj.seek(to=42)
        req.assert_called_once_with("timeseek/42")
        assert result["to"] == 42

    @pytest.mark.parametrize("bad", ["abc", "-1", "", None, "1:20"])
    def test_bad_positions_rejected(self, dj, server_mod, bad):
        with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
            dj.seek(to=bad)
        assert exc.value.status == 400

    def test_upstream_failure_is_returned(self, dj, server_mod):
        with patch.object(dj, "_sonos_request",
                          return_value={"error": "down", "endpoint": "timeseek/5"}):
            assert "error" in dj.seek(to=5)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestClientTicking:
    """Run the real functions rather than trusting a reading of them."""

    @staticmethod
    def _run(script_body):
        with open(INDEX) as f:
            markup = f.read()

        def fn(name):
            start = markup.index("function " + name + "(")
            line_start = markup.rfind("\n", 0, start) + 1
            indent = markup[line_start:start]
            close = markup.index("\n" + indent + "}", start)
            return markup[line_start:close + len(indent) + 2]

        seek_const = markup[markup.index("const SEEK ="):markup.index("\n", markup.index("const SEEK ="))]
        els = {}
        stub = """
        const _els = {};
        const document = { getElementById: (id) => (_els[id] = _els[id] || {
            dataset: {}, style: {}, textContent: '', value: 0, max: 0,
        })};
        """
        script = stub + seek_const + "\n" + fn("fmtTime") + "\n" + fn("syncSeek") + "\n" \
            + fn("currentElapsed") + "\n" + fn("paintSeek") + "\n" \
            + fn("seekBarInput") + "\n" + script_body
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    def test_advances_between_polls_while_playing(self):
        out = self._run("""
            syncSeek({elapsed: 10, duration: 200, playbackState: 'PLAYING'});
            SEEK.readAt = Date.now() - 5000;      // pretend 5s have passed
            console.log(Math.round(currentElapsed()));
        """)
        assert out == "15", out

    def test_does_not_advance_while_paused(self):
        out = self._run("""
            syncSeek({elapsed: 10, duration: 200, playbackState: 'PAUSED_PLAYBACK'});
            SEEK.readAt = Date.now() - 5000;
            console.log(Math.round(currentElapsed()));
        """)
        assert out == "10", out

    def test_never_runs_past_the_end_of_the_track(self):
        out = self._run("""
            syncSeek({elapsed: 195, duration: 200, playbackState: 'PLAYING'});
            SEEK.readAt = Date.now() - 60000;     // a minute of drift
            console.log(Math.round(currentElapsed()));
        """)
        assert out == "200", out

    def test_a_poll_does_not_move_the_handle_mid_drag(self):
        """Without the guard the bar jumps out from under your finger.

        The handle should hold whatever it showed when the drag began (10),
        NOT advance to where playback has since reached (15).
        """
        out = self._run("""
            syncSeek({elapsed: 10, duration: 200, playbackState: 'PLAYING'});
            seekBarInput(120);                    // user starts dragging
            SEEK.readAt = Date.now() - 5000;      // 5s of playback elapses
            paintSeek();                          // a poll lands mid-drag
            console.log(_els['seek-bar'].value);
        """)
        assert out == "10", f"handle moved during a drag (value={out})"

    def test_idle_when_there_is_no_track(self):
        out = self._run("""
            syncSeek({elapsed: 0, duration: 0, playbackState: 'STOPPED'});
            console.log(_els['seek'].dataset.idle);
        """)
        assert out == "true"

    @pytest.mark.parametrize("seconds,expected", [
        (0, "0:00"), (7, "0:07"), (65, "1:05"), (157, "2:37"), (3600, "60:00"),
    ])
    def test_time_formatting(self, seconds, expected):
        assert self._run(f"console.log(fmtTime({seconds}));") == expected

    def test_negative_or_nan_does_not_render_garbage(self):
        assert self._run("console.log(fmtTime(-5));") == "0:00"
        assert self._run("console.log(fmtTime(NaN));") == "0:00"


class TestWiring:
    def test_bar_is_in_the_player_not_a_pane(self, markup):
        """It must stay visible whichever tab is open."""
        body = markup.split("<body>", 1)[1]
        assert body.index('id="seek"') < body.index("<main")

    def test_poll_feeds_the_bar(self, markup):
        np = markup.split("function paintNowPlaying(", 1)[1].split("\n  }", 1)[0]
        assert "syncSeek(data)" in np

    def test_ticks_once_a_second(self, markup):
        assert "setInterval(paintSeek, 1000)" in markup

    def test_commit_happens_on_change_not_input(self, markup):
        """oninput fires continuously while dragging; seeking on every pixel
        would flood Sonos."""
        line = next(l for l in markup.splitlines() if 'id="seek-bar"' in l or 'oninput=' in l)
        assert 'oninput="seekBarInput' in markup
        assert 'onchange="seekBarCommit' in markup
