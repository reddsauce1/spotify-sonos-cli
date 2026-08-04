"""Tests for fine volume control and the shuffle toggle.

Two reporting problems sit behind both. The volume readout only updated when
the slider was released, so you could not tell how far you had moved it until
you let go, and there was no way to move by exactly one. And Sonos reports
play mode about a second and a half behind the command that changed it, so a
control that reads state back shows the user the setting they just turned off.
"""
import re
from unittest.mock import patch

import pytest

from paths import INDEX_HTML


@pytest.fixture(scope="module")
def markup():
    with open(INDEX_HTML) as handle:
        return handle.read()


@pytest.fixture(scope="module")
def js(markup):
    return markup.split("<script>", 1)[1].split("</script>", 1)[0]


@pytest.fixture(scope="module")
def css(markup):
    return markup.split("<style>", 1)[1].split("</style>", 1)[0]


STATE = {
    "volume": 16,
    "playbackState": "PLAYING",
    "playMode": {"repeat": "none", "shuffle": False, "crossfade": False},
    "currentTrack": {"title": "t", "artist": "a", "album": "b", "duration": 200},
    "elapsedTime": 10,
}


class TestARelativeChangeReportsWhereItLanded:
    """Nudging by one is useless if the caller cannot see the result."""

    def test_the_resulting_level_comes_back(self, dj):
        with patch.object(dj, "_sonos_request",
                          return_value={"status": "success", "newVolume": 17}):
            assert dj._do_volume(change="+1")["level"] == 17

    def test_it_reports_the_clamped_value_not_the_arithmetic(self, dj):
        """+5 from 98 lands on 100, and the UI must show 100."""
        with patch.object(dj, "_sonos_request",
                          return_value={"status": "success", "newVolume": 100}):
            assert dj._do_volume(change="+5")["level"] == 100

    def test_the_change_is_still_reported(self, dj):
        with patch.object(dj, "_sonos_request",
                          return_value={"status": "success", "newVolume": 15}):
            assert dj._do_volume(change="-1")["change"] == "-1"

    def test_an_unreported_level_does_not_lose_the_change(self, dj):
        """The volume did move; only the confirmation is missing."""
        with patch.object(dj, "_sonos_request",
                          return_value={"status": "success", "newVolume": None}):
            result = dj._do_volume(change="+1")
        assert result["status"] == "volume adjusted"
        assert "level" not in result

    def test_a_failed_change_is_still_an_error(self, dj):
        with patch.object(dj, "_sonos_request",
                          return_value={"error": "down", "endpoint": "volume"}):
            assert "error" in dj._do_volume(change="+1")

    def test_setting_an_absolute_level_is_unchanged(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as sonos:
            assert dj._do_volume(level=20)["level"] == 20
            sonos.assert_called_once_with("volume/20")

    @pytest.mark.parametrize("bad", ["abc", "+999", "--1", "1;rm", "+1.5"])
    def test_a_bad_change_is_rejected_before_reaching_sonos(self, dj, server_mod, bad):
        with patch.object(server_mod, "requests") as net:
            with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                dj._do_volume(change=bad)
        assert exc.value.status == 400
        net.get.assert_not_called()


class TestShuffle:
    def test_reading_it(self, dj):
        with patch.object(dj, "_sonos_request", return_value=STATE):
            assert dj._do_shuffle()["shuffle"] is False

    def test_reading_it_when_on(self, dj):
        on = {**STATE, "playMode": {"shuffle": True}}
        with patch.object(dj, "_sonos_request", return_value=on):
            assert dj._do_shuffle()["shuffle"] is True

    def test_a_player_with_no_play_mode_reads_as_off(self, dj):
        with patch.object(dj, "_sonos_request", return_value={"volume": 5}):
            assert dj._do_shuffle()["shuffle"] is False

    @pytest.mark.parametrize("given,path,expected", [
        ("on", "shuffle/on", True),
        ("off", "shuffle/off", False),
        ("ON", "shuffle/on", True),
        ("true", "shuffle/on", True),
        ("false", "shuffle/off", False),
        ("1", "shuffle/on", True),
        ("0", "shuffle/off", False),
        (" on ", "shuffle/on", True),
    ])
    def test_setting_it(self, dj, given, path, expected):
        with patch.object(dj, "_sonos_request", return_value={"status": "success"}) as sonos:
            assert dj._do_shuffle(state=given)["shuffle"] is expected
            sonos.assert_called_once_with(path)

    def test_the_reply_is_trusted_over_a_state_read(self, dj):
        """Play mode in /state lags ~1.5s. Reading it back would report the
        old value and flip the toggle in the user's face."""
        with patch.object(dj, "_sonos_request",
                          return_value={"status": "success"}) as sonos:
            assert dj._do_shuffle(state="on")["shuffle"] is True
            assert sonos.call_count == 1, "must not re-read the lagging state"

    @pytest.mark.parametrize("bad", ["maybe", "toggle", "2", "", "on;rm -rf /", "../pause"])
    def test_a_bad_state_never_reaches_sonos(self, dj, server_mod, bad):
        """The value is interpolated into the Sonos path."""
        with patch.object(dj, "_sonos_request") as sonos:
            with pytest.raises(server_mod.cherrypy.HTTPError) as exc:
                dj._do_shuffle(state=bad)
        assert exc.value.status == 400
        sonos.assert_not_called()

    def test_an_upstream_failure_is_a_502(self, dj, server_mod):
        import requests as rq
        with patch.object(server_mod.requests, "get",
                          side_effect=rq.exceptions.ConnectionError("down")):
            assert "error" in dj._do_shuffle(state="on")
        assert server_mod.cherrypy.response.status == 502

    def test_it_is_reported_by_nowplaying(self, dj):
        on = {**STATE, "playMode": {"shuffle": True}}
        with patch.object(dj, "_sonos_request", return_value=on):
            assert dj._do_nowplaying()["shuffle"] is True

    def test_nowplaying_reports_it_even_when_sonos_is_down(self, dj):
        """The UI reads the field unconditionally; a missing key would leave
        the toggle showing whatever it last showed."""
        with patch.object(dj, "_sonos_request",
                          return_value={"error": "down", "endpoint": "state"}):
            assert dj._do_nowplaying()["shuffle"] is False

    def test_the_endpoint_exists(self, server_mod):
        assert hasattr(server_mod.DJServer, "shuffle")

    def test_it_is_not_public(self, server_mod):
        assert "/shuffle" not in server_mod.PUBLIC_PATHS


class TestTheReadoutMovesWhileYouDrag:
    def test_input_updates_the_number(self, markup):
        """oninput fires continuously; onchange only on release."""
        block = markup[markup.index('class="vol"'):markup.index('class="chips"')]
        assert 'oninput="previewVolume(this.value)"' in block

    def test_release_is_what_sends(self, markup):
        block = markup[markup.index('class="vol"'):markup.index('class="chips"')]
        assert 'onchange="setVolume(this.value)"' in block

    def test_dragging_does_not_flood_sonos(self, js):
        body = re.search(r"function previewVolume\(level\) \{.*?\n  \}", js, re.S).group(0)
        assert "fetch" not in body

    def test_the_slider_steps_by_one(self, markup):
        block = markup[markup.index('class="vol"'):markup.index('class="chips"')]
        assert 'step="1"' in block


class TestNudgingByExactlyOne:
    def test_both_directions_exist(self, markup):
        block = markup[markup.index('class="vol"'):markup.index('class="chips"')]
        assert "nudgeVolume(-1)" in block
        assert "nudgeVolume(1)" in block

    def test_it_uses_the_relative_endpoint(self, js):
        body = re.search(r"function nudgeVolume\(step\) \{.*?\n  \}", js, re.S).group(0)
        assert "'/volume?change='" in body

    def test_the_sign_is_sent_explicitly(self, js):
        """Sonos wants +1, not 1; a bare 1 is not a relative change."""
        body = re.search(r"function nudgeVolume\(step\) \{.*?\n  \}", js, re.S).group(0)
        assert "(step > 0 ? '+' : '')" in body

    def test_the_plus_is_url_encoded(self, js):
        """A raw + in a query string decodes to a space."""
        body = re.search(r"function nudgeVolume\(step\) \{.*?\n  \}", js, re.S).group(0)
        assert "encodeURIComponent" in body

    def test_it_moves_immediately_then_corrects_from_the_reply(self, js):
        body = re.search(r"function nudgeVolume\(step\) \{.*?\n  \}", js, re.S).group(0)
        assert "paintVolume(Math.min(100, Math.max(0, (+bar.value) + step)))" in body
        assert "paintVolume(data.level)" in body

    def test_the_buttons_are_thumb_sized(self, css):
        rule = re.search(r"\.vbtn \{([^}]*)\}", css).group(1)
        assert int(re.search(r"width: (\d+)px", rule).group(1)) >= 24
        assert int(re.search(r"height: (\d+)px", rule).group(1)) >= 24


class TestThePollCannotFightTheUser:
    def test_volume_is_left_alone_while_being_adjusted(self, js):
        assert "!volumeIsBusy()" in js

    def test_the_guard_expires_on_its_own(self, js):
        """A boolean set on drag-start can stick if the matching event never
        arrives, and then the slider stops tracking Sonos entirely."""
        body = re.search(r"function volumeIsBusy\(\) \{.*?\}", js, re.S).group(0)
        assert "Date.now() - VOL.touchedAt" in body

    def test_shuffle_waits_out_the_lag(self, js):
        assert "Date.now() - SHUF.touchedAt > 2500" in js

    def test_the_shuffle_window_outlasts_the_lag_it_covers(self, js):
        """Sonos takes about 1500ms to report the new play mode."""
        window = int(re.search(r"SHUF\.touchedAt > (\d+)", js).group(1))
        assert window > 1500


class TestTheShuffleButtonShowsItsState:
    def test_it_is_a_toggle_to_a_screen_reader(self, markup, js):
        assert 'aria-pressed="false"' in markup
        assert "btn.setAttribute('aria-pressed'" in js

    def test_it_is_visually_distinct_when_on(self, css):
        assert ".vbtn.shuf.on" in css

    def test_a_failure_puts_the_button_back(self, js):
        """Optimistic paint has to be undone, or the button lies."""
        body = re.search(r"function toggleShuffle\(\) \{.*?\n  \}", js, re.S).group(0)
        assert body.count("paintShuffle(!wanted)") == 2, "both the error and the reject path"

    def test_it_follows_changes_made_elsewhere(self, js):
        assert "paintShuffle(data.shuffle)" in js


class TestTheNumberDoesNotShoveTheSlider:
    def test_the_readout_has_a_fixed_width(self, css):
        """It changes on every drag frame now, so 9 -> 10 -> 100 would jitter
        the slider sideways under the user's finger."""
        rule = re.search(r"\.vol \.num \{([^}]*)\}", css, re.S).group(1)
        assert "min-width" in rule
        assert "tabular-nums" in rule

    def test_the_buttons_do_not_shrink(self, css):
        rule = re.search(r"\.vbtn \{([^}]*)\}", css, re.S).group(1)
        assert "flex: none" in rule


class TestRelativeVolumeIsAppliedBySpeaker:
    """node's own `volume` action resolves "+3" in JavaScript against a cached
    volume and writes an absolute value.

    That read-modify-write was NOT observed to lose updates -- five concurrent
    nudges moved the volume by five -- because node is single-threaded and
    updates its cache before the SOAP call. relvolume is kept for the two
    things that are demonstrable: the speaker applies the delta, so no cached
    value participates at all, and it returns where it landed, which removes
    the second round trip.
    """

    def test_it_uses_the_speaker_applied_endpoint(self, dj):
        with patch.object(dj, "_sonos_request",
                          return_value={"status": "success", "newVolume": 21}) as sonos:
            dj._do_volume(change="+1")
        endpoint = sonos.call_args_list[0].args[0]
        assert endpoint == "relvolume/+1"
        assert not endpoint.startswith("volume/")

    def test_an_absolute_set_still_uses_the_plain_endpoint(self, dj):
        """Absolute writes were never racey; only the read-modify-write was."""
        with patch.object(dj, "_sonos_request", return_value={"ok": True}) as sonos:
            dj._do_volume(level=20)
        sonos.assert_called_once_with("volume/20")

    def test_it_costs_one_round_trip_not_two(self, dj):
        """The speaker returns where it landed, so the follow-up state read
        that used to confirm it is gone."""
        with patch.object(dj, "_sonos_request",
                          return_value={"status": "success", "newVolume": 21}) as sonos:
            dj._do_volume(change="+1")
        assert sonos.call_count == 1

    def test_the_sign_is_preserved_for_the_speaker(self, dj):
        with patch.object(dj, "_sonos_request",
                          return_value={"status": "success", "newVolume": 5}) as sonos:
            dj._do_volume(change="-3")
        assert sonos.call_args_list[0].args[0] == "relvolume/-3"

    def test_the_custom_action_ships_with_the_repo(self):
        """relvolume is not upstream. If the file is missing from
        sonos-actions/, a fresh install silently loses relative volume."""
        import os
        from paths import SERVER_PY
        action = os.path.join(os.path.dirname(SERVER_PY), "sonos-actions", "relvolume.js")
        assert os.path.isfile(action)
        with open(action) as handle:
            source = handle.read()
        assert "registerAction('relvolume'" in source
        assert "SetRelativeVolume" in source
