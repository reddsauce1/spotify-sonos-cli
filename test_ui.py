"""Tests for the extracted web UI and its HTML escaping.

Track, album, artist and playlist names come from Spotify and from the Sonos
queue. Anyone who can get a track into either -- a shared playlist, a guest
queueing a song -- controls text the UI used to concatenate straight into
innerHTML.
"""
import json
import os
import re
import shutil
import subprocess

import pytest



def extract_function(markup, name):
    """Return the source of a top-level JS function.

    Matches the closing brace at the same indentation as `function`, so the
    helper does not silently break when the file is re-indented -- which is
    exactly what happened when the UI was rewritten.
    """
    start = markup.index("function " + name + "(")
    line_start = markup.rfind("\n", 0, start) + 1
    indent = markup[line_start:start]
    close = markup.index("\n" + indent + "}", start)
    return markup[line_start:close + len(indent) + 2]


HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, 'static', 'index.html')


@pytest.fixture(scope="module")
def markup():
    with open(INDEX) as f:
        return f.read()


class TestExtraction:
    def test_index_html_exists(self, server_mod):
        assert os.path.isfile(server_mod.UI_INDEX_PATH)

    def test_server_points_at_it(self, server_mod):
        assert server_mod.UI_INDEX_PATH == INDEX
        assert server_mod.STATIC_DIR.endswith("static")

    def test_markup_is_a_complete_document(self, markup):
        assert markup.startswith("<!DOCTYPE html>")
        assert markup.rstrip().endswith("</html>")

    def test_no_ui_markup_left_in_server_py(self):
        """The point of the extraction: server.py should no longer carry the
        app's markup. The small login page stays inline deliberately, so it
        still renders if static/ is missing."""
        with open(os.path.join(HERE, 'server.py')) as f:
            source = f.read()
        assert "function escapeHtml" not in source
        assert "refreshNowPlaying" not in source
        assert source.count("<!DOCTYPE html>") == 1  # the login page only


class TestEscapingIsApplied:
    def test_helper_is_defined(self, markup):
        assert "function escapeHtml(" in markup

    def test_ampersand_is_escaped_first(self, markup):
        """If & is not replaced first, the escapes for < and " get double-
        encoded and the output is wrong."""
        body = markup.split("function escapeHtml(", 1)[1]
        order = [m.group(1) for m in re.finditer(r"\.replace\(/(.)/g", body[:600])]
        assert order[0] == "&", order

    @pytest.mark.parametrize("field", [
        "item.name", "item.artist", "item.title", "item.album",
        "data.title", "data.artist", "data.album", "data.artwork",
    ])
    def test_every_dynamic_field_is_escaped(self, markup, field):
        """No occurrence of these may be concatenated into markup raw."""
        pattern = re.compile(r"\+\s*" + re.escape(field) + r"\b")
        for line in markup.splitlines():
            if pattern.search(line) and "escapeHtml" not in line:
                pytest.fail(f"unescaped {field} in: {line.strip()}")

    def test_artwork_is_assigned_as_a_property_not_concatenated(self, markup):
        """Setting .src as a DOM property involves no HTML parsing, so an
        artwork URL cannot break out of an attribute at all -- stronger than
        escaping it into markup. Guard that nobody reverts to concatenation."""
        assert ".src = data.artwork" in markup
        for line in markup.splitlines():
            if "data.artwork" in line and "<img" in line:
                pytest.fail("artwork concatenated into markup: " + line.strip())


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestEscapingBehaviour:
    """Run the real function rather than trusting a grep."""

    @staticmethod
    def _run(payloads):
        with open(INDEX) as f:
            markup = f.read()
        fn = extract_function(markup, "escapeHtml")
        script = fn + "\nconsole.log(JSON.stringify(" + json.dumps(payloads) + ".map(escapeHtml)));"
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    @pytest.mark.parametrize("payload", [
        "<img src=x onerror=alert(1)>",
        '"><script>alert(1)</script>',
        "'; alert(1); //",
        "<svg/onload=alert(1)>",
        '" onmouseover="alert(1)',
    ])
    def test_payload_has_no_active_characters_left(self, payload):
        escaped, = self._run([payload])
        for ch in "<>\"'":
            assert ch not in escaped, f"{ch!r} survived in {escaped!r}"

    def test_ampersand_encoded_once(self):
        """AT&T must render as AT&T, not AT&amp;T, in the browser."""
        escaped, = self._run(["AT&T"])
        assert escaped == "AT&amp;T"

    def test_null_and_undefined_do_not_print_as_text(self):
        """Concatenating a missing field used to render the word 'undefined'."""
        assert self._run([None]) == [""]

    def test_ordinary_names_are_untouched(self):
        assert self._run(["Miles Davis"]) == ["Miles Davis"]


class TestStillAuthGated:
    def test_ui_stays_public_so_it_can_serve_the_login_form(self, server_mod):
        assert "/ui" in server_mod.PUBLIC_PATHS

    def test_static_dir_is_not_mounted_unauthenticated(self, server_mod):
        """Only /, /index, /ui and /login are reachable without credentials --
        adding a static mount must not quietly widen that."""
        assert server_mod.PUBLIC_PATHS == {'', '/index', '/ui', '/login'}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestJavaScriptIsValid:
    """The UI is ~340 lines of JS in a script block. A syntax error there
    breaks the entire page with nothing in the server log to show for it."""

    def test_script_block_parses(self, markup):
        script = markup.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        out = subprocess.run(["node", "--check", "-"], input=script,
                             capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr

    def test_every_handler_referenced_by_onclick_exists(self, markup):
        """An onclick naming a function that was renamed fails silently in the
        browser -- the button just does nothing."""
        called = set(re.findall(r'onclick="(\w+)\(', markup))
        called |= set(re.findall(r"onclick=\\'(\w+)\(", markup))
        defined = set(re.findall(r'function (\w+)\(', markup))
        missing = called - defined
        assert not missing, f"onclick references undefined function(s): {missing}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestWeeklyCalendar:
    """The calendar is rendered client-side from /schedules."""

    @staticmethod
    def _render(schedules, today_index=0):
        with open(INDEX) as f:
            markup = f.read()
        def fn(name):
            return extract_function(markup, name)

        script = (
            "const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];\n"
            "const ACTION_ICON = {play:'>',pause:'||',resume:'>',skip:'>|',"
            "previous:'|<',volume:'V',clearqueue:'X'};\n"
            + fn("escapeHtml") + "\n"
            "const _out = {};\n"
            "const document = {getElementById: () => ({ set innerHTML(v) { _out.html = v; },"
            " get innerHTML() { return _out.html; } })};\n"
            "const fetch = () => Promise.resolve({json: () => Promise.resolve("
            + json.dumps({"schedules": schedules}) + ")});\n"
            "const Date = class { getDay() { return " + str((today_index + 1) % 7) + "; } };\n"
            + fn("renderCalendar") + "\n"
            "renderCalendar();\n"
            "setTimeout(() => console.log(_out.html), 0);\n"
        )
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return out.stdout

    ROUTINE = {
        "id": "a", "time": "07:00", "days": [0, 1, 2, 3, 4], "label": "Wake-up",
        "enabled": True, "steps": [{"offset": 0, "action": "play", "uri": "spotify:playlist:x"}],
    }

    def test_marks_the_days_a_routine_runs(self):
        html = self._render([self.ROUTINE])
        rows = html.split("<tr>")[2]           # the 07:00 row
        cells = rows.split("<td")[2:]          # skip the time cell
        assert sum('class="hit"' in c for c in cells) == 5
        assert sum('class="miss"' in c for c in cells) == 2

    def test_empty_days_fills_the_whole_week(self):
        html = self._render([{**self.ROUTINE, "days": []}])
        assert html.count('class="hit"') == 7

    def test_disabled_routines_are_hidden(self):
        assert "Nothing enabled" in self._render([{**self.ROUTINE, "enabled": False}])

    def test_no_schedules_says_so(self):
        assert "Nothing enabled" in self._render([])

    def test_times_are_rows_in_order(self):
        html = self._render([
            {**self.ROUTINE, "time": "22:30", "id": "b"},
            self.ROUTINE,
        ])
        assert html.index("07:00") < html.index("22:30")

    def test_today_column_is_marked(self):
        """Monday-based: the scheduler uses 0=Mon, JS getDay() is 0=Sun."""
        html = self._render([self.ROUTINE], today_index=2)   # Wednesday
        headers = html.split("</tr>")[0]
        assert 'class="today">Wed' in headers

    def test_label_is_escaped_in_the_cell(self):
        html = self._render([{**self.ROUTINE, "label": '<img src=x onerror=alert(1)>'}])
        assert "<img" not in html.split("<table")[1]


class TestSplitLayout:
    """The layout's whole point: the page never scrolls and the transport
    controls are never navigated away from."""

    def test_body_does_not_scroll(self, markup):
        css = markup.split("<style>", 1)[1].split("</style>", 1)[0]
        # Match the `body {` rule specifically -- a naive split also matches
        # the earlier `html, body {` reset, which sets only height.
        rule = re.search(r"^\s*body\s*\{([^}]*)\}", css, re.MULTILINE)
        assert rule, "no body rule found"
        assert "overflow: hidden" in rule.group(1)

    def test_shell_fills_the_viewport(self, markup):
        assert "100dvh" in markup

    def test_player_is_a_sibling_of_the_panes_not_inside_one(self, markup):
        """If the player lived inside a pane it would disappear when you
        switched tabs, which is the thing this layout exists to prevent."""
        body = markup.split("<body>", 1)[1]
        player = body.index('class="player"')
        first_pane = body.index('class="pane"')
        assert player < first_pane
        # ...and it must not be nested inside the <main> that holds the panes.
        main_start = body.index("<main")
        assert player < main_start

    def test_transport_lives_in_the_player(self, markup):
        body = markup.split("<body>", 1)[1]
        player_block = body[body.index('class="player"'):body.index("</aside>")]
        for cmd in ("previous", "resume", "pause", "skip"):
            assert "quickCmd('" + cmd + "')" in player_block

    @pytest.mark.parametrize("pane", ["search", "queue", "lists", "sched"])
    def test_each_pane_has_a_tab(self, markup, pane):
        assert 'id="tab-' + pane + '"' in markup
        assert 'id="pane-' + pane + '"' in markup

    def test_only_one_pane_starts_active(self, markup):
        # Count in the markup only: the string also appears in the CSS
        # selector `.pane[data-active="true"]`.
        body = markup.split("<body>", 1)[1]
        assert body.count('data-active="true"') == 1

    def test_panes_scroll_internally(self, markup):
        """Scrolling belongs to the list, not the page."""
        assert ".pane-scroll" in markup
        css = markup.split("<style>", 1)[1].split("</style>", 1)[0]
        rule = css.split(".pane-scroll {", 1)[1].split("}", 1)[0]
        assert "overflow-y: auto" in rule

    def test_sidebar_appears_on_wide_screens(self, markup):
        assert 'grid-template-areas: "player content"' in markup

    def test_queue_marks_played_current_and_upcoming(self, markup):
        """track_no is what makes 'previously played' possible at all."""
        assert "data.track_no" in markup
        assert "row played" in markup
        assert "row current" in markup


class TestTracksAddressedByUri:
    """The server's numbered results are ONE global slot written by search,
    my(), recommend() and album_tracks(). A row number therefore means "row N
    of whatever wrote last", not "row N of what you can see. The UI must not
    depend on it."""

    def test_track_actions_send_a_uri(self, markup):
        assert "'/' + action + '?uri=' + encodeURIComponent(uri)" in markup

    def test_track_actions_do_not_send_a_row_number(self, markup):
        assert "?num=' + num" not in markup
        assert "action + '?num=" not in markup

    def test_add_to_playlist_sends_a_uri(self, markup):
        assert "uri: sheetTrackUri" in markup
        assert "num: sheetTrackNum" not in markup

    def test_the_nowplaying_poll_does_not_call_album_tracks(self, markup):
        """Polling album_tracks for artwork silently replaced the user's
        search results every 10 seconds.

        Scoped to the poll: expanding an album in the search results is a
        legitimate, user-initiated call to the same endpoint."""
        poll = markup.split("function refreshNowPlaying(", 1)[1].split("\n  }", 1)[0]
        # A call, not the word: the comment inside this function explains the
        # rule and names the endpoint.
        assert "fetch('/album_tracks" not in poll

    def test_album_expansion_is_user_initiated(self, markup):
        """The one place album_tracks is called is behind a click."""
        calls = [l for l in markup.splitlines() if "fetch('/album_tracks" in l]
        assert len(calls) == 1, calls
        expand = markup.split("function toggleAlbum(", 1)[1].split("\n  }", 1)[0]
        assert "album_tracks" in expand

    def test_artwork_comes_from_nowplaying(self, markup):
        np = markup.split("function refreshNowPlaying(", 1)[1].split("\n  }", 1)[0]
        assert "data.artwork" in np


class TestNowPlayingCarriesArtwork:
    def test_artwork_and_uri_are_returned(self, dj, server_mod):
        from unittest.mock import patch
        state = {
            "currentTrack": {
                "title": "The Trip", "artist": "Kim Fowley", "album": "X",
                "absoluteAlbumArtUri": "http://192.168.8.134:1400/getaa?x",
                "uri": "x-sonos-spotify:spotify%3atrack%3aabc",
            },
            "volume": 12, "playbackState": "PLAYING",
        }
        with patch.object(dj, "_sonos_request", return_value=state):
            result = dj._do_nowplaying()
        assert result["artwork"] == "http://192.168.8.134:1400/getaa?x"
        assert result["uri"].startswith("x-sonos-spotify")

    def test_missing_artwork_is_empty_not_absent(self, dj, server_mod):
        from unittest.mock import patch
        with patch.object(dj, "_sonos_request",
                          return_value={"currentTrack": {"title": "T"}, "volume": 1}):
            assert dj._do_nowplaying()["artwork"] == ""


class TestControlsAreReachableOnAPhone:
    """The player is the only place transport lives, and it is a compact bar
    on a phone. Anything hidden there is unreachable, not merely smaller."""

    CONTROLS = [".vol", ".transport", ".seek"]

    @pytest.mark.parametrize("selector", CONTROLS)
    def test_control_is_not_hidden_by_default(self, markup, selector):
        css = markup.split("<style>", 1)[1].split("</style>", 1)[0]
        rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        assert rule, f"no rule for {selector}"
        assert "display: none" not in rule.group(1), \
            f"{selector} is hidden by default, so it never appears on a phone"

    def test_volume_input_exists_in_the_player(self, markup):
        body = markup.split("<body>", 1)[1]
        player = body[body.index('class="player"'):body.index("</aside>")]
        assert 'id="vol"' in player
        assert 'type="range"' in player

    def test_volume_has_a_usable_touch_target(self, markup):
        """A slider a few pixels tall cannot be grabbed with a thumb."""
        css = markup.split("<style>", 1)[1].split("</style>", 1)[0]
        rule = re.search(r"\.vol input\s*\{([^}]*)\}", css).group(1)
        height = re.search(r"height:\s*(\d+)px", rule)
        assert height and int(height.group(1)) >= 24, rule


class TestChipsOnMobile:
    """Queue depth and how many routines are armed are worth seeing at a
    glance from a phone, not only from the sidebar."""

    def test_chips_are_not_hidden_by_default(self, markup):
        css = markup.split("<style>", 1)[1].split("</style>", 1)[0]
        rule = re.search(r"\.chips\s*\{([^}]*)\}", css).group(1)
        assert "display: none" not in rule

    def test_chips_lie_side_by_side_on_narrow_screens(self, markup):
        """Stacked, they would add two rows to a player that is already three
        deep on a phone."""
        css = markup.split("<style>", 1)[1].split("</style>", 1)[0]
        base = re.search(r"\.chips\s*\{([^}]*)\}", css).group(1)
        assert "flex-direction: row" in base

    def test_chips_stack_again_in_the_sidebar(self, markup):
        """The sidebar has height to spare and no width, so the wide layout
        wants the opposite arrangement to the phone's."""
        css = markup.split("<style>", 1)[1].split("</style>", 1)[0]
        override = re.search(
            r"@media \(min-width: 760px\) \{\s*\.chips \{([^}]*)\}", css)
        assert override, "no wide-screen rule for .chips"
        assert "flex-direction: column" in override.group(1)

    def test_long_chip_text_cannot_push_the_layout_wide(self, markup):
        """'Queue · playing 17879' next to '2 of 3 routines on' has to
        truncate, not overflow."""
        css = markup.split("<style>", 1)[1].split("</style>", 1)[0]
        rule = re.search(r"\.chip\s*\{([^}]*)\}", css).group(1)
        assert "text-overflow: ellipsis" in rule
        assert "min-width: 0" in rule
