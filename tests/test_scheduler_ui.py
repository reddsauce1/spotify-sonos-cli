"""Tests for the rebuilt scheduler UI.

The old flow had two composers for one idea -- a form that created a routine
plus an invisible first step, and a second form inside each card for every
step after that -- no way to edit anything once created, offsets shown as
"+75m", and no indication anywhere of when a routine would next fire. The
last of those is how a wake-up alarm ran Sunday-only for weeks unnoticed.
"""
import json
import re
import shutil
import subprocess

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


class TestTheOldTwoFormFlowIsGone:
    @pytest.mark.parametrize("dead", [
        "buildDayPicker", "schedActionChanged", "addSchedule",
        "maybePasteUri", "toggleStepForm",
    ])
    def test_dead_helper_is_removed(self, markup, dead):
        assert dead not in markup

    @pytest.mark.parametrize("dead_id", [
        "sched-time", "sched-label", "sched-days", "sched-action",
        "sched-uri", "sched-volume",
    ])
    def test_the_standalone_create_form_is_gone(self, markup, dead_id):
        assert 'id="%s"' % dead_id not in markup

    def test_there_is_no_second_per_card_step_form(self, js):
        """Steps are composed in the editor now, not inline in each routine."""
        assert "addstep-" not in js
        assert "stepoff-" not in js


class TestOneComposerForCreateAndEdit:
    def test_the_editor_exists(self, markup):
        assert 'id="editor"' in markup
        assert 'id="ed-body"' in markup

    def test_creating_and_editing_use_the_same_function(self, js):
        assert "function openEditor(routine, prefill)" in js
        assert re.search(r"function editRoutine\(id\) \{.*?openEditor\(routine, null\)",
                         js, re.S)

    def test_the_title_says_which_one_you_are_doing(self, js):
        assert re.search(r"EDITOR\.id \? 'Edit routine' : 'New routine'", js)

    def test_saving_goes_to_the_one_atomic_endpoint(self, js):
        assert "postJson('/schedule_save', payload)" in js

    def test_it_no_longer_calls_the_incremental_endpoints(self, js):
        assert "/schedule_step_add" not in js
        assert "/schedule_step_delete" not in js
        assert "/schedule_add" not in js

    def test_the_id_is_only_sent_when_editing(self, js):
        """Sending id: null on a create would 400 as an unknown schedule."""
        assert "if (EDITOR.id) payload.id = EDITOR.id;" in js

    def test_routines_can_be_edited_from_the_list(self, js):
        assert "editRoutine(" in js


class TestNothingSavesUntilSave:
    def test_the_editor_holds_a_draft(self, js):
        assert re.search(r"let EDITOR = null", js)

    def test_cancel_discards_it(self, js):
        assert re.search(r"function closeEditor\(\) \{\s*EDITOR = null", js)

    def test_escape_closes_it(self, js):
        assert "e.key === 'Escape'" in js

    def test_step_edits_only_touch_the_draft(self, js):
        """Removing a step must not fire a request the way it used to."""
        body = re.search(r"function editorRemoveStep\(index\) \{.*?\n  \}", js, re.S).group(0)
        assert "fetch" not in body and "post" not in body


class TestStepsShowWallClockNotOffsets:
    def test_the_editor_edits_a_time_not_a_minute_count(self, js):
        row = re.search(r"function editorStepRow\(step, index\) \{.*?\n  \}", js, re.S).group(0)
        assert 'type="time"' in row
        assert "placeholder=\"+min\"" not in row

    def test_the_list_renders_the_server_computed_time(self, js):
        routine = re.search(r"function renderRoutine\(s\) \{.*?\n  \}", js, re.S).group(0)
        assert "step.at" in routine
        assert "'m'" not in routine, "no leftover +75m rendering"

    def test_times_convert_back_to_offsets_on_save(self, js):
        assert "offset: stepOffset(step.at, trigger)" in js
        body = re.search(r"function stepOffset\(at, triggerMinutes\) \{.*?\n  \}",
                         js, re.S).group(0)
        assert "% 1440 + 1440) % 1440" in body

    def test_a_step_before_the_trigger_is_marked_next_day(self, js):
        assert "const wraps = toMinutes(step.at) < toMinutes(EDITOR.time);" in js
        assert '<span class="tag">next day</span>' in js

    def test_moving_the_start_time_moves_the_whole_routine(self, js):
        """Otherwise shifting a wake-up by 30 minutes means editing every step."""
        body = re.search(r"function editorTimeChanged\(value\) \{.*?\n  \}", js, re.S).group(0)
        assert "const delta = toMinutes(value) - toMinutes(EDITOR.time);" in body
        assert "step.at = fromMinutes(toMinutes(step.at) + delta)" in body


class TestAStepEarlierThanTheStartReanchors:
    """Setting a step to 06:00 in a routine anchored at 07:00 used to wrap it
    to 06:00 *next day* -- offset 1380, which the server then rejected at its
    720-minute cap with a bare "offset must be between 0 and 720". The editor
    now reads a time more than 12h "after" the start as "this routine starts
    earlier" and moves the start back to it instead."""

    def test_the_cap_matches_the_server(self, js):
        """The JS constant mirrors max_step_offset_minutes; if the server's
        cap moves, this is the drift alarm."""
        import server
        cap = int(re.search(r"const MAX_STEP_OFFSET_MIN = (\d+);", js).group(1))
        assert cap == server.MAX_OFFSET_MINUTES

    def test_the_save_guard_speaks_plainly(self, js):
        """If a draft still exceeds the cap, the toast should explain it in
        hours, not surface the server's offset-range 400."""
        body = re.search(r"function saveEditor\(\) \{.*?\n  \}", js, re.S).group(0)
        assert "Steps span more than 12 hours" in body
        assert re.search(r"showToast\('❌ Steps span[^']*'\);\s*return;", body)

    def test_reanchoring_does_not_go_through_editorTimeChanged(self, js):
        """editorTimeChanged shifts every step along with the start -- the
        right gesture for 'move my wake-up 30 minutes', the wrong one for
        'this one step is earlier'."""
        body = re.search(r"function editorStepField\(index, field, value\) \{.*?\n  \}",
                         js, re.S).group(0)
        assert "EDITOR.time = value;" in body
        assert "editorTimeChanged(" not in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestReanchorBehaviour:
    """Run the real editorStepField rather than trusting a grep."""

    @staticmethod
    def _edit_step_at(editor, index, value):
        with open(INDEX_HTML) as handle:
            markup = handle.read()

        def grab(name):
            start = markup.index("function " + name + "(")
            line_start = markup.rfind("\n", 0, start) + 1
            indent = markup[line_start:start]
            close = markup.index("\n" + indent + "}", start)
            return markup[line_start:close + len(indent) + 2]

        def grab_const(name):
            return re.search(r"const %s = [^\n]+;" % name, markup).group(0)

        script = (
            "function renderEditor() {}\n"
            "let EDITOR = " + json.dumps(editor) + ";\n"
            + grab_const("HHMM_RE") + "\n"
            + grab_const("MAX_STEP_OFFSET_MIN") + "\n"
            + grab("toMinutes") + "\n"
            + grab("stepOffset") + "\n"
            + grab("editorStepField") + "\n"
            + "editorStepField(%d, 'at', %s);\n" % (index, json.dumps(value))
            + "console.log(JSON.stringify(EDITOR));"
        )
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def _editor(self, time, ats):
        return {"id": None, "label": "", "time": time,
                "days": [0, 1, 2, 3, 4], "enabled": True,
                "steps": [{"at": at, "action": "play", "uri": "", "volume": ""}
                          for at in ats]}

    def test_an_earlier_step_pulls_the_start_back(self):
        after = self._edit_step_at(self._editor("07:00", ["07:00", "07:00"]), 1, "06:00")
        assert after["time"] == "06:00"
        assert after["steps"][1]["at"] == "06:00"

    def test_the_other_steps_keep_their_wall_clock_times(self):
        """The whole point of bypassing editorTimeChanged: pulling one step
        earlier must not drag a 07:00 step back to 06:00 with it."""
        after = self._edit_step_at(self._editor("07:00", ["07:00", "07:00"]), 1, "06:00")
        assert after["steps"][0]["at"] == "07:00"

    def test_a_genuine_past_midnight_step_is_left_alone(self):
        """23:45 start with a 00:15 step wraps by 30 minutes -- well under
        the cap, so it stays a next-day step and the start does not move."""
        after = self._edit_step_at(self._editor("23:45", ["23:45"]), 0, "00:15")
        assert after["time"] == "23:45"
        assert after["steps"][0]["at"] == "00:15"

    def test_a_step_within_the_cap_does_not_reanchor(self):
        after = self._edit_step_at(self._editor("07:00", ["07:00"]), 0, "19:00")
        assert after["time"] == "07:00"


class TestPlayStepsHaveAnUntil:
    """An event that ends used to be a concept that lived only in Len's head
    -- ending one meant remembering to schedule a pause by hand, which is why
    routines piled onto each other."""

    def test_the_row_offers_an_until_for_play_steps(self, js):
        row = re.search(r"function editorStepRow\(step, index\) \{.*?\n  \}", js, re.S).group(0)
        assert 'aria-label="Stop at"' in row
        assert "step.action === 'play'" in row

    def test_moving_the_start_time_moves_untils_too(self, js):
        """Shifting a wake-up 30 minutes must shift when it ends, or the
        routine silently changes length."""
        body = re.search(r"function editorTimeChanged\(value\) \{.*?\n  \}", js, re.S).group(0)
        assert "step.until = fromMinutes(toMinutes(step.until) + delta)" in body

    def test_a_new_step_starts_when_the_previous_one_ends(self, js):
        body = re.search(r"function editorAddStep\(\) \{.*?\n  \}", js, re.S).group(0)
        assert "last.until || last.at" in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestUntilCompilesToAPauseStep:
    """'until' is editor sugar over an ordinary pause step: saveEditor writes
    the pause, foldUntils reads it back, and the server never learns a new
    concept. Run the real functions rather than trusting a grep."""

    @staticmethod
    def _run(script_tail):
        with open(INDEX_HTML) as handle:
            markup = handle.read()

        def grab(name):
            start = markup.index("function " + name + "(")
            line_start = markup.rfind("\n", 0, start) + 1
            indent = markup[line_start:start]
            close = markup.index("\n" + indent + "}", start)
            return markup[line_start:close + len(indent) + 2]

        def grab_const(name):
            return re.search(r"const %s = [^\n]+;" % name, markup).group(0)

        script = (
            "function sourceName() { return ''; }\n"
            "function renderEditor() {}\n"
            "function closeEditor() {}\n"
            "function loadSchedules() {}\n"
            "const TOASTS = [];\n"
            "function showToast(m) { TOASTS.push(m); }\n"
            "const POSTS = [];\n"
            "function postJson(url, payload) { POSTS.push(payload); return {then() {}}; }\n"
            + grab_const("HHMM_RE") + "\n"
            + grab_const("MAX_STEP_OFFSET_MIN") + "\n"
            + grab("toMinutes") + "\n"
            + grab("fromMinutes") + "\n"
            + grab("stepOffset") + "\n"
            + grab("foldUntils") + "\n"
            + grab("saveEditor") + "\n"
            + script_tail
        )
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def _editor(self, steps, time="06:00"):
        return ("let EDITOR = " + json.dumps({
            "id": None, "label": "", "time": time, "days": [0],
            "enabled": True, "steps": steps}) + ";\n")

    def test_until_saves_as_a_pause_at_that_time(self):
        posted = self._run(
            self._editor([{"at": "06:00", "action": "play",
                           "uri": "spotify:playlist:abc",
                           "volume": "25", "until": "07:30"}])
            + "saveEditor();\nconsole.log(JSON.stringify(POSTS[0]));")
        assert posted["steps"] == [
            {"offset": 0, "action": "play",
             "uri": "spotify:playlist:abc", "volume": 25},
            {"offset": 90, "action": "pause"},
        ]

    def test_an_until_past_midnight_wraps(self):
        """A 23:00 show ending at 00:30 is 90 minutes, not -22.5 hours."""
        posted = self._run(
            self._editor([{"at": "23:00", "action": "play",
                           "uri": "spotify:playlist:abc",
                           "volume": "", "until": "00:30"}], time="23:00")
            + "saveEditor();\nconsole.log(JSON.stringify(POSTS[0]));")
        assert posted["steps"][1] == {"offset": 90, "action": "pause"}

    def test_a_zero_length_until_is_dropped(self):
        posted = self._run(
            self._editor([{"at": "06:00", "action": "play",
                           "uri": "spotify:playlist:abc",
                           "volume": "", "until": "06:00"}])
            + "saveEditor();\nconsole.log(JSON.stringify(POSTS[0]));")
        assert [s["action"] for s in posted["steps"]] == ["play"]

    def test_an_until_past_the_cap_blocks_the_save(self):
        out = self._run(
            self._editor([{"at": "06:00", "action": "play",
                           "uri": "spotify:playlist:abc",
                           "volume": "", "until": "19:30"}])
            + "saveEditor();\n"
            + "console.log(JSON.stringify({posts: POSTS, toasts: TOASTS}));")
        assert out["posts"] == []
        assert any("12 hours" in t for t in out["toasts"])

    def test_a_pause_after_a_play_reads_back_as_until(self):
        folded = self._run(
            "console.log(JSON.stringify(foldUntils(["
            "{at: '06:00', action: 'play', uri: 'spotify:playlist:abc'},"
            "{at: '07:30', action: 'pause'}], '06:00')));")
        assert len(folded) == 1
        assert folded[0]["action"] == "play"
        assert folded[0]["until"] == "07:30"

    def test_a_standalone_pause_stays_a_visible_step(self):
        folded = self._run(
            "console.log(JSON.stringify(foldUntils(["
            "{at: '06:00', action: 'pause'}], '06:00')));")
        assert [s["action"] for s in folded] == ["pause"]

    def test_only_one_pause_folds_per_play(self):
        folded = self._run(
            "console.log(JSON.stringify(foldUntils(["
            "{at: '06:00', action: 'play', uri: 'spotify:playlist:abc'},"
            "{at: '07:00', action: 'pause'},"
            "{at: '07:30', action: 'pause'}], '06:00')));")
        assert [s["action"] for s in folded] == ["play", "pause"]
        assert folded[0]["until"] == "07:00"


class TestNextRunIsAlwaysVisible:
    """The missing feedback that let a Sunday-only alarm go unnoticed."""

    def test_the_list_shows_it(self, js):
        routine = re.search(r"function renderRoutine\(s\) \{.*?\n  \}", js, re.S).group(0)
        assert "s.next_run" in routine
        assert "describeWhen" in routine

    def test_the_draft_shows_it_before_saving(self, js):
        assert "nextRunPreview(EDITOR.time, EDITOR.days)" in js

    def test_no_days_selected_is_called_out_rather_than_left_blank(self, js):
        assert "Never runs — pick at least one day" in js
        assert "never runs — no days selected" in js

    def test_the_preview_uses_the_servers_monday_first_convention(self, js):
        """JS getDay() is Sunday-first; the scheduler is Monday-first. Getting
        this backwards is exactly the class of bug being fixed."""
        body = re.search(r"function nextRunPreview\(time, days\) \{.*?\n  \}", js, re.S).group(0)
        assert "(when.getDay() + 6) % 7" in body

    def test_the_preview_skips_a_time_that_has_already_passed(self, js):
        body = re.search(r"function nextRunPreview\(time, days\) \{.*?\n  \}", js, re.S).group(0)
        assert "if (when > now) return when;" in body

    def test_empty_days_previews_as_every_day(self, js):
        body = re.search(r"function nextRunPreview\(time, days\) \{.*?\n  \}", js, re.S).group(0)
        assert "[0, 1, 2, 3, 4, 5, 6]" in body


class TestDayPickingIsHardToGetWrong:
    def test_presets_exist(self, js):
        for preset in ("weekdays", "weekend", "every"):
            assert r"editorPreset(\'%s\')" % preset in js

    def test_weekdays_means_monday_to_friday(self, js):
        body = re.search(r"function editorPreset\(which\) \{.*?\n  \}", js, re.S).group(0)
        assert "which === 'weekdays' ? [0, 1, 2, 3, 4]" in body

    def test_weekend_means_saturday_and_sunday(self, js):
        body = re.search(r"function editorPreset\(which\) \{.*?\n  \}", js, re.S).group(0)
        assert "which === 'weekend' ? [5, 6]" in body

    def test_selected_days_are_visually_distinct(self, css):
        assert ".daybtn.on" in css

    def test_day_buttons_report_their_state_to_a_screen_reader(self, js):
        assert "aria-pressed=" in js


class TestScheduleFromAnywhere:
    """Picking the music was the hardest part of the old flow and had the
    worst control: a dropdown whose escape hatch was a JS prompt asking for a
    URI copied out of the Spotify desktop app."""

    def test_a_track_can_be_scheduled_from_search(self, js):
        row = re.search(r"function trackRow\(item, nested\) \{.*?\n  \}", js, re.S).group(0)
        assert "scheduleUri(" in row

    def test_an_album_can_be_scheduled_from_search(self, js):
        row = re.search(r"function renderAlbumRow\(a\) \{.*?\n  \}", js, re.S).group(0)
        assert "scheduleUri(" in row

    def test_a_station_can_be_scheduled(self, js):
        row = re.search(r"function renderStations\(\) \{.*?\n  \}", js, re.S).group(0)
        assert "scheduleUri(" in row

    def test_what_is_playing_can_be_scheduled(self, markup, js):
        assert "scheduleNowPlaying()" in markup
        assert re.search(r"function scheduleNowPlaying\(\).*?fetch\('/nowplaying'\)", js, re.S)

    def test_nothing_playing_is_reported_rather_than_scheduling_undefined(self, js):
        body = re.search(r"function scheduleNowPlaying\(\) \{.*?\n  \}", js, re.S).group(0)
        assert "if (!data || !data.uri)" in body

    def test_it_arrives_prefilled_so_only_the_time_is_left_to_pick(self, js):
        body = re.search(r"function openEditor\(routine, prefill\) \{.*?\n  \}", js, re.S).group(0)
        assert "EDITOR.label = prefill.name" in body
        assert "uri: prefill.uri" in body

    def test_a_uri_from_search_stays_selected_in_the_dropdown(self, js):
        """It is in neither the playlist nor the station list, so without this
        the step silently resets to 'choose a playlist'."""
        body = re.search(r"function editorStepOptions\(step\) \{.*?\n  \}", js, re.S).group(0)
        assert "if (step.uri && !known)" in body
        assert "selected" in body


class TestRoutinesReadAsMusicNotUris:
    def test_steps_name_the_playlist(self, js):
        body = re.search(r"function describeStep\(step\) \{.*?\n  \}", js, re.S).group(0)
        assert "sourceName(step.uri)" in body
        assert "step.uri || ''" not in body


class TestTheEditorIsUsableOnAPhone:
    def test_it_is_a_bottom_sheet_by_default(self, css):
        rule = re.search(r"\.editor \{([^}]*)\}", css).group(1)
        assert "position: fixed" in rule
        assert "inset: auto 0 0 0" in rule

    def test_it_becomes_a_centred_dialog_on_a_wide_screen(self, css):
        assert re.search(r"@media \(min-width: 760px\) \{\s*\.editor \{", css)

    def test_it_can_scroll_when_a_routine_has_many_steps(self, css):
        rule = re.search(r"\.editor \{([^}]*)\}", css).group(1)
        assert "overflow-y: auto" in rule

    def test_it_clears_the_home_indicator(self, css):
        rule = re.search(r"\.editor \{([^}]*)\}", css).group(1)
        assert "env(safe-area-inset-bottom)" in rule

    def test_it_is_announced_as_a_dialog(self, markup):
        block = markup[markup.index('id="editor"') - 200:markup.index('id="ed-body"')]
        assert 'role="dialog"' in block
        assert 'aria-modal="true"' in block

    def test_the_scrim_sits_under_the_editor_and_over_the_page(self, css):
        scrim = int(re.search(r"\.scrim \{[^}]*z-index: (\d+)", css).group(1))
        editor = int(re.search(r"\.editor \{[^}]*z-index: (\d+)", css).group(1))
        assert scrim < editor


class TestATitleCannotBreakOutOfTheHandler:
    """A track name now reaches an onclick attribute, which it never did
    before -- it was only ever text content. escapeHtml is not enough there:
    it turns an apostrophe into &#39;, and the HTML parser decodes that back
    to a real quote inside the attribute, ending the JS string early.
    "Livin' on a Prayer" would be enough to break it.
    """

    CALLERS = ["trackRow", "renderAlbumRow", "renderStations"]

    def _call(self, js, fn):
        body = re.search(r"function %s\(.*?\n  \}" % fn, js, re.S).group(0)
        line = re.search(r"scheduleUri.{0,200}", body, re.S)
        assert line, "no scheduleUri call in " + fn
        return line.group(0)

    @pytest.mark.parametrize("fn", CALLERS)
    def test_the_uri_is_escaped(self, js, fn):
        assert "escapeHtml" in self._call(js, fn)

    @pytest.mark.parametrize("fn", CALLERS)
    def test_the_name_is_percent_encoded(self, js, fn):
        assert "encodeURIComponent" in self._call(js, fn)

    @pytest.mark.parametrize("fn", CALLERS)
    def test_the_apostrophe_is_encoded_too(self, js, fn):
        """encodeURIComponent alone is not enough: ' is in its unreserved set
        (A-Za-z0-9 -_.!~*'()) and comes through untouched."""
        assert r"replace(/'/g, '%27')" in self._call(js, fn)

    @pytest.mark.parametrize("fn", CALLERS)
    def test_escapehtml_alone_is_not_relied_on_for_the_name(self, js, fn):
        call = self._call(js, fn)
        assert not re.search(r"escapeHtml\(\s*(String\()?\w+\.name", call)

    def test_the_receiver_decodes_it(self, js):
        assert "decodeURIComponent(encodedName)" in js

    # JS encodeURIComponent leaves exactly these unescaped. The apostrophe
    # being among them is the whole reason for the extra replace.
    JS_UNRESERVED = "!~*'()"

    def _encode_like_js(self, name):
        from urllib.parse import quote
        return quote(name, safe=self.JS_UNRESERVED).replace("'", "%27")

    def test_plain_encoding_would_have_let_an_apostrophe_through(self):
        """Guards the reason for the replace, so nobody removes it as noise."""
        from urllib.parse import quote
        assert "'" in quote("Livin' on a Prayer", safe=self.JS_UNRESERVED)

    def test_no_quote_character_survives_encoding(self):
        hostile = """Livin' on a Prayer</b>');alert(1);//"""
        encoded = self._encode_like_js(hostile)
        assert "'" not in encoded and '"' not in encoded and "<" not in encoded

    def test_the_apostrophe_survives_the_round_trip(self):
        """Stripping quotes would have been safe too, but would have turned
        "Rock 'n' Roll" into "Rock n Roll" in the routine name."""
        from urllib.parse import unquote
        for name in ["Livin' on a Prayer", "Rock 'n' Roll", 'He said "hi"']:
            assert unquote(self._encode_like_js(name)) == name


class TestAFailedRoutineIsVisible:
    """A routine that quietly did nothing used to leave its only trace in a
    rotating log. The server records last_error per step; the point of these
    is that the card actually says so."""

    def test_a_failed_step_is_marked(self, js):
        assert "step.last_error ? ' failed' : ''" in js

    def test_a_failed_step_swaps_its_icon_for_a_warning(self, js):
        assert "step.last_error ? '⚠'" in js

    def test_the_card_carries_the_failure(self, js):
        assert "class=\"last-error\"" in js
        assert "describeFailure" in js

    def test_the_failure_text_is_escaped(self, js):
        """The message travels from Sonos through the server into innerHTML."""
        assert "escapeHtml(failure)" in js

    def test_the_styling_reuses_the_existing_warning_colour(self, css):
        """'Something here needs looking at' should read the same way as the
        never-runs warning does."""
        assert ".last-error {" in css
        block = css.split(".last-error {", 1)[1].split("}", 1)[0]
        assert "var(--accent-2)" in block


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestFailureWording:
    """Run the real helpers rather than trusting a grep."""

    @staticmethod
    def _describe(failed, today="2026-08-04"):
        with open(INDEX_HTML) as handle:
            markup = handle.read()

        def grab(name):
            start = markup.index("function " + name + "(")
            line_start = markup.rfind("\n", 0, start) + 1
            indent = markup[line_start:start]
            close = markup.index("\n" + indent + "}", start)
            return markup[line_start:close + len(indent) + 2]

        script = (
            # Assigned through globalThis rather than declared: `class Date`
            # in this scope would put the name in the temporal dead zone and
            # `const RealDate = Date` above it would throw.
            "const RealDate = Date;\n"
            "globalThis.Date = class extends RealDate {\n"
            "  constructor(...a) { super(...(a.length ? a : ['" + today + "T12:00:00'])); }\n"
            "};\n"
            + grab("describeDay") + "\n"
            + grab("describeFailure") + "\n"
            "console.log(JSON.stringify(describeFailure("
            + json.dumps(failed) + ")));"
        )
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def test_it_names_the_day_and_the_reason(self):
        text = self._describe([{"last_error": {
            "date": "2026-08-04", "message": "Sonos request timed out",
            "attempts": 1, "final": False}}])
        assert "today" in text
        assert "Sonos request timed out" in text

    def test_yesterday_reads_as_yesterday(self):
        """A wake-up that failed this morning and one that failed last week
        deserve different reactions."""
        text = self._describe([{"last_error": {
            "date": "2026-08-03", "message": "boom", "attempts": 1, "final": False}}])
        assert "yesterday" in text

    def test_an_older_failure_keeps_its_date(self):
        text = self._describe([{"last_error": {
            "date": "2026-07-30", "message": "boom", "attempts": 1, "final": False}}])
        assert "2026-07-30" in text

    def test_giving_up_is_said_plainly(self):
        text = self._describe([{"last_error": {
            "date": "2026-08-04", "message": "boom", "attempts": 3, "final": True}}])
        assert "gave up after 3 tries" in text

    def test_one_try_is_not_pluralised(self):
        text = self._describe([{"last_error": {
            "date": "2026-08-04", "message": "boom", "attempts": 1, "final": True}}])
        assert "1 try" in text

    def test_the_final_failure_wins_over_a_retryable_one(self):
        """Two failed steps, one still retrying and one that gave up -- the
        one that gave up is the one worth reporting."""
        text = self._describe([
            {"last_error": {"date": "2026-08-04", "message": "still trying",
                            "attempts": 1, "final": False}},
            {"last_error": {"date": "2026-08-04", "message": "gave up here",
                            "attempts": 3, "final": True}},
        ])
        assert "gave up here" in text
